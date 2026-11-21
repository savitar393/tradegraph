"""FastAPI HTTP API and Interactive Compliance Workbench tests."""

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


def test_workbench_html_health_catalog_and_regulations(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "TradeGraph Compliance Workbench" in home.text
    assert "/api/v1/optimize" in home.text
    assert "/api/v1/export/evaluate" in home.text
    assert "/api/v1/export-lartas" in home.text
    assert "/api/v1/export-duty" in home.text
    assert "/api/v1/batch/upload" in home.text
    assert "/api/v1/impact/upload" in home.text

    health = client.get("/api/v1/health")
    assert health.status_code == 200
    h = health.json()
    assert h["status"] == "ok"
    assert h["kb_version"] == "0.15.0"
    assert "import-lartas" in h["queries"]
    assert "export-lartas" in h["queries"]
    assert "export-duty" in h["queries"]
    assert "export-fta-options" in h["queries"]
    assert "prepare-lartas-permits" in h["actions"]
    assert h["persistence"] == {"sqlite_enabled": False, "fuseki_enabled": False}

    ready = client.get("/api/v1/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["kb"]["loaded"] is True
    assert ready.json()["java"]["available"] is True

    cat = client.get("/api/v1/catalog")
    assert cat.status_code == 200
    c = cat.json()
    assert c["kb_version"] == "0.15.0"
    hs_codes = {item["code"] for item in c["hs_codes"]}
    assert {
        "7208.10.00", "7209.26.10", "7210.12.10", "8422.11.00", "8507.60.33",
        "3004.10.15", "8413.70.11", "6309.00.00", "8001.10.00", "2604.00.00",
        "1511.10.00", "1801.00.10", "2603.00.00", "1301.90.90",
    } <= hs_codes
    agreements_by_slug = {a["slug"]: a for a in c["agreements"]}
    assert set(agreements_by_slug) == {"atiga", "rcep", "acfta", "ijepa", "ikcepa", "iacepa"}
    assert agreements_by_slug["atiga"]["back_to_back_allowed"] is True
    assert agreements_by_slug["rcep"]["back_to_back_allowed"] is True
    assert agreements_by_slug["acfta"]["back_to_back_allowed"] is True
    assert agreements_by_slug["ijepa"]["back_to_back_allowed"] is False
    assert agreements_by_slug["ikcepa"]["back_to_back_allowed"] is False
    assert agreements_by_slug["iacepa"]["back_to_back_allowed"] is False
    assert len(c["countries"]) >= 15
    assert {d["id"] for d in c["lartas_doc_types"]} == {
        "persetujuanImpor", "laporanSurveyor", "bpomSkiBorder", "bpomSkiPostBorder", "bpomIzinEdar",
    }
    assert {d["id"] for d in c["export_lartas_doc_types"]} == {
        "pebDeclaration", "eksportirTerdaftar", "persetujuanEkspor", "laporanSurveyorEkspor",
    }
    assert len(c["presets"]) == 7
    assert len(c["export_presets"]) == 6
    assert {"export", "import"} <= set(c["batch_samples"])
    assert len(c["impact_scenarios"]) == 3

    regs = client.get("/api/v1/regulations", params={"as_of_date": "2026-08-15"})
    assert regs.status_code == 200
    r = regs.json()
    assert r["active_count"] >= 26
    rel_kinds = {rel["kind"] for rel in r["legal_relations"]}
    assert "partiallyRevokesDocument" in rel_kinds
    assert any(rel["kind"] == "relRevokes" and rel["active_on_date"] is True for rel in r["legal_relations"])


def test_quick_lartas_endpoint_and_validation_errors(client):
    # PROHIBITED on 2026-06-15 (Permendag 47/2025)
    proh = client.get("/api/v1/lartas", params={"hs_code": "6309.00.00", "as_of_date": "2026-06-15", "explain": "true"})
    assert proh.status_code == 200
    p = proh.json()
    assert p["lartas"]["status"] == "PROHIBITED"
    assert p["lartas"]["prohibited"] is True
    assert any("47 Tahun 2025" in (c["regulation_number"] or "") for c in p["lartas"]["citations"])

    # CLEAR when laporanSurveyor is provided for 8413.70.11 in 2026
    clear = client.get("/api/v1/lartas", params={
        "hs_code": "8413.70.11",
        "as_of_date": "2026-06-15",
        "api_type": "API-U",
        "provided_docs": "laporanSurveyor",
    })
    assert clear.status_code == 200
    assert clear.json()["lartas"]["status"] == "CLEAR"

    # Unknown HS code -> clean 422
    bad_hs = client.get("/api/v1/lartas", params={"hs_code": "9999.99.99"})
    assert bad_hs.status_code == 422

    # Bad ISO date -> clean 422
    bad_dt = client.get("/api/v1/lartas", params={"hs_code": "7209.26.10", "as_of_date": "15-09-2025"})
    assert bad_dt.status_code == 422


def test_optimize_endpoint_with_four_step_plan_explain_and_prohibited_block(client):
    # 1. Cold-rolled steel 7209.26.10 (TH -> ID, JP cumulation, missing DV20 & PI+LS) -> RCEP + 4-step plan + provenance
    resp = client.post("/api/v1/optimize", json={
        "hs_code": "7209.26.10",
        "dispatch_country": "TH",
        "destination_country": "ID",
        "as_of_date": "2025-09-15",
        "cif_value": "100000",
        "currency": "USD",
        "has_api": True,
        "api_type": "API-P",
        "materials": [
            {"hs_code": "7208.54.10", "non_originating": True, "origin_country": "JP", "origin_proof_verified": True}
        ],
        "rvc_percent": "30.00",
        "highest_contributor_country": "japan",
        "agreements": ["rcep"],
        "include_plan": True,
        "include_explain": True,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["kb_version"] == "0.15.0"
    assert body["recommended_agreement"] == "rcep"
    assert body["lartas"]["status"] == "BORDER_BLOCKED"
    assert len(body["tax_profile_citations"]) >= 2

    rcep = body["schemes"][0]
    assert rcep["applied_duty_percent"] == "6.60"
    assert rcep["total_tax_savings"] == "9534.00"
    assert [s["action_id"] for s in rcep["plan_steps"]] == [
        "prepare-fta-origin-proof",
        "prepare-rcep-dv20-statement",
        "prepare-lartas-permits",
        "prepare-fta-pib-checklist",
    ]
    assert any(e["reaches_asserted_facts"] is True for e in rcep["explanations"])

    # 2. Prohibited 6309.00.00 hard-blocks FTA recommendation & suppresses planning
    proh = client.post("/api/v1/optimize", json={
        "hs_code": "6309.00.00",
        "dispatch_country": "TH",
        "destination_country": "ID",
        "as_of_date": "2026-06-15",
        "cif_value": "20000",
        "include_plan": True,
        "include_explain": True,
    })
    assert proh.status_code == 200
    pb = proh.json()
    assert pb["recommended_agreement"] == "PROHIBITED"
    assert all(s["eligibility"] == "PROHIBITED" and s["plan_steps"] == [] for s in pb["schemes"])


def test_assess_single_scheme_endpoint_and_errors(client):
    resp = client.post("/api/v1/assess/rcep", json={
        "hs_code": "8413.70.11",
        "dispatch_country": "CN",
        "destination_country": "ID",
        "as_of_date": "2026-06-15",
        "cif_value": "40000",
        "has_api": True,
        "api_type": "API-U",
        "materials": [{"hs_code": "8413.91.30", "non_originating": True}],
        "rvc_percent": "45.00",
        "provided_origin_docs": ["rcepFormRcep"],
        "provided_lartas_docs": ["laporanSurveyor"],
        "include_explain": True,
    })
    assert resp.status_code == 200
    ev = resp.json()
    assert ev["agreement"] == "rcep"
    assert ev["eligibility"] == "ELIGIBLE"
    assert ev["applied_duty_percent"] == "2.00"
    assert len(ev["citations"]) >= 1
    assert any(e["reaches_asserted_facts"] is True for e in ev["explanations"])

    # Inapplicable agreement (ATIGA for China -> ID) returns clean 422
    inapp = client.post("/api/v1/assess/atiga", json={
        "hs_code": "8413.70.11",
        "dispatch_country": "CN",
        "destination_country": "ID",
        "as_of_date": "2026-06-15",
    })
    assert inapp.status_code == 422


def test_export_lartas_and_export_evaluate_endpoints(client):
    # 1. GET /api/v1/export-lartas: Nickel Ore (2604.00.00) -> PROHIBITED_EXPORT (Permendag 8/2025)
    proh = client.get("/api/v1/export-lartas", params={"hs_code": "2604.00.00", "as_of_date": "2025-09-15", "explain": "true"})
    assert proh.status_code == 200
    pb = proh.json()
    assert pb["kb_version"] == "0.15.0"
    assert pb["export_lartas"]["status"] == "PROHIBITED_EXPORT"
    assert pb["export_lartas"]["prohibited"] is True
    assert any("8 Tahun 2025" in (c["regulation_number"] or "") for c in pb["export_lartas"]["citations"])

    # 2. GET /api/v1/export-lartas: Refined Tin (8001.10.00) transitions MISSING -> PEB_PENDING -> EXPORT_READY
    miss = client.get("/api/v1/export-lartas", params={"hs_code": "8001.10.00", "as_of_date": "2025-09-15"})
    assert miss.status_code == 200
    assert miss.json()["export_lartas"]["status"] == "EXPORT_PERMITS_MISSING"
    assert set(miss.json()["export_lartas"]["missing_docs"]) == {
        "eksportirTerdaftar", "persetujuanEkspor", "laporanSurveyorEkspor", "pebDeclaration",
    }

    peb_pend = client.get("/api/v1/export-lartas", params={
        "hs_code": "8001.10.00",
        "as_of_date": "2025-09-15",
        "provided_docs": "eksportirTerdaftar,persetujuanEkspor,laporanSurveyorEkspor",
    })
    assert peb_pend.status_code == 200
    assert peb_pend.json()["export_lartas"]["status"] == "PEB_PENDING"

    ready = client.get("/api/v1/export-lartas", params={
        "hs_code": "8001.10.00",
        "as_of_date": "2025-09-15",
        "provided_docs": "eksportirTerdaftar,persetujuanEkspor,laporanSurveyorEkspor,pebDeclaration",
    })
    assert ready.status_code == 200
    assert ready.json()["export_lartas"]["status"] == "EXPORT_READY"

    # Unknown HS code -> clean 422
    assert client.get("/api/v1/export-lartas", params={"hs_code": "9999.99.99"}).status_code == 422

    # 3. POST /api/v1/export/evaluate: Refined Tin ID -> JP via SG hub (Back-to-Back + HK TPI)
    exp = client.post("/api/v1/export/evaluate", json={
        "hs_code": "8001.10.00",
        "origin_country": "ID",
        "destination_country": "JP",
        "as_of_date": "2025-09-15",
        "fob_value": "250000",
        "rvc_percent": "65.00",
        "transshipment_hub": "SG",
        "requires_back_to_back": True,
        "third_party_invoice_country": "HK",
        "include_explain": True,
    })
    assert exp.status_code == 200
    eb = exp.json()
    assert eb["kb_version"] == "0.15.0"
    assert eb["recommended_agreement"] == "rcep"
    assert eb["export_lartas"]["status"] == "EXPORT_PERMITS_MISSING"
    by_agr = {s["agreement"]: s for s in eb["schemes"]}
    assert by_agr["rcep"]["eligibility"] == "ELIGIBLE"
    assert by_agr["rcep"]["back_to_back_status"] == "ALLOWED"
    assert "Box 14" in (by_agr["rcep"]["third_party_invoice_instruction"] or "")
    assert by_agr["ijepa"]["eligibility"] == "BLOCKED_NO_BACK_TO_BACK"
    assert by_agr["ijepa"]["back_to_back_status"] == "DISALLOWED_BILATERAL"
    assert [step["category"] for step in eb["checklist"]] == [
        "LARTAS", "LARTAS", "LARTAS", "CUSTOMS_PEB", "ORIGIN_PROOF", "TRANSSHIPMENT", "INVOICING",
    ]

    # 4. POST /api/v1/export/evaluate: EV Battery 8507.60.33 ID -> KR emits cumulation suggestion
    ev_resp = client.post("/api/v1/export/evaluate", json={
        "hs_code": "8507.60.33",
        "origin_country": "ID",
        "destination_country": "KR",
        "as_of_date": "2025-09-15",
        "materials": [{"hs_code": "8507.90.92", "non_originating": True, "origin_country": "CN", "origin_proof_verified": False}],
        "rvc_percent": "30.00",
    })
    assert ev_resp.status_code == 200
    ev = ev_resp.json()
    assert ev["recommended_agreement"] == "rcep"
    assert any(
        c["agreement"] == "ikcepa"
        and c["failing_material_hs"] == "8507.90.92"
        and "southKorea" in c["recommended_partner_countries"]
        for c in ev["cumulation_opportunities"]
    )


def test_export_duty_and_batch_screening_endpoints(client):
    import io
    import openpyxl

    # 1. GET /api/v1/export-duty: CPO specific tier & Cocoa temporal PMK 38/2024 vs PMK 68/2025
    cpo = client.get("/api/v1/export-duty", params={
        "hs_code": "1511.10.00",
        "as_of_date": "2025-11-15",
        "quantity_mt": "1000",
        "reference_price_usd": "950.00",
        "explain": "true",
    })
    assert cpo.status_code == 200
    cpo_d = cpo.json()["export_duty"]
    assert cpo_d["status"] == "APPLICABLE"
    assert cpo_d["tier_column"] == 7
    assert cpo_d["specific_rate_usd_per_mt"] == "124.00"
    assert cpo_d["estimated_duty_usd"] == "124000.00"

    cocoa_old = client.get("/api/v1/export-duty", params={
        "hs_code": "1801.00.10",
        "as_of_date": "2025-09-15",
        "quantity_mt": "50",
        "reference_price_usd": "4000",
        "export_price_usd_per_mt": "4000",
    })
    cocoa_new = client.get("/api/v1/export-duty", params={
        "hs_code": "1801.00.10",
        "as_of_date": "2025-11-15",
        "quantity_mt": "50",
        "reference_price_usd": "4000",
        "export_price_usd_per_mt": "4000",
    })
    assert cocoa_old.status_code == 200 and cocoa_old.json()["export_duty"]["rate_percent"] == "15.00"
    assert cocoa_new.status_code == 200 and cocoa_new.json()["export_duty"]["rate_percent"] == "7.50"

    assert client.get("/api/v1/export-duty", params={"hs_code": "9999.99.99"}).status_code == 422

    # 2. POST /api/v1/batch/screen (JSON + CSV + XLSX formats)
    payload = {
        "mode": "export",
        "include_explain": True,
        "items": [
            {"sku": "CPO-1", "hs_code": "1511.10.00", "destination_country": "CN", "as_of_date": "2025-11-15", "fob_value": "950000", "quantity_mt": "1000", "reference_price_usd": "950"},
            {"sku": "NI-2", "hs_code": "2604.00.00", "destination_country": "CN", "as_of_date": "2025-11-15", "fob_value": "500000"},
        ],
    }
    res_json = client.post("/api/v1/batch/screen", json=payload)
    assert res_json.status_code == 200
    body = res_json.json()
    assert body["kb_version"] == "0.15.0"
    assert body["summary"]["total_export_duty_usd"] == "124000.00"
    assert body["summary"]["prohibited_count"] == 1

    res_csv = client.post("/api/v1/batch/screen?format=csv", json=payload)
    assert res_csv.status_code == 200
    assert "text/csv" in res_csv.headers["content-type"]
    assert "124000.00" in res_csv.text

    res_xlsx = client.post("/api/v1/batch/screen?format=xlsx", json=payload)
    assert res_xlsx.status_code == 200
    assert "spreadsheetml" in res_xlsx.headers["content-type"]
    wb = openpyxl.load_workbook(io.BytesIO(res_xlsx.content), data_only=True)
    assert wb.sheetnames == ["Executive Summary", "Screening Results", "Legal Citations"]

    # 3. POST /api/v1/batch/upload with CSV file (import fast_lartas -> JSON + XLSX)
    imp_csv = (
        "sku,hs_code,dispatch_country,as_of_date,cif_value,api_type,provided_lartas_docs\n"
        "IMP-1,7209.26.10,AU,2025-09-15,100000,API-P,\n"
        "IMP-2,8413.70.11,CN,2026-06-15,40000,API-U,laporanSurveyor\n"
        "IMP-3,6309.00.00,TH,2026-06-15,20000,API-U,\n"
    ).encode("utf-8")
    up_json = client.post(
        "/api/v1/batch/upload?format=json",
        files={"file": ("import_manifest.csv", imp_csv, "text/csv")},
        data={"mode": "import", "screening_depth": "fast_lartas", "include_explain": "true"},
    )
    assert up_json.status_code == 200
    ub = up_json.json()
    assert ub["summary"]["total_rows"] == 3
    assert ub["summary"]["ready_count"] == 1
    assert ub["summary"]["blocked_count"] == 1
    assert ub["summary"]["prohibited_count"] == 1

    up_xlsx = client.post(
        "/api/v1/batch/upload?format=xlsx",
        files={"file": ("import_manifest.csv", imp_csv, "text/csv")},
        data={"mode": "import", "screening_depth": "fast_lartas", "include_explain": "true"},
    )
    assert up_xlsx.status_code == 200
    wb2 = openpyxl.load_workbook(io.BytesIO(up_xlsx.content), data_only=True)
    assert wb2["Screening Results"].max_row == 4


def test_regulatory_diff_and_impact_analyzer_endpoints(client):
    import io
    import openpyxl

    # 1. GET /api/v1/regulations/diff
    diff_resp = client.get("/api/v1/regulations/diff", params={"from_date": "2025-09-15", "to_date": "2025-11-15"})
    assert diff_resp.status_code == 200
    diff = diff_resp.json()
    assert diff["enacted_count"] >= 1
    assert any("68 Tahun 2025" in (e["acting_regulation_number"] or "") for e in diff["enacted_regulations"])
    assert any(e["change_type"] == "AMENDS" for e in diff["legal_events"])

    # Reversed dates -> clean 422
    assert client.get("/api/v1/regulations/diff", params={"from_date": "2025-11-15", "to_date": "2025-09-15"}).status_code == 422

    # 2. POST /api/v1/impact/analyze (JSON + CSV + XLSX)
    req_payload = {
        "mode": "export",
        "baseline_date": "2025-09-15",
        "target_date": "2025-11-15",
        "include_explain": True,
        "items": [
            {"sku": "COCOA-1", "hs_code": "1801.00.10", "destination_country": "MY", "fob_value": "200000", "quantity_mt": "50", "reference_price_usd": "4000", "export_price_usd_per_mt": "4000"},
            {"sku": "PINE-2", "hs_code": "1301.90.90", "destination_country": "SG", "fob_value": "100000"},
        ],
    }
    imp_json = client.post("/api/v1/impact/analyze", json=req_payload)
    assert imp_json.status_code == 200
    ib = imp_json.json()
    assert ib["summary"]["favorable_count"] == 1
    assert ib["summary"]["adverse_count"] == 1
    assert ib["summary"]["net_financial_impact_usd"] == "10000.00"

    imp_csv = client.post("/api/v1/impact/analyze?format=csv", json=req_payload)
    assert imp_csv.status_code == 200 and "-15000.00" in imp_csv.text and "25000.00" in imp_csv.text

    imp_xlsx = client.post("/api/v1/impact/analyze?format=xlsx", json=req_payload)
    assert imp_xlsx.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(imp_xlsx.content), data_only=True)
    assert wb.sheetnames == ["Executive Impact Summary", "Regulatory Changes", "SKU Impact Diff", "Legal Citations"]

    # 3. POST /api/v1/impact/upload (Import 2026 Permendag 47/2025 ban + RCEP staging)
    csv_bytes = (
        "sku,hs_code,dispatch_country,cif_value,api_type,agreements,provided_lartas_docs\n"
        "WORN-1,6309.00.00,TH,20000,API-U,,\n"
        "PUMP-2,8413.70.11,CN,100000,API-U,rcep,laporanSurveyor\n"
    ).encode("utf-8")
    up_json = client.post(
        "/api/v1/impact/upload?format=json",
        files={"file": ("imp_portfolio.csv", csv_bytes, "text/csv")},
        data={
            "mode": "import",
            "baseline_date": "2025-09-15",
            "target_date": "2026-06-15",
            "screening_depth": "fast_lartas",
            "include_explain": "true",
        },
    )
    assert up_json.status_code == 200
    ub = up_json.json()
    assert ub["summary"]["breaking_count"] == 1
    assert ub["summary"]["favorable_count"] == 1

    up_xlsx = client.post(
        "/api/v1/impact/upload?format=xlsx",
        files={"file": ("imp_portfolio.csv", csv_bytes, "text/csv")},
        data={
            "mode": "import",
            "baseline_date": "2025-09-15",
            "target_date": "2026-06-15",
            "screening_depth": "fast_lartas",
            "include_explain": "true",
        },
    )
    assert up_xlsx.status_code == 200
    wb2 = openpyxl.load_workbook(io.BytesIO(up_xlsx.content), data_only=True)
    assert wb2["SKU Impact Diff"].max_row == 3


def test_auth_and_admin_rbac(client) -> None:
    # 1. Anonymous /api/v1/auth/me and /api/v1/admin/overview are rejected with 401
    anon = client.get("/api/v1/auth/me")
    assert anon.status_code == 401
    unauth_admin = client.get("/api/v1/admin/overview")
    assert unauth_admin.status_code == 401

    # 2. Register a new regular customer user
    reg = client.post(
        "/api/v1/auth/register",
        json={
            "name": "Budi Santoso",
            "email": "ppjk.baru@logistik.co.id",
            "password": "PasswordKuat2026!",
            "company": "PT Logistik Samudera",
        },
    )
    assert reg.status_code == 200
    reg_body = reg.json()
    assert reg_body["user"]["role"] == "user"
    user_token = reg_body["token"]
    new_user_id = reg_body["user"]["id"]

    # 3. Authenticated /api/v1/auth/me returns the user profile
    user_headers = {"Authorization": f"Bearer {user_token}"}
    me = client.get("/api/v1/auth/me", headers=user_headers)
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "ppjk.baru@logistik.co.id"

    # 4. Regular user cannot access /api/v1/admin/overview or /api/v1/monitor/scan (403 Forbidden)
    forbid_admin = client.get("/api/v1/admin/overview", headers=user_headers)
    assert forbid_admin.status_code == 403
    forbid_scan = client.post("/api/v1/monitor/scan", json={}, headers=user_headers)
    assert forbid_scan.status_code == 403

    # 5. Demo login as admin
    admin_demo = client.post("/api/v1/auth/demo", json={"role": "admin"})
    assert admin_demo.status_code == 200
    admin_body = admin_demo.json()
    assert admin_body["user"]["role"] == "admin"
    admin_token = admin_body["token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # 6. Admin can access /api/v1/admin/overview and manage user roles
    ov = client.get("/api/v1/admin/overview", headers=admin_headers)
    assert ov.status_code == 200
    ov_body = ov.json()
    assert ov_body["kb_metrics"]["owl_triples"] > 1000
    assert len(ov_body["users"]) >= 3

    promote = client.post(
        f"/api/v1/admin/users/{new_user_id}/role",
        json={"role": "admin"},
        headers=admin_headers,
    )
    assert promote.status_code == 200
    assert promote.json()["user"]["role"] == "admin"

    # 7. Promoted user can now access /api/v1/admin/overview, then logout
    promoted_ov = client.get("/api/v1/admin/overview", headers=user_headers)
    assert promoted_ov.status_code == 200

    lg = client.post("/api/v1/auth/logout", headers=user_headers)
    assert lg.status_code == 200
    me_after = client.get("/api/v1/auth/me", headers=user_headers)
    assert me_after.status_code == 401

