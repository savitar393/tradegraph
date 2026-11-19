from decimal import Decimal

import pytest
from rdflib import RDF, SH

from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext
from tradegraph.kb.loader import SWRL, TG
from tradegraph.kb.validation import validate_owl
from tradegraph.validation.service import ValidationService

from .conftest import CASES, EX, PREFIXES


@pytest.mark.parametrize("case_spec", CASES, ids=lambda spec: spec["id"])
def test_source_backed_assessments(case_spec, run_case):
    report = run_case(case_spec)
    assert report.execution_status == "CONVERGED", report.diagnostics
    assert report.consistency == "CONSISTENT"
    assert report.completeness_claim == "NOT_CLAIMED"
    assert report.coverage_status == case_spec.get("expected_coverage", "PARTIAL")
    findings = {finding.policy_id: finding for finding in report.domain_findings}
    expected = case_spec["expected"]
    if not expected:
        assert report.domain_findings == ()
    assert {key: findings[key].status for key in expected} == expected
    assert all(finding.source_clauses for finding in report.domain_findings)
    cited = {clause for finding in report.domain_findings if finding.policy_id in expected
             for clause in finding.source_clauses}
    if expected:
        assert {str(TG[clause]) for clause in case_spec["source_clauses"]} <= cited


def test_one_assessment_follows_production_shipping_issuer_and_party(run_case):
    spec = next(case for case in CASES if case["id"] == "pump-rvc-boundary")
    report, provenance = run_case(spec, include_provenance=True)
    assert report.query_results["CQ01"] == ({"material": EX.material, "supplier": EX.supplier},)
    findings = {finding.policy_id: finding for finding in report.domain_findings}
    assert findings["atiga-rvc"].evidence == (str(EX.worksheet),)
    assert findings["atiga-consignment"].evidence == (str(EX.transport),)
    assert findings["atiga-origin-evidence"].evidence == (str(EX.formd),)
    assert findings["importer-api-u"].responsible_parties == (str(EX.importer),)
    assert any(
        finding.triple == (EX.worksheet, RDF.type, TG.PumpRvcThresholdMet)
        and finding.kind == "RULE_DERIVED"
        for finding in report.findings
    )
    explanation = provenance.explain(findings["atiga-rvc"].finding_id)
    assert explanation.availability == "PARTIAL"
    assert explanation.support_graph.reaches_asserted_facts()
    assert any(ref.clause_iri == str(TG["psr-row-2042"]) and ref.source_record_id.startswith("source-")
               for ref in explanation.source_refs)
    assert {"DECISION_QUERY", "VALIDATION_REPORT", "SWRL_RULE"} <= {
        node.kind for node in explanation.support_graph.nodes
    }


def test_canonical_projections_validate_on_pinned_runtime(compiled):
    owl = compiled.dataset.graph(compiled.owl_projection_ref)
    shapes = compiled.dataset.graph(compiled.shapes_graph_ref)
    assert len(compiled.rule_atom_counts) >= 2
    assert not list(owl.subjects(RDF.type, SWRL.Imp))
    assert not list(owl.subjects(RDF.type, SH.NodeShape))
    assert list(shapes.subjects(RDF.type, SH.NodeShape))
    report = validate_owl(compiled)
    assert report.conforms, report.findings


def test_indirect_rvc_uses_exact_values_from_a_closed_batch_worksheet(compiled, tmp_path):
    context = AssessmentContext(
        compiled.snapshot.sha256, compiled.snapshot.engine_profile_id, batch_iri=str(EX.batch),
    )
    repo = CaseRepository(tmp_path / "valuation.sqlite3")
    case = repo.create_case(PREFIXES + """
ex:worksheet a tg:Document ; tg:documentBatch ex:batch ;
    tg:valuationScopeClosed true ; tg:fobValue 75.00 ; tg:nonOriginatingMaterialValue 45.00 ;
    tg:valuationCurrency ex:thb .
""", context)
    service = ValidationService(compiled)
    result = service.evaluate_calculation("atiga-rvc-indirect", {"worksheet": EX.worksheet}, case)
    assert result.calculation_available, result.diagnostics
    assert result.value == Decimal("40")
    assert result.unit_iri == str(TG.percentUnit)
    assert result.closure.confirmed


def test_case_cannot_modify_the_compiled_rvc_threshold(run_case):
    spec = {"scenario": "pump", "variant": "invalid-policy-edit", "rvc": "50.00"}
    result = run_case(spec)
    assert result.execution_status == "UNSUPPORTED"
    assert result.coverage_status is None
    assert result.domain_findings == ()
