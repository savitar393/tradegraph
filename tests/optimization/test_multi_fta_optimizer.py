"""End-to-end Multi-FTA Optimizer, Indonesian landed import tax (BM+PPN+PPh22), RCEP DV20 fallback, Compliance Planner, and CLI tests (v0.11.0)."""

from decimal import Decimal
import json
from pathlib import Path

import pytest

from tradegraph.cli import main as cli_main
from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.optimization import MaterialInput, MultiFtaOptimizer, ShipmentSpec


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"


@pytest.fixture(scope="module")
def optimizer():
    kb = compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)
    classpath = resolve_runtime_classpath()
    return MultiFtaOptimizer(kb, classpath)


def test_optimizer_combines_roo_eligibility_landed_import_taxes_and_compliance_plan(optimizer):
    # 1. Dishwasher 8422.11.00 (China -> ID, CIF $100,000, RVC 30%, parts 8422.90.10):
    #    ACFTA 0% FAILS RoO (needs RVC40) -> INELIGIBLE; RCEP 6% PASSES via CTSH -> RECOMMENDED!
    low_rvc = optimizer.optimize(ShipmentSpec(
        hs_code="8422.11.00",
        dispatch_country="CN",
        destination_country="ID",
        as_of_date="2025-09-15",
        cif_value=Decimal("100000"),
        currency="USD",
        has_api=True,
        materials=(MaterialInput("8422.90.10", non_originating=True),),
        rvc_percent=Decimal("30.00"),
        include_plan=True,
    ))
    assert low_rvc.recommended_agreement == "rcep"
    assert low_rvc.mfn_duty_percent == Decimal("10.00")
    assert low_rvc.mfn_tax_breakdown is not None
    # MFN 10%: BM $10,000 + PPN 11% ($12,100) + PPh22 2.5% ($2,750) = $24,850
    assert low_rvc.mfn_tax_breakdown.bm_amount == Decimal("10000.00")
    assert low_rvc.mfn_tax_breakdown.ppn_amount == Decimal("12100.00")
    assert low_rvc.mfn_tax_breakdown.pph22_amount == Decimal("2750.00")
    assert low_rvc.mfn_tax_breakdown.total_import_taxes == Decimal("24850.00")

    by_agr = {s.agreement: s for s in low_rvc.schemes}
    assert by_agr["acfta"].eligibility == "INELIGIBLE"
    assert by_agr["acfta"].applied_duty_percent == Decimal("10.00")
    assert by_agr["acfta"].total_tax_savings == Decimal("0.00")

    rcep = by_agr["rcep"]
    assert rcep.eligibility == "ELIGIBLE_PENDING_DOCS"
    assert rcep.origin_status == "SATISFIED"
    assert rcep.applied_duty_percent == Decimal("6.00")
    # RCEP 6%: BM $6,000 + PPN 11% ($11,660) + PPh22 2.5% ($2,650) = $20,310 -> saves $4,540.00!
    assert rcep.tax_breakdown.total_import_taxes == Decimal("20310.00")
    assert rcep.total_tax_savings == Decimal("4540.00")
    assert [step.action_id for step in rcep.plan_steps] == [
        "prepare-fta-origin-proof",
        "prepare-fta-pib-checklist",
    ]
    assert any("rcepFormRcep" in fact for fact in rcep.plan_steps[0].added_facts)

    # 2. Same dishwasher at RVC 45% (with non-API importer: PPh 22 = 7.5%):
    #    Now BOTH ACFTA (0%) and RCEP (6%) pass RoO -> ACFTA WINS with 0% BM!
    high_rvc = optimizer.optimize(ShipmentSpec(
        hs_code="8422.11.00",
        dispatch_country="china",
        destination_country="indonesia",
        as_of_date="2025-09-15",
        cif_value=Decimal("100000"),
        has_api=False,
        materials=(MaterialInput("8422.90.10", non_originating=True),),
        rvc_percent=Decimal("45.00"),
        provided_origin_docs=("acftaFormE",),
    ))
    assert high_rvc.recommended_agreement == "acfta"
    acfta_win = high_rvc.schemes[0]
    assert acfta_win.agreement == "acfta" and acfta_win.eligibility == "ELIGIBLE"
    assert acfta_win.applied_duty_percent == Decimal("0.00")
    # Non-API MFN 10%: BM $10,000 + PPN $12,100 + PPh22 7.5% ($8,250) = $30,350
    # Non-API ACFTA 0%: BM $0 + PPN $11,000 + PPh22 7.5% ($7,500) = $18,500 -> saves $11,850.00!
    assert acfta_win.tax_breakdown.pph22_percent == Decimal("7.50")
    assert acfta_win.tax_breakdown.total_import_taxes == Decimal("18500.00")
    assert acfta_win.total_tax_savings == Decimal("11850.00")


def test_rcep_dv20_automatic_fallback_duty_and_three_step_compliance_plan(optimizer):
    # Cold-rolled steel 7209.26.10 (Thailand -> ID, MFN 15%, RCEP Lampiran II DV20!)
    # made using verified Japanese originating 7208.54.10 hot-rolled coil (RVC 30%):
    # ATIGA & ACFTA fail RoO; RCEP passes via Japanese cumulation!
    base_kwargs = dict(
        hs_code="7209.26.10",
        dispatch_country="TH",
        destination_country="ID",
        as_of_date="2025-09-15",
        cif_value=Decimal("100000"),
        has_api=True,
        materials=(MaterialInput("7208.54.10", non_originating=True, origin_country="JP", origin_proof_verified=True),),
        rvc_percent=Decimal("30.00"),
        highest_contributor_country="japan",
    )

    # A. DV20 = 25% (>= 20%) -> Thailand is RCEP Country of Origin -> ASEAN 6.60% (saves $9,534.00 vs MFN 15%)!
    #    7209.26.10 also requires Permendag 22/2025 PI + LS at BORDER -> planner emits origin + lartas + pib (3 steps)!
    dv20_ok = optimizer.optimize(ShipmentSpec(**base_kwargs, dv20_percent=Decimal("25.00"), include_plan=True))
    assert dv20_ok.recommended_agreement == "rcep"
    assert dv20_ok.lartas.status == "BORDER_BLOCKED"
    assert set(dv20_ok.lartas.missing_border_docs) == {"persetujuanImpor", "laporanSurveyor"}
    rcep_ok = dv20_ok.schemes[0]
    assert rcep_ok.agreement == "rcep"
    assert rcep_ok.rcep_country_of_origin == "thailand"
    assert rcep_ok.dv20_status == "SATISFIED"
    assert rcep_ok.applied_duty_percent == Decimal("6.60")
    # Saving 8.40% BM on $100k = $8,400 * 1.135 = $9,534.00!
    assert rcep_ok.total_tax_savings == Decimal("9534.00")
    # DV20 already admitted, Lartas missing -> origin proof -> lartas permits -> PIB checklist!
    assert [s.action_id for s in rcep_ok.plan_steps] == [
        "prepare-fta-origin-proof",
        "prepare-lartas-permits",
        "prepare-fta-pib-checklist",
    ]
    assert any("persetujuanImpor" in f for f in rcep_ok.plan_steps[1].added_facts)
    assert any("laporanSurveyor" in f for f in rcep_ok.plan_steps[1].added_facts)

    # B. No DV20 statement yet (with Lartas PI+LS already cleared) -> planner skips Lartas and adds DV20 (3 steps)!
    dv20_missing = optimizer.optimize(ShipmentSpec(
        **base_kwargs,
        dv20_percent=None,
        provided_lartas_docs=("persetujuanImpor", "laporanSurveyor"),
        include_plan=True,
    ))
    assert dv20_missing.lartas.status == "CLEAR"
    rcep_miss = dv20_missing.schemes[0]
    assert rcep_miss.agreement == "rcep" and rcep_miss.dv20_status == "UNDETERMINED"
    assert [s.action_id for s in rcep_miss.plan_steps] == [
        "prepare-fta-origin-proof",
        "prepare-rcep-dv20-statement",
        "prepare-fta-pib-checklist",
    ]

    # C. DV20 = 15% (< 20%) -> PMK 209/2022 Pasal 14(5)(b) switches RCEP CoO to Japan -> MFN 15% -> recommends MFN!
    dv20_fail = optimizer.optimize(ShipmentSpec(**base_kwargs, dv20_percent=Decimal("15.00")))
    assert dv20_fail.recommended_agreement == "MFN"
    rcep_fail = next(s for s in dv20_fail.schemes if s.agreement == "rcep")
    assert rcep_fail.origin_status == "SATISFIED"
    assert rcep_fail.dv20_status == "NOT_SATISFIED"
    assert rcep_fail.rcep_country_of_origin == "japan"
    assert rcep_fail.applied_duty_percent == Decimal("15.00")
    assert rcep_fail.total_tax_savings == Decimal("0.00")
    assert any("Pasal 14 ayat 5 huruf b" in d for d in rcep_fail.diagnostics)


def test_cli_optimize_and_assess_with_revocation_alert(tmp_path, capsys):
    case_path = tmp_path / "penicillin_jp.json"
    case_path.write_text(json.dumps({
        "hs_code": "3004.10.15",
        "dispatch_country": "JP",
        "destination_country": "ID",
        "as_of_date": "2026-08-15",
        "cif_value": "50000",
        "currency": "USD",
        "has_api": True,
        "materials": [{"hs_code": "3004.10.19", "non_originating": True}],
        "rvc_percent": "45.00",
        "process_kind": "criterionChemicalReaction",
        "process_satisfied": True,
        "provided_origin_docs": ["ijepaFormJiepa", "rcepFormRcep"],
    }), encoding="utf-8")

    rc_opt = cli_main(["optimize", str(case_path), "--kb", str(CANONICAL_KB), "--plan"])
    assert rc_opt == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kb_version"] == "0.15.0"
    assert payload["recommended_agreement"] == "ijepa"
    assert payload["lartas"]["status"] == "BORDER_BLOCKED"
    assert set(payload["lartas"]["missing_border_docs"]) == {"bpomSkiBorder", "bpomIzinEdar"}
    by_agr = {s["agreement"]: s for s in payload["schemes"]}
    # IJEPA 0% is flagged with 2026-08-01 PMK 60/2026 revocation warning, while RCEP 2% is active!
    assert by_agr["ijepa"]["tariff_revoked_on"] == "2026-08-01"
    assert any("PMK tarif sumber dicabut" in d for d in by_agr["ijepa"]["diagnostics"])
    assert by_agr["rcep"]["applied_duty_percent"] == "2.00"
    assert by_agr["rcep"]["tariff_revoked_on"] is None
    # Origin proof submitted, BPOM SKI Border + Izin Edar missing -> planner emits Lartas permits then PIB checklist!
    assert [s["action_id"] for s in by_agr["ijepa"]["plan_steps"]] == [
        "prepare-lartas-permits",
        "prepare-fta-pib-checklist",
    ]

    rc_assess = cli_main(["assess", str(case_path), "--agreement", "rcep", "--kb", str(CANONICAL_KB)])
    assert rc_assess == 0
    single = json.loads(capsys.readouterr().out)
    assert single["agreement"] == "rcep"
    assert single["eligibility"] == "ELIGIBLE"
    assert single["applied_duty_percent"] == "2.00"
