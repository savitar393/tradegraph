"""Stage 2 automated tests: Interactive HS Classifier, Counterfactual BOM & Sourcing Optimizer, and Multi-Regime Customs Configurator."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from tradegraph.api import create_app
from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.optimization import MultiFtaOptimizer


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"


@pytest.fixture(scope="module")
def client():
    kb = compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)
    optimizer = MultiFtaOptimizer(kb, resolve_runtime_classpath())
    app = create_app(optimizer)
    with TestClient(app) as tc:
        yield tc


def test_hs_classifier_kb_facets_classification_and_disambiguation(client: TestClient) -> None:
    facets_resp = client.get("/api/v1/hs-classifier/facets")
    assert facets_resp.status_code == 200
    f = facets_resp.json()
    assert f["total_profiles"] == 32
    assert {"sector", "material_group", "processing_state", "intended_application"} == set(f["facets"])

    # 1. Cold-rolled steel facet narrows straight to 7209.26.10 at 100%
    r_crc = client.post("/api/v1/hs-classifier/classify", json={
        "query_text": "baja gulungan",
        "sector": "steel",
        "processing_state": "cold_rolled",
    })
    assert r_crc.status_code == 200
    c_crc = r_crc.json()["candidates"]
    assert c_crc[0]["hs_code"] == "7209.26.10"
    assert c_crc[0]["confidence_percent"] == 100.0
    assert c_crc[0]["compliance_preview"]["import_lartas_status"] == "BORDER_BLOCKED"

    # 2. Broad lithium battery query triggers interactive disambiguation questions (EV vs Non-EV vs Separator)
    r_bat = client.post("/api/v1/hs-classifier/classify", json={
        "query_text": "baterai lithium",
        "sector": "electronics",
    })
    assert r_bat.status_code == 200
    bat = r_bat.json()
    top_codes = {c["hs_code"] for c in bat["candidates"][:4]}
    assert {"8507.60.33", "8507.60.90"} <= top_codes
    assert len(bat["disambiguation_questions"]) >= 1

    # 3. Secondhand worn clothing in 2026 previews import_prohibited=True
    r_worn = client.post("/api/v1/hs-classifier/classify", json={
        "query_text": "pakaian bekas",
        "sector": "textile",
        "processing_state": "used_secondhand",
        "as_of_date": "2026-06-15",
    })
    assert r_worn.status_code == 200
    assert r_worn.json()["candidates"][0]["hs_code"] == "6309.00.00"
    assert r_worn.json()["candidates"][0]["compliance_preview"]["import_prohibited"] is True


def test_counterfactual_bom_and_sourcing_optimizer_real_hermit_savings(client: TestClient) -> None:
    # 1. EV Battery KR -> ID at RVC 30%: baseline is RCEP 6%, counterfactuals unlock IK-CEPA 0% with verified USD savings!
    r_ev = client.post("/api/v1/counterfactual/optimize", json={
        "direction": "import",
        "hs_code": "8507.60.33",
        "partner_country": "KR",
        "as_of_date": "2025-09-15",
        "cif_or_fob_value": "100000",
        "api_type": "API-P",
        "materials": [{"hs_code": "8507.90.92", "non_originating": True, "origin_country": "CN", "origin_proof_verified": False}],
        "rvc_percent": "30.00",
    })
    assert r_ev.status_code == 200
    ev = r_ev.json()
    assert ev["blocked"] is False
    assert ev["baseline"]["recommended_agreement"] == "rcep"
    assert ev["baseline"]["applied_duty_percent"] == "6.00"
    cfs = ev["counterfactuals"]
    assert len(cfs) >= 2
    unlocked = [c for c in cfs if c["unlock_succeeded"] and c["target_agreement"] == "ikcepa"]
    assert len(unlocked) >= 2
    assert {c["kind"] for c in unlocked} >= {"CUMULATION_SWITCH", "RVC_THRESHOLD_UPGRADE"}
    for c in unlocked:
        assert c["verified_by_reasoner"] is True
        assert c["new_duty_percent"] == "0.00"
        assert Decimal(c["net_additional_savings"]) > Decimal("6000.00")

    # 2. Export Refined Tin ID -> JP via SG Back-to-Back: counterfactual unlocks direct IJEPA route
    r_exp = client.post("/api/v1/counterfactual/optimize", json={
        "direction": "export",
        "hs_code": "8001.10.00",
        "partner_country": "JP",
        "as_of_date": "2025-09-15",
        "cif_or_fob_value": "250000",
        "rvc_percent": "65.00",
        "transshipment_hub": "SG",
        "requires_back_to_back": True,
        "third_party_invoice_country": "HK",
    })
    assert r_exp.status_code == 200
    exp = r_exp.json()
    assert exp["blocked"] is False
    assert any(
        c["kind"] == "DIRECT_CONSIGNMENT_SWITCH" and c["target_agreement"] == "ijepa" and c["unlock_succeeded"]
        for c in exp["counterfactuals"]
    )

    # 3. Prohibited goods are hard-blocked
    r_proh = client.post("/api/v1/counterfactual/optimize", json={
        "direction": "import",
        "hs_code": "6309.00.00",
        "partner_country": "TH",
        "as_of_date": "2026-06-15",
        "cif_or_fob_value": "20000",
    })
    assert r_proh.status_code == 200
    assert r_proh.json()["blocked"] is True
    assert r_proh.json()["counterfactuals"] == []


def test_multi_regime_customs_configurator(client: TestClient) -> None:
    # 1. Exhibition pump with intact physical condition & same-condition re-export -> recommends temporary-import
    r_temp = client.post("/api/v1/regimes/recommend", json={
        "hs_code": "8413.70.11",
        "as_of_date": "2025-09-15",
        "direction": "import",
        "trade_purpose": "exhibition_event",
        "reexport_plan": "SAME_CONDITION_REEXPORT",
        "company_license": "API-U",
        "identifiable_for_reexport": True,
        "consumed_in_use": False,
        "essential_change_in_use": False,
        "customs_value_usd": "40000",
    })
    assert r_temp.status_code == 200
    t_body = r_temp.json()
    assert t_body["recommended_regime"] == "temporary-import"
    assert len(t_body["regimes"]) == 5
    top = t_body["regimes"][0]
    assert top["suitability"] == "RECOMMENDED"
    assert Decimal(top["estimated_fiscal_relief_usd"]) > Decimal("0")

    # 2. Steel raw material for export manufacturing with KITE license -> recommends kite-pembebasan
    r_kite = client.post("/api/v1/regimes/recommend", json={
        "hs_code": "7209.26.10",
        "as_of_date": "2025-09-15",
        "direction": "import",
        "trade_purpose": "manufacturing_for_export",
        "reexport_plan": "PROCESSED_EXPORT",
        "company_license": "KITE_HOLDER",
        "customs_value_usd": "100000",
    })
    assert r_kite.status_code == 200
    assert r_kite.json()["recommended_regime"] == "kite-pembebasan"

    # 3. Bonded Zone operator -> recommends kawasan-berikat
    r_kb = client.post("/api/v1/regimes/recommend", json={
        "hs_code": "8507.90.92",
        "as_of_date": "2025-09-15",
        "direction": "import",
        "trade_purpose": "industrial_bonded_zone",
        "reexport_plan": "PROCESSED_EXPORT",
        "company_license": "KB_OPERATOR",
        "customs_value_usd": "150000",
    })
    assert r_kb.status_code == 200
    assert r_kb.json()["recommended_regime"] == "kawasan-berikat"

    # 4. Prohibited worn clothing -> blocks all regimes
    r_proh = client.post("/api/v1/regimes/recommend", json={
        "hs_code": "6309.00.00",
        "as_of_date": "2026-06-15",
        "direction": "import",
        "trade_purpose": "exhibition_event",
        "reexport_plan": "SAME_CONDITION_REEXPORT",
        "company_license": "API-U",
    })
    assert r_proh.status_code == 200
    assert r_proh.json()["prohibited"] is True
    assert r_proh.json()["recommended_regime"] == "PROHIBITED"
    assert all(r["suitability"] == "INELIGIBLE" for r in r_proh.json()["regimes"])
