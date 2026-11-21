"""End-to-end Indonesian Export Duty (Bea Keluar PMK 38/2024 jo. PMK 68/2025) and Multi-SKU Batch Screening + Excel/CSV Audit Report tests (v0.15.0)."""

import csv
from decimal import Decimal
import io
import json
from pathlib import Path

import openpyxl
import pytest

from tradegraph.cli import main as cli_main
from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.optimization import (
    BatchScreener,
    ExportComplianceAdvisor,
    ExportSpec,
    MultiFtaOptimizer,
    load_batch_rows_from_csv,
    load_batch_rows_from_xlsx,
)


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"


@pytest.fixture(scope="module")
def services():
    kb = compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)
    optimizer = MultiFtaOptimizer(kb, resolve_runtime_classpath())
    advisor = ExportComplianceAdvisor(optimizer)
    screener = BatchScreener(optimizer, advisor)
    return optimizer, advisor, screener


def test_export_duty_specific_tiered_cpo_and_rbd_palm_olein(services):
    _, advisor, _ = services

    # 1. CPO (1511.10.00) missing Harga Referensi -> REFERENCE_PRICE_REQUIRED
    no_hr = advisor.evaluate(ExportSpec(
        hs_code="1511.10.00",
        destination_country="CN",
        as_of_date="2025-11-15",
        quantity_mt=Decimal("1000"),
        wo_confirmed=True,
    ))
    assert no_hr.kb_version == "0.15.0"
    assert no_hr.export_duty.status == "REFERENCE_PRICE_REQUIRED"
    assert no_hr.export_duty.subject_to_export_duty is True
    assert any(st.category == "CUSTOMS_EXPORT_DUTY" for st in no_hr.checklist)

    # 2. CPO (1511.10.00) at HR USD 650/MT (<= 680, Col 1) -> ZERO_RATE (0 USD/MT)
    low_hr = advisor.evaluate(ExportSpec(
        hs_code="1511.10.00",
        destination_country="CN",
        as_of_date="2025-11-15",
        quantity_mt=Decimal("1000"),
        reference_price_usd=Decimal("650.00"),
        wo_confirmed=True,
    ))
    assert low_hr.export_duty.status == "ZERO_RATE"
    assert low_hr.export_duty.tier_column == 1
    assert low_hr.export_duty.specific_rate_usd_per_mt == Decimal("0.00")
    assert low_hr.export_duty.estimated_duty_usd == Decimal("0.00")

    # 3. CPO (1511.10.00) at HR USD 950/MT (930 < HR <= 980, Col 7) -> USD 124/MT x 1,000 MT = USD 124,000.00
    mid_hr = advisor.evaluate(ExportSpec(
        hs_code="1511.10.00",
        destination_country="CN",
        as_of_date="2025-11-15",
        quantity_mt=Decimal("1000"),
        reference_price_usd=Decimal("950.00"),
        wo_confirmed=True,
        include_explain=True,
    ))
    assert mid_hr.export_duty.status == "APPLICABLE"
    assert mid_hr.export_duty.duty_basis == "SPECIFIC_USD_PER_MT"
    assert mid_hr.export_duty.tier_column == 7
    assert mid_hr.export_duty.specific_rate_usd_per_mt == Decimal("124.00")
    assert mid_hr.export_duty.estimated_duty_usd == Decimal("124000.00")
    assert any("68 Tahun 2025" in (c.regulation_number or "") for c in mid_hr.export_duty.citations)
    assert any("38 Tahun 2024" in (c.regulation_number or "") for c in mid_hr.export_duty.citations)

    # 4. RBD Palm Olein (1511.90.36) at same HR USD 950/MT (Col 7) -> USD 71/MT (lower downstream rate than CPO 124)
    rbd = advisor.evaluate(ExportSpec(
        hs_code="1511.90.36",
        destination_country="CN",
        as_of_date="2025-11-15",
        quantity_mt=Decimal("1000"),
        reference_price_usd=Decimal("950.00"),
        rvc_percent=Decimal("90.00"),
    ))
    assert rbd.export_duty.status == "APPLICABLE"
    assert rbd.export_duty.tier_column == 7
    assert rbd.export_duty.specific_rate_usd_per_mt == Decimal("71.00")
    assert rbd.export_duty.estimated_duty_usd == Decimal("71000.00")

    # 5. Top CPO bracket (> USD 1,430/MT, Col 17) -> USD 288/MT
    top_hr = advisor.evaluate(ExportSpec(
        hs_code="1511.10.00",
        destination_country="CN",
        as_of_date="2025-11-15",
        quantity_mt=Decimal("100"),
        reference_price_usd=Decimal("1500.00"),
    ))
    assert top_hr.export_duty.tier_column == 17
    assert top_hr.export_duty.specific_rate_usd_per_mt == Decimal("288.00")
    assert top_hr.export_duty.estimated_duty_usd == Decimal("28800.00")


def test_export_duty_temporal_amendment_cocoa_pine_resin_and_copper_lartas(services):
    _, advisor, _ = services

    # 1. Cocoa Beans (1801.00.10) at HR USD 4,000/MT (Col 4, 50 MT @ HPE USD 4,000/MT = USD 200,000):
    #    - 2025-09-15 (PMK 38/2024): 15.00% -> USD 30,000.00
    #    - 2025-11-15 (PMK 68/2025, eff. 2025-10-22): 7.50% -> USD 15,000.00 (50% cut!)
    cocoa_before = advisor.evaluate(ExportSpec(
        hs_code="1801.00.10",
        destination_country="MY",
        as_of_date="2025-09-15",
        quantity_mt=Decimal("50"),
        reference_price_usd=Decimal("4000.00"),
        export_price_usd_per_mt=Decimal("4000.00"),
        wo_confirmed=True,
        include_explain=True,
    ))
    assert cocoa_before.export_duty.status == "APPLICABLE"
    assert cocoa_before.export_duty.tier_column == 4
    assert cocoa_before.export_duty.rate_percent == Decimal("15.00")
    assert cocoa_before.export_duty.estimated_duty_usd == Decimal("30000.00")
    assert any("38 Tahun 2024" in (c.regulation_number or "") for c in cocoa_before.export_duty.citations)
    assert not any("68 Tahun 2025" in (c.regulation_number or "") for c in cocoa_before.export_duty.citations)

    cocoa_after = advisor.evaluate(ExportSpec(
        hs_code="1801.00.10",
        destination_country="MY",
        as_of_date="2025-11-15",
        quantity_mt=Decimal("50"),
        reference_price_usd=Decimal("4000.00"),
        export_price_usd_per_mt=Decimal("4000.00"),
        wo_confirmed=True,
        include_explain=True,
    ))
    assert cocoa_after.export_duty.rate_percent == Decimal("7.50")
    assert cocoa_after.export_duty.estimated_duty_usd == Decimal("15000.00")
    assert any("68 Tahun 2025" in (c.regulation_number or "") for c in cocoa_after.export_duty.citations)

    # 2. Getah Pinus (1301.90.90 — NEW Lampiran G in PMK 68/2025 eff. 2025-10-22):
    #    - 2025-09-15: NOT_SUBJECT (0.00)
    #    - 2025-11-15: 25.00% flat ad valorem
    pine_before = advisor.evaluate(ExportSpec(
        hs_code="1301.90.90",
        destination_country="SG",
        as_of_date="2025-09-15",
        fob_value=Decimal("100000"),
    ))
    assert pine_before.export_duty.status == "NOT_SUBJECT"
    assert pine_before.export_duty.estimated_duty_usd == Decimal("0.00")

    pine_after = advisor.evaluate(ExportSpec(
        hs_code="1301.90.90",
        destination_country="SG",
        as_of_date="2025-11-15",
        fob_value=Decimal("100000"),
        include_explain=True,
    ))
    assert pine_after.export_duty.status == "APPLICABLE"
    assert pine_after.export_duty.rate_percent == Decimal("25.00")
    assert pine_after.export_duty.estimated_duty_usd == Decimal("25000.00")

    # 3. Copper Concentrate (2603.00.00): COMBINED Permendag 9/2025 PE+LS Export Lartas + PMK 38/2024 7.5% Bea Keluar
    copper = advisor.evaluate(ExportSpec(
        hs_code="2603.00.00",
        destination_country="JP",
        as_of_date="2025-11-15",
        quantity_mt=Decimal("300"),
        export_price_usd_per_mt=Decimal("2000.00"),
        wo_confirmed=True,
        include_explain=True,
    ))
    assert copper.export_lartas.status == "EXPORT_PERMITS_MISSING"
    assert set(copper.export_lartas.required_docs) == {"persetujuanEkspor", "laporanSurveyorEkspor", "pebDeclaration"}
    assert copper.export_duty.status == "APPLICABLE"
    assert copper.export_duty.rate_percent == Decimal("7.50")
    assert copper.export_duty.estimated_duty_usd == Decimal("45000.00")
    cats = [st.category for st in copper.checklist]
    assert "LARTAS" in cats and "CUSTOMS_EXPORT_DUTY" in cats and "CUSTOMS_PEB" in cats


def test_batch_screener_export_and_import_csv_xlsx_audit_reports(services):
    _, _, screener = services

    # 1. Export portfolio batch (5 real SKUs + 1 intentional invalid HS row to verify fault tolerance)
    export_csv = "\n".join([
        "sku,hs_code,destination_country,as_of_date,fob_value,quantity_mt,reference_price_usd,export_price_usd_per_mt,rvc_percent,wo_confirmed,transshipment_hub,requires_back_to_back,third_party_invoice_country,provided_export_docs",
        "EXP-CPO-01,1511.10.00,CN,2025-11-15,950000,1000,950,,95,true,,,,",
        "EXP-COCOA-02,1801.00.10,MY,2025-11-15,200000,50,4000,4000,95,true,,,,",
        "EXP-CU-03,2603.00.00,JP,2025-11-15,600000,300,,2000,80,true,,,,",
        "EXP-TIN-04,8001.10.00,JP,2025-11-15,250000,,,,65,,SG,true,HK,",
        "EXP-NI-05,2604.00.00,CN,2025-11-15,500000,5000,,,100,true,,,,",
        "EXP-BAD-06,9999.99.99,SG,2025-11-15,10000,,,,,,,,,",
    ])
    parsed_rows = load_batch_rows_from_csv(export_csv)
    assert len(parsed_rows) == 6

    res = screener.screen(parsed_rows, mode="export", include_explain=True)
    assert res.kb_version == "0.15.0"
    assert res.summary.total_rows == 6
    assert res.summary.prohibited_count == 1
    assert res.summary.blocked_count == 2  # Copper (PE+LS) & Tin (ET+PE+LS)
    assert res.summary.action_required_count == 2  # CPO & Cocoa (PEB + Bea Keluar)
    assert res.summary.error_count == 1
    # Total Bea Keluar = CPO 124,000 + Cocoa 15,000 + Copper 45,000 = 184,000.00 USD
    assert res.summary.total_export_duty_usd == Decimal("184000.00")
    assert len(res.citations) >= 5

    # Verify UTF-8 BOM CSV output
    csv_bytes = res.to_csv_bytes()
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    csv_rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    assert len(csv_rows) == 6
    assert csv_rows[0]["Estimated Bea Keluar (USD)"] == "124000.00"
    assert csv_rows[4]["Overall Status"] == "PROHIBITED"
    assert csv_rows[5]["Overall Status"] == "ERROR"

    # Verify styled 3-sheet Excel (.xlsx) workbook with openpyxl
    xlsx_bytes = res.to_xlsx_bytes()
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    assert wb.sheetnames == ["Executive Summary", "Screening Results", "Legal Citations"]
    ws_sum = wb["Executive Summary"]
    summary_kv = {ws_sum.cell(r, 1).value: ws_sum.cell(r, 2).value for r in range(1, ws_sum.max_row + 1)}
    assert summary_kv["Total Shipments Screened"] == 6
    assert summary_kv["Total Estimated Bea Keluar (USD)"] == 184000.0
    ws_res = wb["Screening Results"]
    assert ws_res.max_row == 7  # 1 header + 6 rows
    assert ws_res.cell(2, 2).value == "EXP-CPO-01"
    assert ws_res.cell(2, 13).value == 124000.0
    ws_cit = wb["Legal Citations"]
    assert ws_cit.max_row >= 6

    # 2. Import manifest fast_lartas batch screening + XLSX input loader round-trip
    import_rows = [
        {"sku": "IMP-STL-01", "hs_code": "7209.26.10", "dispatch_country": "AU", "as_of_date": "2025-09-15", "cif_value": "100000", "api_type": "API-P"},
        {"sku": "IMP-PMP-02", "hs_code": "8413.70.11", "dispatch_country": "CN", "as_of_date": "2026-06-15", "cif_value": "40000", "api_type": "API-U", "provided_lartas_docs": "laporanSurveyor"},
        {"sku": "IMP-BAN-03", "hs_code": "6309.00.00", "dispatch_country": "TH", "as_of_date": "2026-06-15", "cif_value": "20000", "api_type": "API-U"},
    ]
    # Write as .xlsx and round-trip through load_batch_rows_from_xlsx
    in_wb = openpyxl.Workbook()
    in_ws = in_wb.active
    cols = ["sku", "hs_code", "dispatch_country", "as_of_date", "cif_value", "api_type", "provided_lartas_docs"]
    in_ws.append(cols)
    for row in import_rows:
        in_ws.append([row.get(c, "") for c in cols])
    in_buf = io.BytesIO()
    in_wb.save(in_buf)
    loaded_imp = load_batch_rows_from_xlsx(in_buf.getvalue())
    assert len(loaded_imp) == 3

    imp_res = screener.screen(loaded_imp, mode="import", screening_depth="fast_lartas", include_explain=True)
    assert imp_res.summary.total_rows == 3
    assert imp_res.summary.ready_count == 1  # Pump with LS provided
    assert imp_res.summary.blocked_count == 1  # Steel missing PI+LS
    assert imp_res.summary.prohibited_count == 1  # Worn clothing banned
    assert imp_res.rows[0].recommended_agreement == "iacepa"
    assert imp_res.rows[0].net_tax_savings == Decimal("11917.50")
    assert imp_res.summary.total_net_tax_savings > Decimal("12000.00")


def test_cli_batch_command_end_to_end(tmp_path, capsys):
    in_csv = tmp_path / "export_batch.csv"
    in_csv.write_text("\n".join([
        "sku,hs_code,destination_country,as_of_date,fob_value,quantity_mt,reference_price_usd",
        "CPO-1,1511.10.00,CN,2025-11-15,950000,1000,950",
        "PINE-2,1301.90.90,SG,2025-11-15,100000,,",
    ]), encoding="utf-8")
    out_csv = tmp_path / "audit.csv"
    out_xlsx = tmp_path / "audit.xlsx"

    rc = cli_main([
        "batch", str(in_csv),
        "--mode", "export",
        "--kb", str(CANONICAL_KB),
        "--explain",
        "--out-csv", str(out_csv),
        "--out-xlsx", str(out_xlsx),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kb_version"] == "0.15.0"
    assert payload["summary"]["total_export_duty_usd"] == "149000.00"  # 124,000 CPO + 25,000 Pine Resin
    assert out_csv.exists() and out_csv.stat().st_size > 100
    assert out_xlsx.exists() and out_xlsx.stat().st_size > 2000
