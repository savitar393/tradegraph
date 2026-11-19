from datetime import datetime
from pathlib import Path

import pytest
from rdflib import Literal, Namespace, URIRef, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext, AssessmentRequest, EvidenceClaim, FactPatch, PlanningLimits, PlanRequest
from tradegraph.graph.store import project_case_graphs
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb
from tradegraph.kb.rdf import parse_turtle
from tradegraph.planning.engine import PlanningEngine
from tradegraph.reasoning.client import ReasoningClient
from tradegraph.scenarios.service import ScenarioService


ROOT = Path(__file__).resolve().parents[2]
EX = Namespace("urn:tradegraph:test:scenario#")


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    directory = tmp_path_factory.mktemp("whatif")
    kb = compile_kb(ROOT / "knowledge/tradegraph.ttl", ENGINE_PROFILE_ID)
    classpath = ReasoningClient.build_classpath(ROOT / "semantic/pom.xml", directory / "classpath.txt")
    repo = CaseRepository(directory / "case.sqlite3")
    context = AssessmentContext(
        kb.snapshot.sha256, ENGINE_PROFILE_ID, jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
        procedure_iri=str(TG.temporaryImport), transaction_at=datetime.fromisoformat("2025-09-15T10:00:00+07:00"),
        product_iri=str(EX.pump), batch_iri=str(EX.batch), evidence_scope_id="permit",
    )
    case = repo.create_case("", context)
    revision = URIRef(f"urn:tradegraph:case:{case.case_id}:revision:2")
    facts = f"""
@prefix ex: <{EX}> .
@prefix tg: <{TG}> .
@prefix xsd: <{XSD}> .
<{revision}> tg:revisionShipment ex:shipment ;
    tg:intendedUse tg:exhibitionUse ; tg:requestedSettlement tg:reexportSettlement .
ex:pump tg:hasTariffClassification tg:hs-84137011 ; tg:hasBatch ex:batch ;
    tg:identifiableForReexport true ; tg:consumedInUse false ; tg:essentialChangeInUse false .
ex:batch tg:usesMaterial ex:material .
ex:shipment tg:containsGoods ex:pump ; tg:shipmentDispatchCountry tg:thailand ;
    tg:shipmentDestinationCountry tg:indonesia ; tg:cargoCategory tg:regularCargo .
ex:permit a tg:Document ; tg:documentType tg:temporaryImportPermit ; tg:documentSubject ex:pump ;
    tg:documentShipment ex:shipment ; tg:documentJurisdiction tg:jurisdictionIndonesiaCustoms ;
    tg:documentValidFrom "2025-09-01T00:00:00Z"^^xsd:dateTime ;
    tg:documentValidUntil "2025-10-01T00:00:00Z"^^xsd:dateTime .
"""
    claim = EvidenceClaim(str(EX.claim), str(EX.pump), str(TG.hasSupportingDocument), str(EX.permit),
                          EX.permit, verification_status="VERIFIED")
    case = repo.revise_case(case.case_id, case.revision, FactPatch(
        "Record exhibition import and permit", tuple(parse_turtle(facts.encode(), "urn:tradegraph:scenario:")),
        evidence_claims=(claim,),
    ))
    assessment = AssessmentService(repo, ReasoningClient(kb, classpath))
    report = assessment.assess(AssessmentRequest(kb.snapshot.sha256, case.case_id, case.revision, ("CQ01",)))
    assert report.execution_status == "CONVERGED", report.diagnostics
    assert report.coverage_status == "PARTIAL"
    return repo, case, report, ScenarioService(assessment), revision, assessment


def test_whatif_reassesses_two_branches_from_the_same_run(base):
    repo, original, report, scenarios, revision, _ = base
    remove = ((revision, TG.requestedSettlement, TG.reexportSettlement),)
    proposal = (revision, TG.requestedSettlement, TG.domesticSettlement)
    proposed = scenarios.evaluate_scenario(report.run_id, FactPatch("Seek domestic settlement", (proposal,), remove))
    sale = scenarios.evaluate_scenario(report.run_id, FactPatch("Ordinary sale as sole ground", (
        proposal, (revision, TG.settlementGround, TG.ordinaryCommercialTransfer),
        (revision, TG.settlementGroundsClosed, Literal(True)),
    ), remove))
    first = {change.key: change for change in proposed.finding_changes if change.kind == "POLICY"}
    second = {change.key: change for change in sale.finding_changes if change.kind == "POLICY"}
    assert first["temporary-domestic-settlement"].before == "NOT_APPLICABLE"
    assert first["temporary-domestic-settlement"].after == "UNDETERMINED"
    assert second["temporary-domestic-settlement"].after == "NOT_SATISFIED"
    assert proposed.fact_changes.added == (proposal,)
    assert proposed.fact_changes.removed == remove
    assert proposed.scenario_report.coverage_status == sale.scenario_report.coverage_status == "PARTIAL"
    assert proposed.scenario_report.query_results == report.query_results
    assert repo.load_head(original.case_id) == original
    for diff in (proposed, sale):
        branch = repo.load_case(original.case_id, diff.scenario_report.case_revision)
        assert branch.parent_revision == original.revision
        assert len(list(project_case_graphs(branch).facts.triples((None, TG.revisionShipment, EX.shipment)))) == 1


def test_canonical_planner_assesses_hypothetical_drafts_in_branch(base):
    repo, original, report, _, _, assessment = base
    candidates = PlanningEngine(assessment).search_plans(PlanRequest(
        report.run_id, str(TG["goal-reexport-preparation"]), (),
        PlanningLimits(max_states=8, max_depth=3, timeout_ms=60_000), "fewest-actions",
    ))
    candidate, = [item for item in candidates if item.goal_reached]
    assert [step.action_id for step in candidate.steps] == [
        "prepare-reexport-notice", "prepare-reexport-checklist",
    ]
    assert candidate.preconditions_hold
    assert candidate.cost is None
    final = repo.load_case(original.case_id, candidate.case_revision)
    assert (None, TG.hasDraftDocumentType, TG.idExportDeclaration) in project_case_graphs(final).facts
    assert (None, TG.hasDraftDocumentType, TG.idExportDeclaration) not in project_case_graphs(original).facts
    final_report, _ = assessment.provenance.load_run(candidate.assessment_run_id)
    actual = next(finding for finding in final_report.domain_findings if finding.policy_id == "temporary-reexport-notice")
    assert actual.status == "UNDETERMINED"
