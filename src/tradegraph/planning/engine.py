"""Bounded search over assessed case branches and declarative RDF effects."""

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, localcontext
from time import monotonic

from rdflib import Graph, Literal, URIRef, XSD

from tradegraph.assessment.policies import assessment_graph
from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.facts import CASE_DATA, canonical_assertion, parse_persisted_facts, rebase_triple, revision_ref
from tradegraph.contracts import (
    AssessmentReport, AssessmentRequest, CaseSnapshot, FactChanges, FactPatch, PlanCandidate,
    PlanningAction, PlanRequest, PlanStep,
)
from tradegraph.kb.loader import CATALOG
from tradegraph.kb.queries import context_bindings, prepare_local_query
from tradegraph.validation.service import exact_decimal_context


@dataclass(frozen=True, slots=True)
class _State:
    case: CaseSnapshot
    report: AssessmentReport
    steps: tuple[PlanStep, ...] = ()


class _DeadlineReached(Exception):
    pass


class PlanningEngine:
    def __init__(self, assessment: AssessmentService):
        self.assessment = assessment

    def search_plans(self, request: PlanRequest) -> list[PlanCandidate]:
        deadline = monotonic() + request.limits.timeout_ms / 1000
        report, case = self.assessment.provenance.load_run(request.base_run_id)
        kb = self.assessment.client.kb
        if not _usable(report) or report.kb_sha256 != kb.snapshot.sha256:
            raise ValueError("Planning requires a converged, consistent baseline on the registered KB.")
        goal = kb.goal_catalog.get(request.goal_iri)
        objective = kb.objective_catalog.get(request.objective_id)
        if goal is None or objective is None:
            raise ValueError("Use a goal and an objective from the registered KB.")

        actions = _actions(kb.action_catalog, request)
        dependencies = {key for action in actions.values() for key in action.dependencies}
        compiled = {}
        root = revision_ref(case.case_id, case.revision)
        facts = parse_persisted_facts(case.facts_turtle, case.case_id)
        seen = {(frozenset(facts), frozenset(), case.evidence_scope_closed)}
        first = _State(case, report)
        queue = deque([first])
        deepest = first
        accepted = []
        cutoffs = set()
        attempts = 1
        reference_graphs = tuple(kb.dataset.graph(graph_id) for graph_id in (
            kb.owl_projection_ref, kb.definitions_graph_ref,
        ))
        fixed_terms = {term for graph in reference_graphs for triple in graph
                       for term in triple if isinstance(term, URIRef)}
        fixed_subjects = {subject for graph in reference_graphs for subject in graph.subjects()
                          if isinstance(subject, URIRef)}

        def query(sparql, kind):
            key = sparql, kind
            if key not in compiled:
                compiled[key] = prepare_local_query(sparql, kind)
            return compiled[key]

        try:
            while queue:
                _remaining(deadline)
                state = queue.popleft()
                extra_coverage = tuple(
                    (URIRef(f"{revision_ref(state.case.case_id, state.case.revision)}/context"),
                     CASE_DATA.assessedCoverage, URIRef(entry.iri))
                    for entry in state.report.coverage if entry.status in {"SUPPORTED", "PARTIAL"}
                )
                data, context = assessment_graph(
                    kb, state.case, ((finding.triple, finding.support) for finding in state.report.findings),
                    state.report.validation_report, extra_triples=extra_coverage,
                )
                selectors = context_bindings(context)
                if data.query(query(goal.sparql, "AskQuery"), initBindings=selectors).askAnswer is True:
                    accepted.append(state)
                    continue
                asserted = set(parse_persisted_facts(state.case.facts_turtle, state.case.case_id))
                terms = {term for subject, _, value in asserted | set(context) for term in (subject, value)}
                terms.update(selectors.values())
                current_revision = revision_ref(state.case.case_id, state.case.revision)
                completed = {step.action_id for step in state.steps}
                for action_id, action in actions.items():
                    if not set(action.dependencies) <= completed:
                        continue
                    for bindings, patch in _transitions(action, data, selectors, asserted, terms, fixed_terms,
                                                       fixed_subjects, query, deadline):
                        next_facts = (asserted - set(patch.removed)) | set(patch.added)
                        signature = (
                            frozenset(rebase_triple(triple, current_revision, root) for triple in next_facts),
                            frozenset((completed | {action_id}) & dependencies),
                            None,  # An asserted fact edit clears the earlier evidence-closure declaration.
                        )
                        if signature in seen:
                            continue
                        if len(state.steps) >= request.limits.max_depth:
                            cutoffs.add("Maximum depth reached.")
                            continue
                        if attempts >= request.limits.max_states:
                            cutoffs.add("Maximum states reached.")
                            continue
                        _remaining(deadline)
                        seen.add(signature)
                        attempts += 1
                        branch = self.assessment.repository.revise_case(
                            state.case.case_id, state.case.revision,
                            FactPatch(f"Hypothetical action {action_id}", patch.added, patch.removed),
                            branch=True,
                        )
                        result = self.assessment.assess(AssessmentRequest(
                            report.kb_sha256, branch.case_id, branch.revision, tuple(report.query_results),
                        ), timeout_ms=_remaining(deadline))
                        if not _usable(result):
                            if result.consistency != "INCONSISTENT":
                                cutoffs.add(f"A candidate assessment returned {result.execution_status}.")
                            continue
                        step = PlanStep(action_id, bindings, patch, action.source_clauses, action.cost, action.duration)
                        child = _State(branch, result, (*state.steps, step))
                        if len(child.steps) > len(deepest.steps):
                            deepest = child
                        queue.append(child)
            _remaining(deadline)
        except _DeadlineReached:
            cutoffs.add("Planning time budget expired.")

        candidates = [
            _candidate(state, True, cutoffs) for state in accepted
        ] or ([_candidate(deepest, False, cutoffs)] if cutoffs else [])
        return sorted(candidates, key=lambda candidate: _rank(candidate, objective))


def _actions(catalog, request) -> dict[str, PlanningAction]:
    allowed = set(request.allowed_action_ids) or {key for key, action in catalog.items() if action.planning is not None}
    for key in allowed:
        if key not in catalog or catalog[key].planning is None:
            raise ValueError(f"Unknown executable action {key}.")
    chosen = {key for key in allowed if request.goal_iri in catalog[key].planning.goal_iris}
    pending = list(chosen)
    while pending:
        dependencies = (set(catalog[pending.pop()].planning.dependencies) & allowed) - chosen
        chosen.update(dependencies)
        pending.extend(dependencies)
    return {key: catalog[key].planning for key in sorted(chosen)}


def _transitions(action, data, selectors, asserted, terms, fixed_terms, fixed_subjects, query, deadline):
    bound = dict(selectors)
    for parameter in action.parameters:
        if parameter.context_binding is not None:
            term = selectors.get(parameter.context_binding)
            if not _typed(parameter.datatype_iri, term):
                return
            bound[parameter.name] = term
    rows = (
        (row.asdict() for row in data.query(query(action.grounding_sparql, "SelectQuery"), initBindings=bound))
        if action.grounding_sparql else ({},)
    )
    for row in rows:
        _remaining(deadline)
        bindings = dict(bound)
        for parameter in action.parameters:
            if parameter.context_binding is None and parameter.name in row:
                bindings[parameter.name] = row[parameter.name]
        actual = tuple((parameter.name, bindings.get(parameter.name)) for parameter in action.parameters)
        if any(term not in terms or not _typed(parameter.datatype_iri, term)
               for parameter, (_, term) in zip(action.parameters, actual, strict=True)):
            continue
        if any(data.query(query(condition, "AskQuery"), initBindings=bindings).askAnswer is not True
               for condition in action.conditions):
            continue
        added, removed = set(), set()
        for change in action.changes:
            _remaining(deadline)
            result = data.query(query(change.sparql, "ConstructQuery"), initBindings=bindings)
            (added if change.operation == "ADD" else removed).update(result.graph)
        if any(not isinstance(subject, URIRef) or subject not in terms or subject in fixed_subjects
               or not isinstance(value, (URIRef, Literal))
               or (isinstance(value, URIRef) and value not in terms and value not in fixed_terms)
               for subject, _, value in added | removed):
            continue
        try:
            result = (asserted - {canonical_assertion(triple) for triple in removed}) | {
                canonical_assertion(triple) for triple in added
            }
        except ValueError:
            continue
        if result != asserted:
            yield actual, FactChanges(tuple(sorted(result - asserted)), tuple(sorted(asserted - result)))


def _typed(datatype, term):
    if datatype == str(CATALOG.IRI):
        return isinstance(term, URIRef)
    return isinstance(term, Literal) and str(term.datatype) == datatype and not term.ill_typed


def _remaining(deadline) -> int:
    left = deadline - monotonic()
    if left <= 0:
        raise _DeadlineReached
    return max(1, int(left * 1000))


def _usable(report):
    return report.execution_status == "CONVERGED" and report.consistency == "CONSISTENT"


def _total(steps, field):
    quantities = tuple(getattr(step, field) for step in steps)
    if not quantities or any(quantity is None for quantity in quantities):
        return None, None
    units = {quantity.unit_iri for quantity in quantities}
    if len(units) != 1:
        return None, None
    values = {str(index): Literal(item.value, datatype=XSD.decimal) for index, item in enumerate(quantities)}
    with localcontext(exact_decimal_context(Graph(), values)):
        return sum((item.value for item in quantities), Decimal(0)), units.pop()


def _candidate(state, goal_reached, cutoffs):
    cost, currency = _total(state.steps, "cost")
    duration, duration_unit = _total(state.steps, "duration")
    report = state.report
    evidence = report.validation_report.evidence_findings if report.validation_report else ()
    assumptions = ["Effects in this candidate are hypothetical."]
    if state.steps and cost is None:
        assumptions.append("Total cost is unknown or uses incompatible units.")
    if state.steps and duration is None:
        assumptions.append("Total duration is unknown or uses incompatible units.")
    return PlanCandidate(
        state.steps, tuple(assumptions), report.findings + report.domain_findings + evidence,
        cost, currency, duration, duration_unit, "TRUNCATED" if cutoffs else "COMPLETE", False,
        goal_reached, True, report.run_id, state.case.revision, tuple(sorted(cutoffs)),
    )


def _rank(candidate, objective):
    if objective.metric == "COST":
        value = candidate.cost if candidate.cost_unit_iri == objective.unit_iri else None
    elif objective.metric == "DURATION":
        value = candidate.duration if candidate.duration_unit_iri == objective.unit_iri else None
    else:
        value = len(candidate.steps)
    return value is None, value or 0, len(candidate.steps), tuple(step.action_id for step in candidate.steps)
