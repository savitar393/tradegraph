"""End-to-end 6-FTA (ATIGA, RCEP, ACFTA, IJEPA, IK-CEPA, IA-CEPA) and scaled Ch. 72/84/85 optimization tests (v0.13.0)."""

from decimal import Decimal
from pathlib import Path

import pytest

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


def test_korea_ev_battery_switches_between_rcep_ctsh_and_ikcepa_rvc40(optimizer):
    # 8507.60.33 EV Li-ion Battery (South Korea -> ID, MFN 10%, Unrestricted Lartas):
    # Using non-originating 8507.90.92 battery separators (SAME heading 85.07, DIFFERENT subheading 8507.90 -> 8507.60):
    # A. At RVC 30%: IK-CEPA 0% FAILS (needs CTH or RVC40) -> RCEP Korea 6.00% PASSES via CTSH and WINS!
    low_rvc = optimizer.optimize(ShipmentSpec(
        hs_code="8507.60.33",
        dispatch_country="KR",
        destination_country="ID",
        as_of_date="2025-09-15",
        cif_value=Decimal("100000"),
        has_api=True,
        materials=(MaterialInput("8507.90.92", non_originating=True),),
        rvc_percent=Decimal("30.00"),
    ))
    assert low_rvc.kb_version == "0.15.0"
    assert low_rvc.recommended_agreement == "rcep"
    by_agr = {s.agreement: s for s in low_rvc.schemes}
    assert set(by_agr) == {"ikcepa", "rcep"}
    assert by_agr["ikcepa"].eligibility == "INELIGIBLE"
    assert by_agr["rcep"].eligibility == "ELIGIBLE_PENDING_DOCS"
    assert by_agr["rcep"].applied_duty_percent == Decimal("6.00")
    assert by_agr["rcep"].total_tax_savings == Decimal("4540.00")

    # B. At RVC 45% with Form KI-CEPA: IK-CEPA 0.00% (PIB 72) WINS over RCEP Korea 6.00% and saves $11,350.00!
    high_rvc = optimizer.optimize(ShipmentSpec(
        hs_code="8507.60.33",
        dispatch_country="KR",
        destination_country="ID",
        as_of_date="2025-09-15",
        cif_value=Decimal("100000"),
        has_api=True,
        materials=(MaterialInput("8507.90.92", non_originating=True),),
        rvc_percent=Decimal("45.00"),
        provided_origin_docs=("ikcepaFormKicepa",),
        include_explain=True,
    ))
    assert high_rvc.recommended_agreement == "ikcepa"
    ik = high_rvc.schemes[0]
    assert ik.agreement == "ikcepa" and ik.facility_code == "72"
    assert ik.eligibility == "ELIGIBLE"
    assert ik.applied_duty_percent == Decimal("0.00")
    assert ik.total_tax_savings == Decimal("11350.00")
    assert any("227/PMK.010/2022" in (c.regulation_number or "") for c in ik.citations)
    assert any("219/PMK.04/2022" in (c.regulation_number or "") for c in ik.citations)


def test_korea_tinplate_ikcepa_beats_rcep_mfn_and_plans_form_kicepa_plus_lartas(optimizer):
    # 7210.12.10 Tinplate (KR -> ID, MFN 12.50%, Permendag 22/2025 PI+LS BORDER):
    # RCEP-KR is MFN-excluded (12.50%); IK-CEPA is 0.00% -> saves $14,187.50 on $100k CIF!
    res = optimizer.optimize(ShipmentSpec(
        hs_code="7210.12.10",
        dispatch_country="KR",
        destination_country="ID",
        as_of_date="2025-09-15",
        cif_value=Decimal("100000"),
        has_api=True,
        api_type="API-P",
        materials=(MaterialInput("7208.10.00", non_originating=True),),
        rvc_percent=Decimal("45.00"),
        include_plan=True,
    ))
    assert res.recommended_agreement == "ikcepa"
    assert res.lartas.status == "BORDER_BLOCKED"
    assert set(res.lartas.missing_border_docs) == {"persetujuanImpor", "laporanSurveyor"}
    by_agr = {s.agreement: s for s in res.schemes}
    assert by_agr["rcep"].eligibility == "EXCLUDED_TO_MFN"
    ik = by_agr["ikcepa"]
    assert ik.applied_duty_percent == Decimal("0.00")
    assert ik.total_tax_savings == Decimal("14187.50")
    assert [s.action_id for s in ik.plan_steps] == [
        "prepare-fta-origin-proof",
        "prepare-lartas-permits",
        "prepare-fta-pib-checklist",
    ]
    assert any("ikcepaFormKicepa" in f for f in ik.plan_steps[0].added_facts)
    assert any("persetujuanImpor" in f for f in ik.plan_steps[1].added_facts)


def test_australia_steel_iacepa_beats_rcep_australia_and_skips_dv20(optimizer):
    # 7209.26.10 Cold-Rolled Steel (AU -> ID, MFN 15.00%, Permendag 22/2025 PI+LS BORDER):
    # IA-CEPA (PIB 65) is 4.50% with NO DV20, beating RCEP Australia (6.60% + DV20) and saving $11,917.50!
    res = optimizer.optimize(ShipmentSpec(
        hs_code="7209.26.10",
        dispatch_country="AU",
        destination_country="ID",
        as_of_date="2025-09-15",
        cif_value=Decimal("100000"),
        has_api=True,
        api_type="API-P",
        materials=(MaterialInput("7208.10.00", non_originating=True),),
        rvc_percent=Decimal("45.00"),
        provided_origin_docs=("iacepaOriginDeclaration",),
        provided_lartas_docs=("persetujuanImpor", "laporanSurveyor"),
        include_explain=True,
    ))
    assert res.recommended_agreement == "iacepa"
    assert res.lartas.status == "CLEAR"
    by_agr = {s.agreement: s for s in res.schemes}
    assert set(by_agr) == {"iacepa", "rcep"}
    ia = by_agr["iacepa"]
    assert ia.facility_code == "65"
    assert ia.back_to_back_allowed is False
    assert ia.eligibility == "ELIGIBLE"
    assert ia.applied_duty_percent == Decimal("4.50")
    # 10.50% BM saving on $100k = $10,500 * 1.135 = $11,917.50
    assert ia.total_tax_savings == Decimal("11917.50")
    assert any("54/PMK.010/2022" in (c.regulation_number or "") for c in ia.citations)
    assert any("82/PMK.04/2020" in (c.regulation_number or "") for c in ia.citations)
    assert by_agr["rcep"].applied_duty_percent == Decimal("6.60")
    assert by_agr["rcep"].requires_rcep_dv20 is True
