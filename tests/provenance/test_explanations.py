from datetime import UTC, datetime
from pathlib import Path

import pytest
from rdflib import Literal, Namespace, RDF, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext, AssessmentRequest, EvidenceClaim, FactPatch
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb
from tradegraph.provenance.service import ProvenanceService
from tradegraph.reasoning.client import ReasoningClient


ROOT = Path(__file__).resolve().parents[2]
FX = Namespace("http://www.semanticweb.org/tradegraph/test/provenance#")
DIRECT_LINK = (FX.item, FX.directLink, FX.shipment)
DERIVED_LINK = (FX.item, FX.derivedLink, FX.shipment)
RULE_RESULT = (FX.item, RDF.type, FX.Reviewed)
FACTS = """
@prefix fx: <http://www.semanticweb.org/tradegraph/test/provenance#> .
@prefix tg: <http://www.semanticweb.org/tradegraph/kb#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
fx:item fx:directLink fx:shipment ; fx:amount 0.10 .
fx:other fx:amount 9.00 .
fx:document a tg:Document ; tg:documentSubject fx:item ;
    tg:documentType tg:temporaryImportPermit ; tg:documentJurisdiction tg:jurisdictionIndonesiaCustoms ;
    tg:documentValidFrom "2025-01-01T00:00:00Z"^^xsd:dateTime ;
    tg:documentValidUntil "2026-01-01T00:00:00Z"^^xsd:dateTime .
"""


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    temp = tmp_path_factory.mktemp("provenance")
    canonical = ROOT / "knowledge/tradegraph.ttl"
    fixture = Path(__file__).with_name("fixture.ttl")
    source = temp / "kb.ttl"
    source.write_bytes(canonical.read_bytes() + b"\n" + fixture.read_bytes())
    kb = compile_kb(source, ENGINE_PROFILE_ID)
    classpath = ReasoningClient.build_classpath(ROOT / "semantic/pom.xml", temp / "classpath.txt")
    client = ReasoningClient(kb, classpath)
    repo = CaseRepository(temp / "cases.sqlite3")
    case = repo.create_case(
        FACTS,
        AssessmentContext(
            kb.snapshot.sha256, ENGINE_PROFILE_ID, jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
            transaction_at=datetime(2025, 9, 15, tzinfo=UTC), product_iri=str(FX.item), evidence_scope_id="lab",
        ),
        evidence_claims=(EvidenceClaim(
            str(FX.claim), str(FX.item), str(TG.hasSupportingDocument), str(FX.document),
            FX.document, verification_status="VERIFIED",
        ),),
    )
    service = AssessmentService(repo, client)
    report = service.assess(AssessmentRequest(kb.snapshot.sha256, case.case_id, case.revision))
    assert report.execution_status == "CONVERGED", report.diagnostics
    return client, repo, case, service, report


def finding_id(report, triple):
    return next(finding.finding_id for finding in report.findings if finding.triple == triple)


def test_owl_justification_reaches_the_asserted_fact_and_source_snapshot(scenario):
    _, _, case, service, report = scenario
    target_id = finding_id(report, DERIVED_LINK)
    explanation = service.provenance.explain(target_id)
    assert explanation.availability == "AVAILABLE", explanation.diagnostics
    assert explanation.finding_id == target_id
    assert explanation.run_id == report.run_id
    assert explanation.kb_sha256 == case.context.kb_sha256
    assert explanation.case_revision == case.revision
    assert explanation.engine_profile_id == ENGINE_PROFILE_ID
    assert explanation.run_at == report.run_at
    assert explanation.support_graph.reaches_asserted_facts()
    assert any(node.kind == "ASSERTED_FACT" and node.fact == DIRECT_LINK for node in explanation.support_graph.nodes)
    assert any(node.reference_id == "lab-derived-link" for node in explanation.support_graph.nodes)
    assert [ref.source_record_id for ref in explanation.source_refs] == ["lab-provenance-v1"]
    assert explanation.source_refs[0].clause_iri == str(FX.clause)
    assert not any(node.fact and node.fact[0] == FX.other for node in explanation.support_graph.nodes)


def test_swrl_replays_bindings_and_links_its_owl_premise(scenario):
    _, _, _, service, report = scenario
    explanation = service.provenance.explain(finding_id(report, RULE_RESULT))
    assert explanation.availability == "AVAILABLE", explanation.diagnostics
    replay = next(node for node in explanation.support_graph.nodes if node.kind == "SWRL_RULE")
    assert replay.reference_id == "lab-reviewed-link"
    assert dict(replay.bindings) == {
        str(FX.x): FX.item, str(FX.y): FX.shipment, str(FX.n): Literal("0.10", datatype=XSD.decimal),
    }
    assert any(node.fact == DERIVED_LINK for node in explanation.support_graph.nodes)
    assert explanation.support_graph.reaches_asserted_facts()
    assert explanation.source_refs


def test_owl_after_drools_replays_earlier_rule_support(scenario):
    _, _, _, service, report = scenario
    explanation = service.provenance.explain(finding_id(report, (FX.item, RDF.type, FX.Ready)))
    assert explanation.availability == "AVAILABLE", explanation.diagnostics
    assert any(node.kind == "SWRL_RULE" for node in explanation.support_graph.nodes)
    assert explanation.support_graph.reaches_asserted_facts()


def test_validation_report_is_linked_to_scope_and_input_claim(scenario):
    _, _, _, service, report = scenario
    validation = report.validation_report
    assert validation.conforms
    finding = next(finding for finding in validation.evidence_findings if finding.focus_iri == str(FX.claim))
    explanation = service.provenance.explain(finding.finding_id)
    report_node = next(node for node in explanation.support_graph.nodes if node.kind == "VALIDATION_REPORT")
    assert report_node.reference_id == validation.report_id
    assert report_node.shape_id == finding.shape_id
    assert finding.shacl_report_turtle
    assert explanation.support_graph.reaches_asserted_facts()


def test_removing_asserted_support_changes_the_next_revision_only(scenario):
    _, repo, case, service, first = scenario
    second = repo.revise_case(
        case.case_id, case.revision, FactPatch("Remove named shipping link", remove_triples=(DIRECT_LINK,)),
    )
    report = service.assess(AssessmentRequest(case.context.kb_sha256, case.case_id, second.revision))
    assert report.execution_status == "CONVERGED", report.diagnostics
    assert RULE_RESULT not in {finding.triple for finding in report.findings}
    assert DERIVED_LINK not in {finding.triple for finding in report.findings}
    old = service.provenance.explain(finding_id(first, RULE_RESULT))
    assert old.availability == "AVAILABLE"
    assert old.case_revision == case.revision


def test_unavailable_support_has_no_fabricated_premises(scenario):
    client, _, case, _, report = scenario
    provenance = ProvenanceService(client, max_owl_checks=1)
    provenance.register(report, case)
    explanation = provenance.explain(finding_id(report, DERIVED_LINK))
    assert explanation.availability == "EXPLANATION_UNAVAILABLE"
    assert not explanation.support_graph.reaches_asserted_facts()
    assert not explanation.source_refs
    with pytest.raises(LookupError):
        provenance.explain("missing:999")
