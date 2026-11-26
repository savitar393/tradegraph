"""Stage 1 automated tests: Temporary Import (8 KB policies + planning), RoO Diagnose + Repair, 2-Layer ProofGraph, and 8-Task Hub UI."""

from __future__ import annotations

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


def test_task_hub_ui_and_stage1_catalog_presets(client: TestClient) -> None:
    home = client.get("/")
    assert home.status_code == 200
    for marker in (
        "TradeGraph Compliance Workbench",
        "taskHubPane",
        "screeningBuilderPane",
        "rooBuilderPane",
        "tempImportBuilderPane",
        "docsStandalonePane",
        "renderProofExplorer",
        "/api/v1/temporary-import/evaluate",
        "/api/v1/roo/diagnose",
        "/api/v1/documents/templates",
    ):
        assert marker in home.text

    cat = client.get("/api/v1/catalog").json()
    assert len(cat["screening_presets"]) >= 4
    assert len(cat["roo_presets"]) >= 4
    assert len(cat["temporary_import_presets"]) >= 6


def test_temporary_import_8_policies_planning_and_proof_graph(client: TestClient) -> None:
    # 1. Exhibition pump fully satisfied + re-export timely -> ELIGIBLE & Art. 47 exemption NOT_APPLICABLE
    r_ok = client.post("/api/v1/temporary-import/evaluate", json={
        "hs_code": "8413.70.11",
        "dispatch_country": "TH",
        "destination_country": "ID",
        "as_of_date": "2025-09-15",
        "procedure": "reExportTemporaryImport",
        "intended_use": "exhibitionUse",
        "proposed_use": "exhibitionUse",
        "requested_settlement": "reexportSettlement",
        "identifiable_for_reexport": True,
        "consumed_in_use": False,
        "essential_change_in_use": False,
        "permit_end_date": "2025-10-15",
        "provided_docs": [
            "temporaryImportPermit",
            "temporaryImportGuarantee",
            "reexportPlan",
            "temporaryReexportNotice",
            "temporaryReexportRealization",
        ],
        "notice_date": "2025-10-10",
        "realization_date": "2025-11-02",
        "include_plan": True,
        "include_explain": True,
    })
    assert r_ok.status_code == 200
    d_ok = r_ok.json()
    assert d_ok["coverage_status"] == "PARTIAL"
    assert d_ok["overall_status"] == "ELIGIBLE"
    pmap = {p["policy_id"]: p["status"] for p in d_ok["policies"]}
    assert len(pmap) == 10
    assert pmap["temporary-goods-conditions"] == "SATISFIED"
    assert pmap["temporary-import-permit"] == "SATISFIED"
    assert pmap["temporary-import-guarantee"] == "SATISFIED"
    assert pmap["temporary-general-import-controls"] == "NOT_APPLICABLE"
    assert pmap["temporary-reexport-notice"] == "SATISFIED"
    assert pmap["temporary-reexport-realization"] == "SATISFIED"
    # 2-layer Proof SupportGraph populated
    assert len(d_ok["explanations"]) >= 4
    assert any(
        e["reaches_asserted_facts"] is True and len(e["support_nodes"]) >= 2 and len(e["support_edges"]) >= 1
        for e in d_ok["explanations"]
    )

    # 2. Missing re-export notice & realization -> ACTION_REQUIRED + 2-step KB plan
    r_plan = client.post("/api/v1/temporary-import/evaluate", json={
        "hs_code": "8413.70.11",
        "dispatch_country": "TH",
        "as_of_date": "2025-09-15",
        "procedure": "temporaryImport",
        "intended_use": "exhibitionUse",
        "requested_settlement": "reexportSettlement",
        "permit_end_date": "2025-10-15",
        "provided_docs": ["temporaryImportPermit", "temporaryImportGuarantee", "reexportPlan"],
        "include_plan": True,
        "include_explain": False,
    })
    assert r_plan.status_code == 200
    d_plan = r_plan.json()
    assert d_plan["overall_status"] == "ACTION_REQUIRED"
    assert [s["action_id"] for s in d_plan["plan_steps"]] == [
        "prepare-reexport-notice",
        "prepare-reexport-checklist",
    ]

    # 3. Ordinary commercial sale -> INELIGIBLE (temporary-domestic-settlement NOT_SATISFIED)
    r_sale = client.post("/api/v1/temporary-import/evaluate", json={
        "hs_code": "8413.70.11",
        "dispatch_country": "TH",
        "as_of_date": "2025-09-15",
        "procedure": "temporaryImport",
        "intended_use": "exhibitionUse",
        "proposed_use": "commercialResaleUse",
        "requested_settlement": "domesticSettlement",
        "settlement_ground": "ordinaryCommercialTransfer",
        "permit_end_date": "2025-10-15",
        "provided_docs": ["temporaryImportPermit", "temporaryImportGuarantee", "reexportPlan"],
        "include_plan": False,
        "include_explain": False,
    })
    assert r_sale.status_code == 200
    d_sale = r_sale.json()
    assert d_sale["overall_status"] == "INELIGIBLE"
    assert {p["policy_id"]: p["status"] for p in d_sale["policies"]}["temporary-domestic-settlement"] == "NOT_SATISFIED"

    # 4. Consumable goods -> INELIGIBLE (temporary-goods-conditions NOT_SATISFIED)
    r_cons = client.post("/api/v1/temporary-import/evaluate", json={
        "hs_code": "8413.70.11",
        "dispatch_country": "TH",
        "as_of_date": "2025-09-15",
        "consumed_in_use": True,
        "provided_docs": ["temporaryImportPermit", "temporaryImportGuarantee", "reexportPlan"],
        "include_plan": False,
        "include_explain": False,
    })
    assert r_cons.status_code == 200
    assert r_cons.json()["overall_status"] == "INELIGIBLE"
    assert {p["policy_id"]: p["status"] for p in r_cons.json()["policies"]}["temporary-goods-conditions"] == "NOT_SATISFIED"

    # 5. Late re-export notice & realization (> H+30) -> INELIGIBLE
    r_late = client.post("/api/v1/temporary-import/evaluate", json={
        "hs_code": "8413.70.11",
        "dispatch_country": "TH",
        "as_of_date": "2025-09-15",
        "procedure": "reExportTemporaryImport",
        "permit_end_date": "2025-10-15",
        "provided_docs": [
            "temporaryImportPermit",
            "temporaryImportGuarantee",
            "reexportPlan",
            "temporaryReexportNotice",
            "temporaryReexportRealization",
        ],
        "notice_date": "2025-10-20",
        "realization_date": "2025-11-30",
        "include_plan": False,
        "include_explain": False,
    })
    assert r_late.status_code == 200
    late_map = {p["policy_id"]: p["status"] for p in r_late.json()["policies"]}
    assert r_late.json()["overall_status"] == "INELIGIBLE"
    assert late_map["temporary-reexport-notice"] == "NOT_SATISFIED"
    assert late_map["temporary-reexport-realization"] == "NOT_SATISFIED"

    # 6. Outside verified KB date -> OUT_OF_COVERAGE (open-world honesty)
    r_out = client.post("/api/v1/temporary-import/evaluate", json={
        "hs_code": "8413.70.11",
        "dispatch_country": "TH",
        "as_of_date": "2025-09-16",
        "provided_docs": ["temporaryImportPermit", "temporaryImportGuarantee", "reexportPlan"],
        "include_plan": False,
        "include_explain": False,
    })
    assert r_out.status_code == 200
    assert r_out.json()["coverage_status"] == "OUT_OF_COVERAGE"
    assert r_out.json()["overall_status"] == "OUT_OF_COVERAGE"

    # 7. Prohibited goods (worn clothing 6309.00.00 in 2026) -> PROHIBITED hard-block
    r_proh = client.post("/api/v1/temporary-import/evaluate", json={
        "hs_code": "6309.00.00",
        "dispatch_country": "TH",
        "as_of_date": "2026-06-15",
        "include_plan": False,
        "include_explain": False,
    })
    assert r_proh.status_code == 200
    assert r_proh.json()["overall_status"] == "PROHIBITED"
    assert r_proh.json()["policies"] == []


def test_roo_diagnose_import_and_export_with_support_graph_and_repairs(client: TestClient) -> None:
    # 1. Import EV Battery KR -> ID at RVC 30%: IK-CEPA fails CTH/RVC40 -> repair suggests KR cumulation + RVC +10%, RCEP passes CTSH with SupportGraph
    r_imp = client.post("/api/v1/roo/diagnose", json={
        "direction": "import",
        "hs_code": "8507.60.33",
        "partner_country": "KR",
        "as_of_date": "2025-09-15",
        "materials": [{"hs_code": "8507.90.92", "non_originating": True, "origin_country": "CN", "origin_proof_verified": False}],
        "rvc_percent": "30.00",
        "include_explain": True,
    })
    assert r_imp.status_code == 200
    imp = r_imp.json()
    assert imp["direction"] == "import"
    assert imp["recommended_agreement"] == "rcep"
    schemes = {s["agreement"]: s for s in imp["schemes"]}
    assert schemes["rcep"]["origin_status"] == "SATISFIED"
    assert schemes["ikcepa"]["origin_status"] == "NOT_SATISFIED"
    assert any(r["agreement"] == "ikcepa" and r["kind"] == "CUMULATION_SOURCING" for r in imp["repair_suggestions"])
    assert any(r["agreement"] == "ikcepa" and r["kind"] == "RVC_INCREASE" for r in imp["repair_suggestions"])
    assert any(
        len(e.get("support_nodes", [])) >= 2 and len(e.get("support_edges", [])) >= 1 and e.get("policy_label")
        for e in schemes["rcep"]["explanations"]
    )

    # 2. Export EV Battery ID -> KR at RVC 30%: HermiT cumulation suggestions + SupportGraph
    r_exp = client.post("/api/v1/roo/diagnose", json={
        "direction": "export",
        "hs_code": "8507.60.33",
        "partner_country": "KR",
        "as_of_date": "2025-09-15",
        "materials": [{"hs_code": "8507.90.92", "non_originating": True, "origin_country": "CN", "origin_proof_verified": False}],
        "rvc_percent": "30.00",
        "include_explain": True,
    })
    assert r_exp.status_code == 200
    exp = r_exp.json()
    assert exp["direction"] == "export"
    assert exp["recommended_agreement"] == "rcep"
    assert len(exp["cumulation_opportunities"]) >= 1
    assert any(r["kind"] == "CUMULATION_SOURCING" for r in exp["repair_suggestions"])
    rcep_exp = next(s for s in exp["schemes"] if s["agreement"] == "rcep")
    assert any(len(e.get("support_nodes", [])) >= 2 for e in rcep_exp["explanations"])
