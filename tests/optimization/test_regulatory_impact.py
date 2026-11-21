"""End-to-end Regulatory Change Diff & Portfolio Impact Analyzer (Temporal What-If) tests."""

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
    MultiFtaOptimizer,
    RegulatoryImpactAnalyzer,
)


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"


@pytest.fixture(scope="module")
def analyzer() -> RegulatoryImpactAnalyzer:
    kb = compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)
    optimizer = MultiFtaOptimizer(kb, resolve_runtime_classpath())
    screener = BatchScreener(optimizer, ExportComplianceAdvisor(optimizer))
    return RegulatoryImpactAnalyzer(optimizer, screener)


def test_regulatory_diff_windows(analyzer):
    # 1. PMK 68/2025 window (eff. 2025-10-22)
    d1 = analyzer.diff_regulations("2025-09-15", "2025-11-15")
    assert d1.kb_version == "0.15.0"
    assert any("68 Tahun 2025" in (e.acting_regulation_number or "") for e in d1.enacted_regulations)
    assert any(
        e.change_type == "AMENDS"
        and "68 Tahun 2025" in (e.acting_regulation_number or "")
        and "38 Tahun 2024" in (e.target_regulation_number or "")
        for e in d1.legal_events
    )

    # 2. Permendag 47/2025 window (eff. 2026-01-01)
    d2 = analyzer.diff_regulations("2025-09-15", "2026-06-15")
    assert any("47 Tahun 2025" in (e.acting_regulation_number or "") for e in d2.enacted_regulations)
    assert any(
        e.change_type == "PARTIALLY_REVOKES" and "47 Tahun 2025" in (e.acting_regulation_number or "")
        for e in d2.legal_events
    )

    # 3. PMK 60/2026 & PMK 61/2026 revocation cliff (eff. 2026-08-01)
    d3 = analyzer.diff_regulations("2026-06-15", "2026-08-15")
    assert any(
        e.change_type == "REVOKES"
        and "60 TAHUN 2026" in (e.acting_regulation_number or "")
        and "50/PMK.010/2022" in (e.target_regulation_number or "")
        for e in d3.legal_events
    )

    # 4. Invalid date ordering raises ValueError
    with pytest.raises(ValueError, match="strictly later"):
        analyzer.diff_regulations("2025-11-15", "2025-09-15")


def test_export_pmk68_shock_and_excel_csv_reports(analyzer):
    rows = [
        {"sku": "COCOA-01", "hs_code": "1801.00.10", "destination_country": "MY", "fob_value": "200000", "quantity_mt": "50", "reference_price_usd": "4000", "export_price_usd_per_mt": "4000", "wo_confirmed": True},
        {"sku": "PINE-02", "hs_code": "1301.90.90", "destination_country": "SG", "fob_value": "100000", "wo_confirmed": True},
        {"sku": "CPO-03", "hs_code": "1511.10.00", "destination_country": "CN", "fob_value": "950000", "quantity_mt": "1000", "reference_price_usd": "950", "wo_confirmed": True},
        {"sku": "COPPER-04", "hs_code": "2603.00.00", "destination_country": "JP", "fob_value": "600000", "quantity_mt": "300", "export_price_usd_per_mt": "2000", "wo_confirmed": True},
    ]
    res = analyzer.analyze_portfolio(
        rows,
        mode="export",
        baseline_date="2025-09-15",
        target_date="2025-11-15",
        include_explain=True,
    )
    by_sku = {r.reference_id: r for r in res.rows}

    # Cocoa Beans: 15% ($30,000) -> 7.5% ($15,000) = -$15,000 FAVORABLE
    assert by_sku["COCOA-01"].impact_level == "FAVORABLE"
    assert "EXPORT_DUTY_DECREASED" in by_sku["COCOA-01"].change_tags
    assert by_sku["COCOA-01"].delta_export_duty_usd == Decimal("-15000.00")

    # Getah Pinus: NOT_SUBJECT ($0) -> 25% ($25,000) = +$25,000 ADVERSE
    assert by_sku["PINE-02"].impact_level == "ADVERSE"
    assert "NEW_EXPORT_DUTY" in by_sku["PINE-02"].change_tags
    assert by_sku["PINE-02"].delta_export_duty_usd == Decimal("25000.00")

    # CPO: $124,000 -> $124,000, but legal basis updates from PMK 38/2024 to PMK 68/2025
    assert by_sku["CPO-03"].impact_level == "REGULATORY_UPDATE"
    assert by_sku["CPO-03"].delta_export_duty_usd == Decimal("0.00")

    # Copper: unchanged ($45,000 -> $45,000, Lampiran E untouched by PMK 68/2025)
    assert by_sku["COPPER-04"].impact_level == "UNCHANGED"
    assert by_sku["COPPER-04"].delta_export_duty_usd == Decimal("0.00")

    # Net financial impact = -15,000 + 25,000 = +10,000.00 USD
    assert res.summary.favorable_count == 1
    assert res.summary.adverse_count == 1
    assert res.summary.regulatory_update_count == 1
    assert res.summary.unchanged_count == 1
    assert res.summary.net_financial_impact_usd == Decimal("10000.00")
    assert len(res.citations) >= 5

    # Verify UTF-8 BOM CSV output
    csv_bytes = res.to_csv_bytes()
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    csv_rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    assert len(csv_rows) == 4
    assert csv_rows[0]["Delta Bea Keluar USD"] == "-15000.00"
    assert csv_rows[1]["Delta Bea Keluar USD"] == "25000.00"

    # Verify styled 4-sheet Excel (.xlsx) audit workbook
    xlsx_bytes = res.to_xlsx_bytes()
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    assert wb.sheetnames == [
        "Executive Impact Summary",
        "Regulatory Changes",
        "SKU Impact Diff",
        "Legal Citations",
    ]
    ws_sum = wb["Executive Impact Summary"]
    kv = {ws_sum.cell(r, 1).value: ws_sum.cell(r, 2).value for r in range(1, ws_sum.max_row + 1)}
    assert kv["Net Bea Keluar Impact USD (+Cost / -Savings)"] == 10000.0
    assert wb["Regulatory Changes"].max_row >= 3
    assert wb["SKU Impact Diff"].max_row == 5
    assert wb["Legal Citations"].max_row >= 6


def test_import_2026_ban_rcep_staging_and_pmk60_revocation_cliff(analyzer):
    # A. 2025-09-15 -> 2026-06-15: Permendag 47/2025 prohibition + RCEP 2026 tariff cuts
    imp_rows = [
        {"sku": "BAN-WORN-01", "hs_code": "6309.00.00", "dispatch_country": "TH", "cif_value": "20000", "api_type": "API-U"},
        {"sku": "PUMP-CN-02", "hs_code": "8413.70.11", "dispatch_country": "CN", "cif_value": "100000", "api_type": "API-U", "agreements": "rcep", "provided_lartas_docs": "laporanSurveyor"},
        {"sku": "STEEL-TH-03", "hs_code": "7209.26.10", "dispatch_country": "TH", "cif_value": "100000", "api_type": "API-P", "agreements": "rcep"},
    ]
    res_2026 = analyzer.analyze_portfolio(
        imp_rows,
        mode="import",
        baseline_date="2025-09-15",
        target_date="2026-06-15",
        screening_depth="fast_lartas",
        include_explain=True,
    )
    by_sku = {r.reference_id: r for r in res_2026.rows}
    assert by_sku["BAN-WORN-01"].impact_level == "BREAKING"
    assert "NEW_PROHIBITION" in by_sku["BAN-WORN-01"].change_tags
    # Prohibition must NOT be falsely reported as negative tax savings
    assert by_sku["BAN-WORN-01"].delta_import_taxes is None

    assert by_sku["PUMP-CN-02"].impact_level == "FAVORABLE"
    assert by_sku["PUMP-CN-02"].baseline_duty_percent == Decimal("2.50")
    assert by_sku["PUMP-CN-02"].target_duty_percent == Decimal("2.00")
    assert by_sku["PUMP-CN-02"].delta_import_taxes < Decimal("0")

    assert by_sku["STEEL-TH-03"].impact_level == "FAVORABLE"
    assert by_sku["STEEL-TH-03"].baseline_duty_percent == Decimal("6.60")
    assert by_sku["STEEL-TH-03"].target_duty_percent == Decimal("5.90")
    assert by_sku["STEEL-TH-03"].delta_import_taxes < Decimal("0")

    assert res_2026.summary.breaking_count == 1
    assert res_2026.summary.favorable_count == 2
    assert res_2026.summary.net_financial_impact_usd < Decimal("0")

    # B. 2026-06-15 -> 2026-08-15: PMK 60/2026 revokes IJEPA tariff (50/PMK.010/2022) on 2026-08-01
    rev_rows = [
        {"sku": "PUMP-JP-01", "hs_code": "8413.70.11", "dispatch_country": "JP", "cif_value": "80000", "api_type": "API-P", "agreements": "ijepa", "provided_lartas_docs": "laporanSurveyor"},
        {"sku": "PUMP-CN-02", "hs_code": "8413.70.11", "dispatch_country": "CN", "cif_value": "80000", "api_type": "API-P", "agreements": "rcep", "provided_lartas_docs": "laporanSurveyor"},
    ]
    res_rev = analyzer.analyze_portfolio(
        rev_rows,
        mode="import",
        baseline_date="2026-06-15",
        target_date="2026-08-15",
        screening_depth="fast_lartas",
    )
    rev_by_sku = {r.reference_id: r for r in res_rev.rows}
    assert rev_by_sku["PUMP-JP-01"].impact_level == "BREAKING"
    assert "TARIFF_PMK_REVOKED" in rev_by_sku["PUMP-JP-01"].change_tags
    assert "60 TAHUN 2026" in (rev_by_sku["PUMP-JP-01"].newly_revoked_pmk or "")
    assert rev_by_sku["PUMP-CN-02"].impact_level == "UNCHANGED"

    # C. 2025-03-01 -> 2025-09-15: Export Lartas wave (Permendag 8 & 9/2025 eff. 2025-03-13)
    exp_wave = analyzer.analyze_portfolio(
        [
            {"sku": "NI-01", "hs_code": "2604.00.00", "destination_country": "CN", "fob_value": "500000"},
            {"sku": "TIN-02", "hs_code": "8001.10.00", "destination_country": "SG", "fob_value": "250000"},
        ],
        mode="export",
        baseline_date="2025-03-01",
        target_date="2025-09-15",
    )
    ew = {r.reference_id: r for r in exp_wave.rows}
    assert ew["NI-01"].impact_level == "BREAKING" and "NEW_PROHIBITION" in ew["NI-01"].change_tags
    assert ew["TIN-02"].impact_level == "ADVERSE" and "NEW_LARTAS_PERMITS" in ew["TIN-02"].change_tags
    assert set(ew["TIN-02"].added_permits) == {"eksportirTerdaftar", "persetujuanEkspor", "laporanSurveyorEkspor"}


def test_cli_impact_command_end_to_end(tmp_path, capsys):
    in_csv = tmp_path / "portfolio.csv"
    in_csv.write_text("\n".join([
        "sku,hs_code,destination_country,fob_value,quantity_mt,reference_price_usd,export_price_usd_per_mt",
        "COCOA-1,1801.00.10,MY,200000,50,4000,4000",
        "PINE-2,1301.90.90,SG,100000,,,",
    ]), encoding="utf-8")
    out_csv = tmp_path / "impact.csv"
    out_xlsx = tmp_path / "impact.xlsx"

    rc = cli_main([
        "impact", str(in_csv),
        "--mode", "export",
        "--from-date", "2025-09-15",
        "--to-date", "2025-11-15",
        "--kb", str(CANONICAL_KB),
        "--explain",
        "--out-csv", str(out_csv),
        "--out-xlsx", str(out_xlsx),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kb_version"] == "0.15.0"
    assert payload["summary"]["net_financial_impact_usd"] == "10000.00"
    assert out_csv.exists() and out_csv.stat().st_size > 100
    assert out_xlsx.exists() and out_xlsx.stat().st_size > 2000
