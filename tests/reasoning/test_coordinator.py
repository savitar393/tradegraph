from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rdflib import Literal, Namespace, RDF, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import (
    AssessmentContext, AssessmentRequest, EvidenceClaim, FactPatch, ReasoningJob, ReasoningLimits,
)
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb
from tradegraph.reasoning.client import ReasoningClient


ROOT = Path(__file__).resolve().parents[2]
FX = Namespace("urn:tradegraph:test:feedback#")
PREFIXES = """
@prefix fx: <urn:tradegraph:test:feedback#> .
@prefix tg: <urn:tradegraph:kb#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
"""
FACTS = """
fx:product tg:hasBatch fx:batch ; owl:sameAs fx:alias .
fx:batch tg:usesMaterial fx:substance .
fx:alias fx:amount 0.10 .
"""


@pytest.fixture(scope="module")
def java_classpath(tmp_path_factory):
    return ReasoningClient.build_classpath(
        ROOT / "semantic" / "pom.xml", tmp_path_factory.mktemp("reasoning-build") / "classpath.txt",
    )


@pytest.fixture
def scenario(tmp_path, java_classpath):
    def create(*, facts=FACTS, fixture_transform=lambda source: source, **context_values):
        knowledge = tmp_path / "fixture.ttl"
        knowledge.write_text(
            (ROOT / "knowledge" / "tradegraph.ttl").read_text(encoding="utf-8") + "\n"
            + fixture_transform(Path(__file__).with_name("fixture.ttl").read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        kb = compile_kb(knowledge, ENGINE_PROFILE_ID)
        client = ReasoningClient(kb, java_classpath)
        repository = CaseRepository(tmp_path / "cases.sqlite3")
        context = AssessmentContext(
            kb.snapshot.sha256, ENGINE_PROFILE_ID, jurisdiction_iri=str(FX.country),
            transaction_at=datetime(2026, 1, 1, tzinfo=UTC), product_iri=str(FX["product"]),
        )
        case = repository.create_case(
            PREFIXES + facts, replace(context, **context_values),
            evidence_claims=(
                EvidenceClaim(
                    str(FX.claim), str(FX["product"]), str(FX.amount), str(FX.source),
                    Literal("0.9", datatype=XSD.decimal),
                    verification_status="VERIFIED",
                ),
            ),
        )
        return kb, client, repository, case
    return create


def run(client, case, **limits):
    return client.reason(ReasoningJob(case.context.kb_sha256, case, ("feedback",), ReasoningLimits(**limits)))


def test_reasoning_cycles_and_pins_context(scenario):
    kb, client, repository, case = scenario()
    repository.revise_case(case.case_id, case.revision, FactPatch(
        "Change amount in the next revision",
        remove_triples=((FX.alias, FX.amount, Literal("0.10", datatype=XSD.decimal, normalize=False)),),
    ))
    report = AssessmentService(repository, client).assess(
        AssessmentRequest(kb.snapshot.sha256, case.case_id, case.revision, ("feedback",))
    )
    assert report.execution_status == "CONVERGED", report.diagnostics
    assert report.consistency == "CONSISTENT"
    assert report.completeness_claim == "NOT_CLAIMED"
    assert report.context.kb_sha256 == kb.snapshot.sha256
    assert report.context.case_revision == case.revision == 1
    assert report.context.engine_profile_id == ENGINE_PROFILE_ID
    assert report.context.assessment_context_iri.endswith(":revision:1:context")
    assert report.validation_report.evidence_findings[0].status == "UNDETERMINED"
    assert report.query_results["feedback"] == ({"product": FX["product"]},)
    triples = {finding.triple: finding.support for finding in report.findings}
    assert len(triples) == len(report.findings)
    assert all(finding.support.context == report.context for finding in report.findings)
    material = triples[FX["product"], TG.dependsOnMaterial, FX.substance]
    feedback = triples[FX["product"], RDF.type, FX.RuleResult]
    final = triples[FX["product"], RDF.type, FX.derived_after_rule_feedback]
    assert (material.kind, feedback.kind, final.kind) == ("OWL_ENTAILED", "RULE_DERIVED", "OWL_ENTAILED")
    assert material.iteration == feedback.iteration < final.iteration
    assert triples[FX["product"], FX.copiedMaterial, FX.substance].kind == "RULE_DERIVED"
    amounts = [
        obj for subject, predicate, obj in triples if subject == FX["product"] and predicate == FX.copiedAmount
    ]
    assert len(amounts) == 1 and amounts[0].eq(Literal("0.10", datatype=XSD.decimal))
    assert triples[FX["product"], FX.copiedAmount, amounts[0]].kind == "RULE_DERIVED"
    assert not any(t[1].endswith("differentFrom") for t in triples)


@pytest.mark.parametrize(("options", "status", "consistent"), [
    ({"timeout_ms": 1}, "TIMEOUT", None),
    ({"max_iterations": 1}, "LIMIT_REACHED", "CONSISTENT"),
])
def test_incomplete_runs_withhold_findings(scenario, options, status, consistent):
    kb, client, repository, case = scenario()
    report = AssessmentService(repository, client, limits=ReasoningLimits(**options)).assess(
        AssessmentRequest(kb.snapshot.sha256, case.case_id, case.revision, ("feedback",))
    )
    assert report.execution_status == status, report.diagnostics
    assert report.consistency == consistent
    assert report.findings == ()
    assert report.query_results == {}
    assert report.diagnostics
    assert report.validation_report is None


@pytest.mark.parametrize("builtin", ["notEqual", "add"])
def test_rejects_builtin_before_publishing_results(scenario, builtin):
    _, client, _, case = scenario(
        fixture_transform=lambda source: source.replace("swrlb:greaterThanOrEqual", f"swrlb:{builtin}"),
    )
    result = run(client, case)
    assert result.execution_status == "UNSUPPORTED", result.diagnostics
    assert result.inferred_facts == ()
    assert result.query_results == {}
    assert "scoped-feedback" in " ".join(result.diagnostics)


def test_detects_inconsistency_after_rule_feedback_even_at_iteration_limit(scenario):
    _, client, _, case = scenario(facts=FACTS + "fx:product a fx:Block .")
    result = run(client, case, max_iterations=1)
    assert result.execution_status == "FAILED"
    assert result.consistency == "INCONSISTENT", result.diagnostics
    assert result.inferred_facts == ()
    assert result.query_results == {}


@pytest.mark.parametrize("variant", [
    "outside-jurisdiction", "before-start", "at-end", "missing-time", "evidence-only", "different-type",
])
def test_rule_scope_and_typed_inputs(scenario, variant):
    options = {
        "outside-jurisdiction": {"jurisdiction_iri": str(FX.other)},
        "before-start": {"transaction_at": datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)},
        "at-end": {"transaction_at": datetime(2026, 2, 1, tzinfo=UTC)},
        "missing-time": {"transaction_at": None},
        "evidence-only": {"facts": FACTS.replace("fx:alias fx:amount 0.10 .", "")},
        "different-type": {"facts": FACTS.replace("0.10", '"0.10"')},
    }
    _, client, _, case = scenario(**options[variant])
    result = run(client, case)
    if variant in {"missing-time", "different-type"}:
        assert result.execution_status == "UNSUPPORTED", result.diagnostics
        assert result.query_results == {}
    else:
        assert result.execution_status == "CONVERGED", result.diagnostics
        assert result.query_results["feedback"] == ()
        assert FX.RuleResult not in {fact[2] for fact in result.inferred_facts}


def test_refuses_mixed_snapshots(scenario):
    _, client, _, case = scenario()
    job = ReasoningJob("0" * 64, case, ())
    result = client.reason(job)
    assert result.execution_status == "FAILED"
    assert result.inferred_facts == ()
    assert result.query_results == {}


def test_canonical_kb_query_uses_owl_materialization(tmp_path, java_classpath):
    kb = compile_kb(ROOT / "knowledge" / "tradegraph.ttl", ENGINE_PROFILE_ID)
    repository = CaseRepository(tmp_path / "cases.sqlite3")
    context = AssessmentContext(
        kb.snapshot.sha256, ENGINE_PROFILE_ID, product_iri=str(FX["product"]),
        jurisdiction_iri=str(FX.country), transaction_at=datetime(2026, 1, 1, tzinfo=UTC),
        evidence_scope_id="document",
    )
    case = repository.create_case(
        PREFIXES + """
            fx:product tg:hasBatch fx:batch .
            fx:batch tg:usesMaterial fx:substance .
            fx:substance tg:suppliedBy fx:supplier .
            fx:document tg:documentSubject fx:product ;
                tg:documentType tg:temporaryImportPermit ; tg:documentJurisdiction fx:country ;
                tg:documentValidFrom "2025-01-01T00:00:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> ;
                tg:documentValidUntil "2027-01-01T00:00:00Z"^^<http://www.w3.org/2001/XMLSchema#dateTime> .
        """,
        context,
        evidence_claims=(EvidenceClaim(
            str(FX.supportingClaim), str(FX["product"]), str(TG.hasSupportingDocument),
            str(FX.document), FX.document, verification_status="VERIFIED",
        ),),
    )
    result = AssessmentService(repository, ReasoningClient(kb, java_classpath)).assess(
        AssessmentRequest(kb.snapshot.sha256, case.case_id, case.revision, ("CQ01",)),
    )
    assert result.execution_status == "CONVERGED", result.diagnostics
    assert result.query_results["CQ01"] == ({"material": FX.substance, "supplier": FX.supplier},)
    assert all(finding.kind == "OWL_ENTAILED" for finding in result.findings)
    assert (FX.document, RDF.type, TG.Document) in {finding.triple for finding in result.findings}
    assert result.validation_report.conforms, result.validation_report.findings
