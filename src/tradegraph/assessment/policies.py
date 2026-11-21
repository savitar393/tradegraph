"""Evaluate the case policies and evidence admission declared by a compiled KB."""

from collections.abc import Iterable, Mapping
from decimal import Context, Inexact, localcontext

from rdflib import Graph, Literal, RDF, URIRef

from tradegraph.cases.facts import CASE_DATA, TG
from tradegraph.contracts import (
    CaseSnapshot, CompiledKB, CoverageEntry, DomainFinding, FactSupport, InferenceResult, NamedFact,
    PolicyDefinition, ValidationReport,
)
from tradegraph.graph.store import project_case_graphs
from tradegraph.kb.queries import prepare_local_query
from tradegraph.reasoning.slicing import slice_owl_for_case
from tradegraph.validation.service import exact_decimal_context


def evaluate_policies(
    kb: CompiledKB, case: CaseSnapshot, result: InferenceResult, evidence: ValidationReport | None,
) -> tuple[tuple[CoverageEntry, ...], tuple[DomainFinding, ...], str]:
    data, context_graph = assessment_graph(
        kb, case, zip(result.inferred_facts, result.supports, strict=True), evidence,
    )
    context = context_graph.identifier
    matches = {str(row["scope"]) for row in _select(data, kb.coverage_query.sparql, {"context": context})}
    coverage = tuple(entry for entry in kb.snapshot.coverage if entry.iri in matches)
    scopes = {entry.iri for entry in coverage if entry.status in {"SUPPORTED", "PARTIAL"}}
    if not scopes:
        return coverage, (), "OUT_OF_COVERAGE"
    arithmetic = exact_decimal_context(data, {})
    findings = tuple(
        _finding(kb, data, context_graph, policy_id, policy, arithmetic)
        for policy_id, policy in kb.policy_catalog.items() if policy.scope_iri in scopes
    )
    status = "SUPPORTED" if all(entry.status == "SUPPORTED" for entry in coverage) else "PARTIAL"
    return coverage, findings, status


def assessment_graph(
    kb: CompiledKB, case: CaseSnapshot, inferred: Iterable[tuple[NamedFact, FactSupport]],
    evidence: ValidationReport | None, *, extra_triples: Iterable[NamedFact] = (),
) -> tuple[Graph, Graph]:
    """Combine one assessed revision with the documents admitted by its validation run."""
    graphs = project_case_graphs(case)
    context = graphs.context.identifier
    owl = kb.dataset.graph(kb.owl_projection_ref)
    defs = kb.dataset.graph(kb.definitions_graph_ref)
    case_delta = Graph()
    for source_graph in (graphs.facts, graphs.context):
        for triple in source_graph:
            case_delta.add(triple)
    pinned = set(graphs.context.predicates(context)) | {TG.usesEvidenceSet}
    for triple, support in inferred:
        subject, predicate, value = triple
        if subject != context or predicate not in pinned:
            case_delta.add(triple)
            if support.kind == "RULE_DERIVED" and predicate == RDF.type:
                case_delta.add((subject, CASE_DATA.ruleDerivedType, value))

    if evidence is not None:
        for finding in evidence.evidence_findings:
            if finding.status == "SATISFIED" and kb.evidence_catalog[finding.definition_id].admit:
                source = graphs.evidence.value(URIRef(finding.focus_iri), CASE_DATA.sourceReference)
                if isinstance(source, URIRef):
                    case_delta.add((context, CASE_DATA.admittedDocument, source))

    for triple in extra_triples:
        case_delta.add(triple)

    sliced_owl = slice_owl_for_case(owl, case_delta, graphs.context, Graph())
    data = Graph()
    for source_graph in (sliced_owl, defs, case_delta):
        for triple in source_graph:
            data.add(triple)
    return data, graphs.context


def _select(data: Graph, sparql: str, bindings: Mapping):
    query = prepare_local_query(sparql, "SelectQuery")
    return tuple(row.asdict() for row in data.query(query, initBindings=bindings))


def _finding(kb, data, context_graph, policy_id: str, policy: PolicyDefinition, arithmetic: Context) -> DomainFinding:
    context = context_graph.identifier
    bindings = {"context": context, "definition": URIRef(policy.iri)}
    jurisdiction = context_graph.value(context, TG.appliesInJurisdiction)
    roles = ()
    if policy.role_iri is not None:
        revision = context_graph.value(context, TG.assessesRevision)
        roles = tuple(sorted({
            str(actor) for assignment in data.objects(revision, TG.revisionRoleAssignment)
            if (assignment, TG.hasActorRole, URIRef(policy.role_iri)) in data
            for actor in data.objects(assignment, TG.roleActor) if isinstance(actor, URIRef)
        }))

    def evaluate(query_id):
        if query_id is None:
            return ()
        query = kb.decision_queries[query_id]
        with localcontext(arithmetic) as numeric:
            numeric.traps[Inexact] = query.requires_exact_decimal
            return _select(data, query.sparql, bindings)

    rows = ()
    selected_queries = ()
    diagnostics = ()
    if policy.jurisdiction_iri is not None and jurisdiction != URIRef(policy.jurisdiction_iri):
        status, roles = "NOT_APPLICABLE", ()
    elif exception := evaluate(policy.exception_query):
        status, rows = "NOT_APPLICABLE", exception
        selected_queries = (policy.exception_query,)
    else:
        satisfied = evaluate(policy.satisfied_query)
        failed = evaluate(policy.failed_query)
        if satisfied and failed:
            status, rows = "UNDETERMINED", satisfied + failed
            selected_queries = (policy.satisfied_query, policy.failed_query)
            diagnostics = ("Conflicting support for satisfaction and failure.",)
        elif satisfied:
            status, rows = "SATISFIED", satisfied
            selected_queries = (policy.satisfied_query,)
        elif failed:
            status, rows = "NOT_SATISFIED", failed
            selected_queries = (policy.failed_query,)
        else:
            status = "UNDETERMINED"
            diagnostics = ("No applicable outcome was established from the pinned facts and admitted evidence.",)
    proof_facts = set()
    for row in rows:
        subject, predicate, value = (row.get(name) for name in ("supportSubject", "supportPredicate", "supportObject"))
        if isinstance(subject, URIRef) and isinstance(predicate, URIRef) and isinstance(value, (URIRef, Literal)):
            if (subject, predicate, value) in data:
                proof_facts.add((subject, predicate, value))
    return DomainFinding(
        policy_id, policy.label, policy.kind, status, policy.scope_iri, policy.source_clauses,
        tuple(sorted({str(row["evidence"]) for row in rows if isinstance(row.get("evidence"), URIRef)})),
        roles, tuple(sorted({str(row["deadline"]) for row in rows if "deadline" in row})), diagnostics,
        tuple(query for query in selected_queries if query is not None), tuple(sorted(proof_facts)),
    )
