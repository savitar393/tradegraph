"""End-to-end Export Lartas (ET/PE/LS/Ban), PEB, Back-to-Back CO, Third-Party Invoicing, and Regional Cumulation tests (v0.14.0)."""

from decimal import Decimal
import json
from pathlib import Path

import pytest

from tradegraph.cli import main as cli_main
from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.optimization import ExportComplianceAdvisor, ExportSpec, MaterialInput, MultiFtaOptimizer


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"


@pytest.fixture(scope="module")
def advisor() -> ExportComplianceAdvisor:
    kb = compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)
    optimizer = MultiFtaOptimizer(kb, resolve_runtime_classpath())
    return ExportComplianceAdvisor(optimizer)


def test_export_lartas_matrix_prohibited_restricted_peb_and_temporal(advisor):
    # 1. Prohibited exports under Permendag 8/2025 (effective 2025-03-13): Nickel Ore & Raw Rattan
    for hs in ("2604.00.00", "1401.20.10"):
        res = advisor.evaluate(ExportSpec(
            hs_code=hs,
            destination_country="CN",
            as_of_date="2025-09-15",
            wo_confirmed=True,
            include_explain=True,
        ))
        assert res.kb_version == "0.15.0"
        assert res.export_lartas.status == "PROHIBITED_EXPORT"
        assert res.export_lartas.prohibited is True
        assert res.recommended_agreement == "PROHIBITED_EXPORT"
        assert res.checklist == ()
        assert all(s.eligibility == "PROHIBITED_EXPORT" for s in res.schemes)
        assert any("8 Tahun 2025" in (c.regulation_number or "") for c in res.export_lartas.citations)

    # 2. Temporal guard: before Permendag 8/2025 effective date (2025-03-13), rule is not yet active
    pre_eff = advisor.evaluate(ExportSpec(
        hs_code="2604.00.00",
        destination_country="CN",
        as_of_date="2025-03-01",
    ))
    assert pre_eff.export_lartas.prohibited is False
    assert pre_eff.export_lartas.status == "PEB_PENDING"

    # 3. Restricted exports under Permendag 9/2025:
    # - 8001.10.00 Refined Tin: ET + PE + LS
    # - 2701.11.00 Anthracite Coal: ET + LS (no PE)
    # - 7204.21.00 Stainless Steel Scrap: PE only
    tin = advisor.evaluate(ExportSpec(hs_code="8001.10.00", destination_country="SG", as_of_date="2025-09-15"))
    assert tin.export_lartas.status == "EXPORT_PERMITS_MISSING"
    assert set(tin.export_lartas.required_docs) == {
        "eksportirTerdaftar", "persetujuanEkspor", "laporanSurveyorEkspor", "pebDeclaration",
    }

    coal = advisor.evaluate(ExportSpec(
        hs_code="2701.11.00",
        destination_country="CN",
        as_of_date="2025-09-15",
        wo_confirmed=True,
        provided_export_docs=("eksportirTerdaftar",),
    ))
    assert coal.export_lartas.status == "EXPORT_PERMITS_MISSING"
    assert set(coal.export_lartas.missing_docs) == {"laporanSurveyorEkspor", "pebDeclaration"}
    assert "persetujuanEkspor" not in coal.export_lartas.required_docs

    scrap_peb_pending = advisor.evaluate(ExportSpec(
        hs_code="7204.21.00",
        destination_country="JP",
        as_of_date="2025-09-15",
        provided_export_docs=("persetujuanEkspor",),
    ))
    assert scrap_peb_pending.export_lartas.status == "PEB_PENDING"
    assert scrap_peb_pending.export_lartas.missing_docs == ("pebDeclaration",)

    scrap_ready = advisor.evaluate(ExportSpec(
        hs_code="7204.21.00",
        destination_country="JP",
        as_of_date="2025-09-15",
        provided_export_docs=("persetujuanEkspor", "pebDeclaration"),
    ))
    assert scrap_ready.export_lartas.status == "EXPORT_READY"
    assert scrap_ready.export_lartas.missing_docs == ()


def test_back_to_back_co_transshipment_hub_and_third_party_invoicing(advisor):
    # 1. Refined Tin 8001.10.00 (ID -> JP via SG hub) with Back-to-Back CO + Hong Kong Third-Party Invoice:
    #    IJEPA is bilateral -> BLOCKED_NO_BACK_TO_BACK; RCEP includes SG -> ELIGIBLE & recommended!
    res = advisor.evaluate(ExportSpec(
        hs_code="8001.10.00",
        destination_country="JP",
        as_of_date="2025-09-15",
        fob_value=Decimal("250000"),
        rvc_percent=Decimal("65.00"),
        transshipment_hub="SG",
        requires_back_to_back=True,
        third_party_invoice_country="HK",
        include_explain=True,
    ))
    assert res.recommended_agreement == "rcep"
    assert res.transshipment_hub == "singapore"
    by_agr = {s.agreement: s for s in res.schemes}
    assert set(by_agr) == {"rcep", "ijepa"}

    assert by_agr["ijepa"].back_to_back_allowed is False
    assert by_agr["ijepa"].back_to_back_status == "DISALLOWED_BILATERAL"
    assert by_agr["ijepa"].eligibility == "BLOCKED_NO_BACK_TO_BACK"

    assert by_agr["rcep"].back_to_back_allowed is True
    assert by_agr["rcep"].back_to_back_status == "ALLOWED"
    assert by_agr["rcep"].eligibility == "ELIGIBLE"
    assert by_agr["rcep"].origin_status == "SATISFIED"
    assert by_agr["rcep"].third_party_invoicing_allowed is True
    assert "Box 14" in (by_agr["rcep"].third_party_invoice_instruction or "")
    assert "Box 13" in (by_agr["rcep"].third_party_invoice_instruction or "")

    assert [s.category for s in res.checklist] == [
        "LARTAS", "LARTAS", "LARTAS", "CUSTOMS_PEB", "ORIGIN_PROOF", "TRANSSHIPMENT", "INVOICING",
    ]
    all_reg_nums = {
        c.regulation_number
        for c in (*res.export_lartas.citations, *(c for s in res.schemes for c in s.citations))
        if c.regulation_number
    }
    assert any("9 Tahun 2025" in r for r in all_reg_nums)
    assert any("155/PMK.04/2022" in r for r in all_reg_nums)
    assert any("209/PMK.04/2022" in r for r in all_reg_nums)
    assert any("73/PMK.04/2021" in r for r in all_reg_nums)

    # 2. Non-party hub test: ID -> SG via Japan hub with Back-to-Back CO:
    #    ATIGA does NOT include Japan -> BLOCKED_NON_PARTY_HUB; RCEP includes Japan -> ELIGIBLE!
    hub_res = advisor.evaluate(ExportSpec(
        hs_code="8001.10.00",
        destination_country="SG",
        as_of_date="2025-09-15",
        rvc_percent=Decimal("65.00"),
        transshipment_hub="JP",
        requires_back_to_back=True,
    ))
    hub_by_agr = {s.agreement: s for s in hub_res.schemes}
    assert hub_by_agr["atiga"].back_to_back_status == "HUB_NOT_A_PARTY"
    assert hub_by_agr["atiga"].eligibility == "BLOCKED_NON_PARTY_HUB"
    assert hub_by_agr["rcep"].back_to_back_status == "ALLOWED"
    assert hub_by_agr["rcep"].eligibility == "ELIGIBLE"
    assert hub_res.recommended_agreement == "rcep"


def test_regional_cumulation_sourcing_optimizer_before_and_after(advisor):
    # Before partner sourcing: 8507.60.33 EV Battery ID -> KR with Chinese 8507.90.92 separators & RVC 30%:
    # - IK-CEPA (general rule CTH or RVC40) fails CTH (same heading 8507) & RVC (30% < 40%) -> emits CumulationSuggestion for southKorea!
    # - RCEP (subheading rule CTSH or RVC40) passes CTSH (8507.90 != 8507.60) -> recommended!
    before = advisor.evaluate(ExportSpec(
        hs_code="8507.60.33",
        destination_country="KR",
        as_of_date="2025-09-15",
        materials=(MaterialInput(hs_code="8507.90.92", non_originating=True, origin_country="CN", origin_proof_verified=False),),
        rvc_percent=Decimal("30.00"),
        third_party_invoice_country="SG",
    ))
    before_by_agr = {s.agreement: s for s in before.schemes}
    assert before_by_agr["ikcepa"].eligibility == "INELIGIBLE_ROO"
    assert before_by_agr["rcep"].eligibility == "ELIGIBLE"
    assert before.recommended_agreement == "rcep"
    assert any(
        s.agreement == "ikcepa"
        and s.failing_material_hs == "8507.90.92"
        and s.recommended_partner_countries == ("southKorea",)
        for s in before.cumulation_opportunities
    )

    # After switching 8507.90.92 separator sourcing to South Korea with verified partner origin proof:
    # - IK-CEPA bilateral cumulation turns 8507.90.92 originating -> IK-CEPA becomes ELIGIBLE & recommended!
    after = advisor.evaluate(ExportSpec(
        hs_code="8507.60.33",
        destination_country="KR",
        as_of_date="2025-09-15",
        materials=(MaterialInput(hs_code="8507.90.92", non_originating=True, origin_country="KR", origin_proof_verified=True),),
        rvc_percent=Decimal("30.00"),
    ))
    after_by_agr = {s.agreement: s for s in after.schemes}
    assert after_by_agr["ikcepa"].eligibility == "ELIGIBLE"
    assert after_by_agr["ikcepa"].origin_status == "SATISFIED"
    assert after.recommended_agreement == "ikcepa"
    assert after.cumulation_opportunities == ()


def test_cli_export_command_end_to_end(tmp_path, capsys):
    # 1. Refined Tin ID -> JP via SG hub with --explain
    case_path = tmp_path / "export_tin.json"
    case_path.write_text(json.dumps({
        "hs_code": "8001.10.00",
        "origin_country": "ID",
        "destination_country": "JP",
        "as_of_date": "2025-09-15",
        "rvc_percent": "65.00",
        "transshipment_hub": "SG",
        "requires_back_to_back": True,
        "third_party_invoice_country": "HK",
    }), encoding="utf-8")

    rc = cli_main(["export", str(case_path), "--kb", str(CANONICAL_KB), "--explain"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kb_version"] == "0.15.0"
    assert payload["recommended_agreement"] == "rcep"
    assert payload["export_lartas"]["status"] == "EXPORT_PERMITS_MISSING"
    assert len(payload["export_lartas"]["citations"]) >= 3

    # 2. Prohibited Nickel Ore 2604.00.00 hard-blocks export & checklist
    ban_path = tmp_path / "export_nickel.json"
    ban_path.write_text(json.dumps({
        "hs_code": "2604.00.00",
        "destination_country": "CN",
        "as_of_date": "2025-09-15",
    }), encoding="utf-8")

    rc_ban = cli_main(["export", str(ban_path), "--kb", str(CANONICAL_KB), "--explain"])
    assert rc_ban == 0
    ban_payload = json.loads(capsys.readouterr().out)
    assert ban_payload["recommended_agreement"] == "PROHIBITED_EXPORT"
    assert ban_payload["checklist"] == []
    assert any("8 Tahun 2025" in (c["regulation_number"] or "") for c in ban_payload["export_lartas"]["citations"])
