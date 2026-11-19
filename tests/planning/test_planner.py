from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rdflib import Namespace

from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext, AssessmentReport, PlanningLimits, PlanRequest, ReasoningContext
from tradegraph.graph.store import project_case_graphs
from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb
from tradegraph.planning import engine
from tradegraph.planning.engine import PlanningEngine


ROOT = Path(__file__).resolve().parents[2]
FX = Namespace("urn:tradegraph:test:planning#")
DATA = Namespace("urn:tradegraph:test:instance#")


class RecordingAssessment:
    """Exercise planner control flow without starting a JVM for every synthetic state."""

    def __init__(self, repository, kb):
        self.repository = repository
        self.client = SimpleNamespace(kb=kb)
        self.provenance = SimpleNamespace(load_run=lambda run_id: self.runs[run_id])
        self.runs = {}
        self.calls = []

    def assess(self, request, *, timeout_ms=None):
        case = self.repository.load_case(request.case_id, request.revision)
        graphs = project_case_graphs(case)
        context = ReasoningContext(
            request.kb_sha256, ENGINE_PROFILE_ID, case.case_id, case.revision,
            str(graphs.context.identifier), case.context,
        )
        report = AssessmentReport(
            str(uuid4()), context, "CONVERGED", "CONSISTENT", (), (), {}, "NOT_CLAIMED", (),
            run_at=datetime.now(UTC),
        )
        self.calls.append((case, timeout_ms))
        self.runs[report.run_id] = report, case
        return report


@pytest.fixture
def setup(tmp_path):
    canonical = ROOT / "knowledge/tradegraph.ttl"
    kb_file = tmp_path / "kb.ttl"
    kb_file.write_bytes(canonical.read_bytes() + b"\n" + Path(__file__).with_name("fixture.ttl").read_bytes())
    kb = compile_kb(kb_file, ENGINE_PROFILE_ID)
    repo = CaseRepository(tmp_path / "planner.sqlite3")
    context = AssessmentContext(kb.snapshot.sha256, ENGINE_PROFILE_ID, product_iri=str(DATA.item))

    def create(phase=FX.start):
        case = repo.create_case(f"<{DATA.item}> <{FX.phase}> <{phase}> .", context)
        assessment = RecordingAssessment(repo, kb)
        report = assessment.assess(SimpleNamespace(kb_sha256=kb.snapshot.sha256, case_id=case.case_id,
                                                   revision=case.revision))
        return repo, case, assessment, report, PlanningEngine(assessment)

    return create


def request(report, *, depth=5, states=20, actions=("lab-prepare", "lab-finish", "lab-cycle")):
    return PlanRequest(
        report.run_id, str(FX.goal), actions, PlanningLimits(max_states=states, max_depth=depth), "lab-cost",
    )


def test_missing_precondition_produces_no_candidate(setup):
    _, _, assessment, report, planner = setup(FX.blocked)
    assert planner.search_plans(request(report)) == []
    assert len(assessment.calls) == 1


def test_missing_dependency_blocks_an_otherwise_applicable_action(setup):
    _, _, assessment, report, planner = setup(FX.ready)
    assert planner.search_plans(request(report, actions=("lab-finish",))) == []
    assert len(assessment.calls) == 1


def test_cycles_are_deduplicated_and_unknown_cost_is_preserved(setup):
    repo, case, assessment, report, planner = setup()
    candidates = planner.search_plans(request(report))
    assert len(candidates) == 1
    full = candidates[0]
    assert [step.action_id for step in full.steps] == ["lab-prepare", "lab-finish"]
    assert full.steps[0].cost.value == Decimal("2.50")
    assert full.cost is None
    assert full.duration == Decimal("2.00")
    assert full.duration_unit_iri == str(FX.day)
    assert full.search_status == "COMPLETE"
    assert full.goal_reached and full.preconditions_hold
    assert full.predicted_findings == ()
    assert any((DATA.item, FX.phase, FX.done) in project_case_graphs(candidate).facts for candidate, _ in assessment.calls)
    assert len(assessment.calls) < 6
    assert repo.load_head(case.case_id) == case


@pytest.mark.parametrize(("depth", "states", "steps"), [(1, 10, 1), (5, 1, 0)])
def test_cutoff_returns_assessed_partial_candidate_precisely(setup, depth, states, steps):
    repo, case, assessment, report, planner = setup()
    candidate, = planner.search_plans(request(report, depth=depth, states=states))
    assert len(candidate.steps) == steps
    assert candidate.search_status == "TRUNCATED"
    assert not candidate.goal_reached
    assert candidate.optimality_claim is False
    assert candidate.preconditions_hold
    assert assessment.runs[candidate.assessment_run_id][0].case_revision == candidate.case_revision
    assert repo.load_head(case.case_id) == case


def test_grounding_consumes_only_typed_terms_already_in_the_case(setup):
    _, _, assessment, report, planner = setup()
    candidate, = planner.search_plans(request(report, actions=("lab-ground",)))
    assert candidate.goal_reached
    step, = candidate.steps
    assert step.bindings == (("subject", DATA.item),)
    assert len(assessment.calls) == 2


def test_literal_effects_use_persisted_rdf_canonicalization_for_deduplication(setup):
    _, _, assessment, report, planner = setup()
    assert planner.search_plans(request(report, actions=("lab-annotate",))) == []
    assert len(assessment.calls) == 2


def test_time_budget_passes_remaining_time_to_assessment(monkeypatch, setup):
    _, _, assessment, report, planner = setup()
    clock = [100.0]
    monkeypatch.setattr(engine, "monotonic", lambda: clock[0])
    assess = assessment.assess

    def elapsed(request, *, timeout_ms=None):
        evaluated = assess(request, timeout_ms=timeout_ms)
        clock[0] += 1
        return evaluated

    monkeypatch.setattr(assessment, "assess", elapsed)
    candidate, = planner.search_plans(PlanRequest(
        report.run_id, str(FX.goal), ("lab-prepare", "lab-finish"),
        PlanningLimits(timeout_ms=1_000), "lab-cost",
    ))
    assert candidate.search_status == "TRUNCATED"
    assert candidate.diagnostics == ("Planning time budget expired.",)
    assert not candidate.goal_reached
    assert assessment.calls[-1][1] == 1_000
