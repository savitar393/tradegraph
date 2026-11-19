"""Build auditable support graphs for findings in a pinned assessment run."""

from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Literal as Status

from rdflib import Graph, Literal, RDF, RDFS, URIRef

from tradegraph.cases.facts import CASE_DATA, TG
from tradegraph.contracts import AssessmentReport, CaseSnapshot, DomainFinding, EvidenceFinding, Finding, NamedFact
from tradegraph.graph.store import project_case_graphs
from tradegraph.reasoning.client import ReasoningClient


@dataclass(frozen=True, slots=True)
class SourceReference:
    clause_iri: str
    locator: str
    document_iri: str
    source_record_id: str
    url: str | None


@dataclass(frozen=True, slots=True)
class SupportNode:
    id: str
    kind: str
    fact: NamedFact | None = None
    reference_id: str | None = None
    rendering: str | None = None
    bindings: tuple[tuple[str, URIRef | Literal], ...] = ()
    shape_id: str | None = None


@dataclass(frozen=True, slots=True)
class SupportGraph:
    root: str
    nodes: tuple[SupportNode, ...]
    edges: tuple[tuple[str, str], ...]

    def reaches_asserted_facts(self) -> bool:
        assertions = {node.id for node in self.nodes if node.kind == "ASSERTED_FACT"}
        pending = [self.root]
        seen = set()
        while pending:
            node = pending.pop()
            if node in assertions:
                return True
            if node not in seen:
                seen.add(node)
                pending.extend(child for parent, child in self.edges if parent == node)
        return False


@dataclass(frozen=True, slots=True)
class Explanation:
    finding_id: str
    run_id: str
    run_at: datetime | None
    case_id: str
    case_revision: int
    context_iri: str
    kb_sha256: str
    engine_profile_id: str
    support_graph: SupportGraph
    source_refs: tuple[SourceReference, ...]
    availability: Status["AVAILABLE", "PARTIAL", "EXPLANATION_UNAVAILABLE"]
    diagnostics: tuple[str, ...] = ()


type Explainable = Finding | EvidenceFinding | DomainFinding


class ProvenanceService:
    def __init__(self, client: ReasoningClient, *, timeout_ms: int = 30_000, max_owl_checks: int = 96):
        if any(type(value) is not int or not 0 < value < 2**31 for value in (timeout_ms, max_owl_checks)):
            raise ValueError("Explanation budgets must be positive 32-bit integers.")
        self.client = client
        self.timeout_ms = timeout_ms
        self.max_owl_checks = max_owl_checks
        self._runs = {}
        self._findings = {}
        self._reports = {}

    def register(self, report: AssessmentReport, case: CaseSnapshot) -> None:
        pins = (
            report.kb_sha256, report.context.engine_profile_id, report.context.case_id, report.case_revision,
        )
        if pins != (case.context.kb_sha256, case.context.engine_profile_id, case.case_id, case.revision) or (
            report.context.assessment_context != case.context
        ):
            raise ValueError("Assessment report and case snapshot do not match.")
        if pins[:2] != (self.client.kb.snapshot.sha256, self.client.kb.snapshot.engine_profile_id):
            raise ValueError("Assessment report and registered KB do not match.")
        evidence = report.validation_report
        entries = report.findings + report.domain_findings + (evidence.evidence_findings if evidence else ())
        self._runs[report.run_id] = report, case
        self._findings.update({
            entry.finding_id: (report.run_id, entry) for entry in entries if entry.finding_id is not None
        })
        if evidence is not None and evidence.report_id is not None:
            self._reports[evidence.report_id] = evidence

    def validation_report(self, report_id: str):
        return self._reports[report_id]

    def load_run(self, run_id: str) -> tuple[AssessmentReport, CaseSnapshot]:
        return self._runs[run_id]

    def explain(self, finding_id: str) -> Explanation:
        run_id, finding = self._findings[finding_id]
        report, case = self._runs[run_id]
        builder = _Trace(self, report, case, finding_id)
        supported = builder.visit(finding)
        references = builder.references()
        if not supported:
            availability = "EXPLANATION_UNAVAILABLE"
        elif builder.diagnostics or isinstance(finding, DomainFinding):
            availability = "PARTIAL"
        else:
            availability = "AVAILABLE"
        context = report.context
        return Explanation(
            finding_id, run_id, report.run_at, case.case_id, case.revision, context.assessment_context_iri,
            context.kb_sha256, context.engine_profile_id,
            SupportGraph(finding_id, tuple(builder.nodes.values()), tuple(sorted(builder.edges))),
            references, availability, tuple(sorted(builder.diagnostics)),
        )


class _Trace:
    def __init__(self, service: ProvenanceService, report: AssessmentReport, case: CaseSnapshot, root: str):
        self.service = service
        self.report = report
        self.case = case
        self.root = root
        self.deadline = monotonic() + service.timeout_ms / 1000
        kb = service.client.kb
        graphs = project_case_graphs(case)
        self.asserted = graphs.facts + graphs.context
        self.evidence = graphs.evidence
        self.schema = kb.dataset.graph(kb.owl_projection_ref)
        self.references_graph = kb.dataset.graph(kb.reference_graph_ref)
        self.inferred = {finding.triple: finding for finding in report.findings}
        self.nodes: dict[str, SupportNode] = {}
        self.edges: set[tuple[str, str]] = set()
        self.clauses: set[str] = set()
        self.diagnostics: set[str] = set()
        self.visited: dict[str, bool] = {}

    def add(self, parent, kind, **fields):
        ident = f"{self.root}:support:{len(self.nodes)}"
        self.nodes[ident] = SupportNode(ident, kind, **fields)
        self.edges.add((parent, ident))
        return ident

    def visit(self, finding: Explainable) -> bool:
        ident = finding.finding_id
        if ident in self.visited:
            return self.visited[ident]
        self.visited[ident] = False
        if isinstance(finding, Finding):
            self.nodes[ident] = SupportNode(ident, finding.kind, fact=finding.triple)
            found = self.inference(finding)
        elif isinstance(finding, EvidenceFinding):
            self.nodes[ident] = SupportNode(ident, "VALIDATION", reference_id=finding.definition_id)
            found = self.validation(finding)
        else:
            self.nodes[ident] = SupportNode(ident, finding.kind, reference_id=finding.policy_id)
            found = self.policy(finding)
        self.visited[ident] = found
        return found

    def inference(self, finding: Finding) -> bool:
        root = finding.finding_id
        proof = self.service.client.replay_support(
            self.case, finding, self.report.findings, deadline=self.deadline,
            max_owl_checks=self.service.max_owl_checks,
        )
        self.diagnostics.update(proof.diagnostics)
        if proof.explanation_status != "AVAILABLE":
            return False
        if proof.rule is not None:
            rule = proof.rule
            node = self.add(root, "SWRL_RULE", reference_id=rule.rule_id, bindings=rule.bindings)
            self.clauses.update(rule.source_clauses)
            for premise in rule.premises:
                self.premise(node, premise)
        for axiom in proof.axioms:
            self.clauses.update(axiom.source_clauses)
            node = self.add(root, "OWL_AXIOM", reference_id=next(iter(axiom.axiom_ids), None),
                            rendering=axiom.rendering)
            if axiom.fact is not None:
                self.premise(node, axiom.fact)
        return True

    def premise(self, parent, fact):
        if assertion := _matching_assertion(self.asserted, fact):
            self.add(parent, "ASSERTED_FACT", fact=assertion)
        elif prior := self.inferred.get(fact):
            self.edges.add((parent, prior.finding_id))
            if not self.visit(prior):
                self.diagnostics.add("A derived premise has no supported replay.")
        elif assertion := _matching_assertion(self.schema, fact):
            self.add(parent, "KB_ASSERTION", fact=assertion)
        else:
            self.add(parent, "UNRESOLVED_PREMISE", fact=fact)
            self.diagnostics.add("A replayed premise could not be linked to an asserted fact or earlier finding.")

    def validation(self, finding: EvidenceFinding) -> bool:
        report = self.report.validation_report
        if report is None or report.report_id is None or finding.shacl_report_turtle is None:
            self.diagnostics.add("No SHACL execution report exists for the selected finding.")
            return False
        node = self.add(finding.finding_id, "VALIDATION_REPORT", reference_id=report.report_id,
                        shape_id=finding.shape_id)
        if finding.focus_iri is not None:
            for triple in sorted(self.evidence.triples((URIRef(finding.focus_iri), None, None))):
                self.add(node, "ASSERTED_FACT", fact=triple)
        return True

    def policy(self, finding: DomainFinding) -> bool:
        root = finding.finding_id
        self.clauses.update(finding.source_clauses)
        if not finding.decision_query_ids:
            self.diagnostics.add("No decision query returned a witness for the selected policy outcome.")
            return False
        for query_id in finding.decision_query_ids:
            self.add(root, "DECISION_QUERY", reference_id=query_id)
        for fact in finding.proof_facts:
            self.premise(root, fact)
        if self.report.validation_report is not None:
            for checked in self.report.validation_report.evidence_findings:
                if checked.focus_iri is None or checked.status != "SATISFIED":
                    continue
                document = self.evidence.value(URIRef(checked.focus_iri), CASE_DATA.sourceReference)
                if str(document) in finding.evidence:
                    self.edges.add((root, checked.finding_id))
                    self.visit(checked)
        self.diagnostics.add("The query witness identifies selected supports. A full query proof is unavailable.")
        return True

    def references(self) -> tuple[SourceReference, ...]:
        result = []
        for clause_iri in sorted(self.clauses):
            clause = URIRef(clause_iri)
            document = self.references_graph.value(clause, TG.clauseInDocument)
            locator = self.references_graph.value(clause, TG.clauseLocator)
            version = self.references_graph.value(document, TG.sourceRecordId) if isinstance(document, URIRef) else None
            if not isinstance(locator, Literal) or not isinstance(version, Literal):
                self.diagnostics.add(f"Source version for {clause_iri} is unavailable in the pinned KB.")
                continue
            url = self.references_graph.value(document, RDFS.seeAlso)
            result.append(SourceReference(clause_iri, str(locator), str(document), str(version),
                                          str(url) if isinstance(url, URIRef) else None))
        return tuple(result)


def _matching_assertion(graph: Graph, fact: NamedFact):
    if fact in graph:
        return fact
    subject, predicate, value = fact
    if isinstance(value, Literal):
        for actual in graph.objects(subject, predicate):
            if isinstance(actual, Literal) and actual.datatype == value.datatype and actual.language == value.language:
                if value.eq(actual) is True:
                    return subject, predicate, actual
    return None
