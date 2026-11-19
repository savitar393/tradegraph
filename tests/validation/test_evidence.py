from dataclasses import replace
from datetime import UTC, datetime

import pytest
from rdflib import Namespace

from tradegraph.contracts import EvidenceClaim
from tradegraph.kb.loader import TG
from tradegraph.validation.service import validate_evidence

EX = Namespace("urn:tradegraph:test:validation#")
PREFIXES = """
@prefix ex: <urn:tradegraph:test:validation#> .
@prefix tg: <urn:tradegraph:kb#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
"""
CLAIM = EvidenceClaim(
    str(EX.claim), str(EX.product), str(TG.hasSupportingDocument), str(EX.document),
    EX.document, verification_status="VERIFIED",
)
DOCUMENT = """
ex:document a tg:Document ;
    tg:documentType tg:temporaryImportPermit ;
    tg:documentSubject ex:product ;
    tg:documentJurisdiction ex:jurisdiction ;
    tg:documentValidFrom "2026-01-01T00:00:00Z"^^xsd:dateTime ;
    tg:documentValidUntil "2026-02-01T00:00:00Z"^^xsd:dateTime .
"""


def test_validates_projected_document_and_records_scope(kb, case_factory):
    case = case_factory(claims=(CLAIM,), closed=True)
    report = validate_evidence(kb, case, PREFIXES + DOCUMENT)
    assert report.conforms, report.findings
    assert report.kb_sha256 == kb.snapshot.sha256
    assert report.case_revision == case.revision
    assert report.scope_id == "documents"
    assert report.evidence_scope_closed is True
    assert len(report.evidence_findings) == 1
    finding = report.evidence_findings[0]
    assert finding.kind == "VALIDATION"
    assert finding.status == "SATISFIED"
    assert finding.focus_iri == str(EX.claim)
    assert finding.shape_id in report.shape_ids


@pytest.mark.parametrize("closed", [None, True])
def test_missing_document_is_undetermined_even_under_closed_evidence_scope(kb, case_factory, closed):
    case = case_factory(closed=closed)
    report = validate_evidence(kb, case, case.facts_turtle)
    assert report.conforms is False
    assert report.evidence_scope_closed is closed
    assert report.evidence_findings[0].status == "UNDETERMINED"


@pytest.mark.parametrize(("facts", "claim", "overrides"), [
    (DOCUMENT, replace(CLAIM, subject_iri=str(EX.otherProduct)), {}),
    (DOCUMENT.replace("tg:documentSubject ex:product", "tg:documentSubject ex:otherProduct"), CLAIM, {}),
    (DOCUMENT.replace("tg:documentJurisdiction ex:jurisdiction", "tg:documentJurisdiction ex:other"), CLAIM, {}),
    (DOCUMENT, CLAIM, {"transaction_at": datetime(2026, 2, 1, tzinfo=UTC)}),
    (DOCUMENT, CLAIM, {"transaction_at": datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)}),
    (DOCUMENT, replace(CLAIM, verification_status="UNVERIFIED"), {}),
    ("", CLAIM, {}),
    (DOCUMENT, CLAIM, {"evidence_scope_id": None}),
    (DOCUMENT, CLAIM, {"transaction_at": None}),
])
def test_rejects_wrong_scope_time_or_incomplete_claim(kb, case_factory, facts, claim, overrides):
    case = case_factory(facts, claims=(claim,), **overrides)
    report = validate_evidence(kb, case, case.facts_turtle)
    assert report.conforms is False
    assert report.findings
    assert report.evidence_findings[0].status == "UNDETERMINED"


def test_cannot_supply_verification_status_through_fact_projection(kb, case_factory):
    case = case_factory(DOCUMENT, claims=(replace(CLAIM, verification_status="UNVERIFIED"),))
    with pytest.raises(ValueError):
        validate_evidence(kb, case, PREFIXES + """
            ex:claim <urn:tradegraph:case-data#verificationStatus> "VERIFIED" .
        """)


def test_rejects_a_different_kb_snapshot(kb, case_factory):
    case = case_factory(kb_sha256="0" * 64)
    with pytest.raises(ValueError, match="snapshot"):
        validate_evidence(kb, case, case.facts_turtle)
