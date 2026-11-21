from datetime import UTC, datetime
from pathlib import Path

import pytest
from rdflib import Namespace

from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext
from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb


KB = Path(__file__).resolve().parents[2] / "knowledge" / "tradegraph.ttl"
EX = Namespace("http://www.semanticweb.org/tradegraph/test/validation#")
PREFIXES = """
@prefix ex: <http://www.semanticweb.org/tradegraph/test/validation#> .
@prefix tg: <http://www.semanticweb.org/tradegraph/kb#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
"""


@pytest.fixture
def kb():
    return compile_kb(KB, ENGINE_PROFILE_ID)


@pytest.fixture
def case_factory(tmp_path, kb):
    repository = CaseRepository(tmp_path / "cases.db")

    def create(facts="", *, claims=(), closed=None, **overrides):
        context = {
            "kb_sha256": kb.snapshot.sha256,
            "engine_profile_id": ENGINE_PROFILE_ID,
            "jurisdiction_iri": str(EX.jurisdiction),
            "transaction_at": datetime(2026, 1, 1, tzinfo=UTC),
            "evidence_scope_id": "documents",
            "product_iri": str(EX.product),
            "batch_iri": str(EX.batch),
        }
        context.update(overrides)
        return repository.create_case(
            PREFIXES + facts, AssessmentContext(**context), evidence_claims=claims, evidence_scope_closed=closed,
        )

    return create
