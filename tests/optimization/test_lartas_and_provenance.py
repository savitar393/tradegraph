"""Tests for Indonesian Lartas / NTM clearance (Kemendag 2025 + PerBPOM 28/2023) and audit-grade --explain provenance (v0.12.0)."""

from decimal import Decimal
import json
from pathlib import Path

import pytest

from tradegraph.cli import main as cli_main
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.optimization import MaterialInput, MultiFtaOptimizer, ShipmentSpec


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"


@pytest.fixture(scope="module")
def optimizer():
    kb = compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)
    classpath = resolve_runtime_classpath()
    return MultiFtaOptimizer(kb, classpath)


def test_lartas_matrix_border_post_border_prohibited_clear_and_unrestricted(optimizer):
    jur = TG.jurisdictionIndonesiaCustoms

    def check(hs: str, as_of: str = "2025-09-15", provided: tuple[str, ...] = (), api_type: str | None = None, explain: bool = False):
        hs_iri = optimizer.resolve_hs_iri(hs)
        return optimizer._assess_lartas(
            hs_iri, jur,
            ShipmentSpec(hs_code=hs, dispatch_country="TH", as_of_date=as_of, api_type=api_type, provided_lartas_docs=provided, include_explain=explain),
        )

    # 1. PROHIBITED: 6309.00.00 (Pakaian bekas) effective 2026-01-01 under Permendag 47/2025
    before_ban = check("6309.00.00", as_of="2025-12-15")
    assert before_ban.status == "UNRESTRICTED"
    proh = check("6309.00.00", as_of="2026-06-15", explain=True)
    assert proh.status == "PROHIBITED" and proh.prohibited is True
    assert any("47 Tahun 2025" in (c.regulation_number or "") and c.url for c in proh.citations)

    # 2. BORDER_BLOCKED (Kemendag): 5801.36.10 & 6101.20.00 (PI+LS, Permendag 17/2025), 7209.26.10 (PI+LS, Permendag 22/2025)
    for hs, reg in (("5801.36.10", "17 Tahun 2025"), ("6101.20.00", "17 Tahun 2025"), ("7209.26.10", "22 Tahun 2025")):
        res = check(hs, explain=True)
        assert res.status == "BORDER_BLOCKED"
        assert set(res.missing_border_docs) == {"persetujuanImpor", "laporanSurveyor"}
        assert any(reg in (c.regulation_number or "") and c.url for c in res.citations)

    # 3. BORDER_BLOCKED (Elektronik): 8413.70.11 (LS only, Permendag 21/2025) stays ACTIVE in 2026 (Permendag 47/2025 Pasal 8(b) is partial only!)
    pump_2026 = check("8413.70.11", as_of="2026-06-15", explain=True)
    assert pump_2026.status == "BORDER_BLOCKED"
    assert pump_2026.missing_border_docs == ("laporanSurveyor",)
    assert any("21 Tahun 2025" in (c.regulation_number or "") and c.url for c in pump_2026.citations)
    pump_clear = check("8413.70.11", as_of="2026-06-15", provided=("laporanSurveyor",), api_type="API-P")
    assert pump_clear.status == "CLEAR" and not pump_clear.api_mismatch

    # 4. BORDER_BLOCKED (BPOM): 3004.10.15 (bpomSkiBorder + bpomIzinEdar, PerBPOM 28/2023)
    obat = check("3004.10.15", explain=True)
    assert obat.status == "BORDER_BLOCKED"
    assert set(obat.missing_border_docs) == {"bpomSkiBorder", "bpomIzinEdar"}
    assert any("28 Tahun 2023" in (c.regulation_number or "") and c.url for c in obat.citations)

    # 5. POST_BORDER_PENDING (Kemendag & BPOM): 0101.21.00 (PI only, Permendag 18/2025), 0901.21.11 & 0902.10.10 (PerBPOM 28/2023)
    horse = check("0101.21.00", explain=True)
    assert horse.status == "POST_BORDER_PENDING"
    assert horse.missing_border_docs == () and horse.missing_post_border_docs == ("persetujuanImpor",)
    assert any("18 Tahun 2025" in (c.regulation_number or "") and c.url for c in horse.citations)

    for hs in ("0901.21.11", "0902.10.10"):
        food = check(hs, explain=True)
        assert food.status == "POST_BORDER_PENDING"
        assert set(food.missing_post_border_docs) == {"bpomSkiPostBorder", "bpomIzinEdar"}
        assert any("28 Tahun 2023" in (c.regulation_number or "") and c.url for c in food.citations)

    # 6. UNRESTRICTED: 8422.11.00, 2309.10.10, 9605.00.00
    for hs in ("8422.11.00", "2309.10.10", "9605.00.00"):
        assert check(hs).status == "UNRESTRICTED"


def test_prohibited_import_blocks_fta_recommendation_and_skips_planning(optimizer):
    result = optimizer.optimize(ShipmentSpec(
        hs_code="6309.00.00",
        dispatch_country="TH",
        destination_country="ID",
        as_of_date="2026-06-15",
        cif_value=Decimal("20000"),
        include_plan=True,
        include_explain=True,
    ))
    assert result.recommended_agreement == "PROHIBITED"
    assert result.lartas.status == "PROHIBITED"
    assert result.lartas.prohibited is True
    assert any("BARANG DILARANG IMPOR" in d for d in result.lartas.diagnostics)
    assert any("47 Tahun 2025" in (c.regulation_number or "") for c in result.lartas.citations)
    assert all(s.eligibility == "PROHIBITED" and s.plan_steps == () for s in result.schemes)


def test_four_step_combined_fta_dv20_lartas_pib_plan_and_audit_provenance(optimizer):
    # Cold-rolled steel 7209.26.10 (TH -> ID, JP cumulation, missing DV20 & missing PI+LS):
    # Generates complete 4-step clearance plan + full resolved citations + ProvenanceService support explanations!
    result = optimizer.optimize(
        ShipmentSpec(
            hs_code="7209.26.10",
            dispatch_country="TH",
            destination_country="ID",
            as_of_date="2025-09-15",
            cif_value=Decimal("100000"),
            has_api=True,
            api_type="API-P",
            materials=(MaterialInput("7208.54.10", non_originating=True, origin_country="JP", origin_proof_verified=True),),
            rvc_percent=Decimal("30.00"),
            highest_contributor_country="japan",
            include_plan=True,
            include_explain=True,
        ),
        agreements=("rcep",),
    )
    assert result.recommended_agreement == "rcep"
    assert result.lartas.status == "BORDER_BLOCKED"
    assert any("22 Tahun 2025" in (c.regulation_number or "") and c.url for c in result.lartas.citations)
    assert any("7 Tahun 2021" in (c.regulation_number or "") for c in result.tax_profile_citations)
    assert any("41/PMK.010/2022" in (c.regulation_number or "") for c in result.tax_profile_citations)

    rcep = result.schemes[0]
    assert [s.action_id for s in rcep.plan_steps] == [
        "prepare-fta-origin-proof",
        "prepare-rcep-dv20-statement",
        "prepare-lartas-permits",
        "prepare-fta-pib-checklist",
    ]
    assert any("221/PMK.010/2022" in (c.regulation_number or "") for c in rcep.citations)
    assert len(rcep.explanations) >= 2
    by_policy = {e.policy_id: e for e in rcep.explanations}
    assert "rcep-psr-overall" in by_policy
    assert "rcep-psr-ctc" in by_policy
    assert "rcep-psr-consignment" in by_policy
    assert "rcep-tariff-differential-dv20" in by_policy
    assert by_policy["rcep-psr-overall"].availability in {"AVAILABLE", "PARTIAL"}
    assert "DECISION_QUERY" in by_policy["rcep-psr-overall"].node_kinds
    assert by_policy["rcep-psr-overall"].source_citations
    # Satisfied evidence-backed criteria (CTC via Japanese cumulation + direct consignment) trace all the way to ASSERTED_FACT!
    assert by_policy["rcep-psr-ctc"].reaches_asserted_facts is True
    assert "VALIDATION_REPORT" in by_policy["rcep-psr-ctc"].node_kinds
    assert by_policy["rcep-psr-consignment"].reaches_asserted_facts is True
    # Generic Multi-FTA SWRL rule witness for RVC failure (30% < 40%)
    assert "rcep-psr-rvc" in by_policy
    assert "SWRL_RULE" in by_policy["rcep-psr-rvc"].node_kinds
    assert by_policy["rcep-psr-rvc"].reaches_asserted_facts is True

    # RCEP DV20 SWRL rule witness (25% >= 20% -> SATISFIED via rcep-dv20-threshold-met)
    dv20_res = optimizer.optimize(
        ShipmentSpec(
            hs_code="7209.26.10",
            dispatch_country="TH",
            destination_country="ID",
            as_of_date="2025-09-15",
            cif_value=Decimal("100000"),
            materials=(MaterialInput("7208.54.10", non_originating=True, origin_country="JP", origin_proof_verified=True),),
            rvc_percent=Decimal("45.00"),
            dv20_percent=Decimal("25.00"),
            include_explain=True,
        ),
        agreements=("rcep",),
    )
    dv20_exp = {e.policy_id: e for e in dv20_res.schemes[0].explanations}
    assert dv20_res.schemes[0].dv20_status == "SATISFIED"
    assert "SWRL_RULE" in dv20_exp["rcep-tariff-differential-dv20"].node_kinds
    assert dv20_exp["rcep-tariff-differential-dv20"].reaches_asserted_facts is True
    assert "SWRL_RULE" in dv20_exp["rcep-psr-rvc"].node_kinds


def test_cli_explain_flag_on_optimize_and_assess(tmp_path, capsys):
    # 8413.70.11 CN -> ID on 2026-06-15: MFN 5%, ACFTA 5%, RCEP China staged to 2.00% -> RCEP wins!
    # Permendag 21/2025 LS BORDER stays active in 2026 -> CLEAR when laporanSurveyor provided.
    case_path = tmp_path / "pump_cn_2026.json"
    case_path.write_text(json.dumps({
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
    }), encoding="utf-8")

    rc_opt = cli_main(["optimize", str(case_path), "--kb", str(CANONICAL_KB), "--agreements", "acfta,rcep", "--explain"])
    assert rc_opt == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kb_version"] == "0.15.0"
    assert payload["recommended_agreement"] == "rcep"
    assert payload["lartas"]["status"] == "CLEAR"
    assert any("21 Tahun 2025" in (c["regulation_number"] or "") and c["url"] for c in payload["lartas"]["citations"])
    assert len(payload["tax_profile_citations"]) >= 2

    rcep_opt = payload["schemes"][0]
    assert rcep_opt["agreement"] == "rcep" and rcep_opt["eligibility"] == "ELIGIBLE"
    assert rcep_opt["applied_duty_percent"] == "2.00"
    assert any("224/PMK.010/2022" in (c["regulation_number"] or "") for c in rcep_opt["citations"])
    assert len(rcep_opt["explanations"]) >= 1
    assert any(e["reaches_asserted_facts"] is True for e in rcep_opt["explanations"])

    rc_assess = cli_main(["assess", str(case_path), "--agreement", "acfta", "--kb", str(CANONICAL_KB), "--explain"])
    assert rc_assess == 0
    acfta = json.loads(capsys.readouterr().out)
    assert acfta["agreement"] == "acfta" and acfta["applied_duty_percent"] == "5.00"
    assert len(acfta["citations"]) >= 1 and len(acfta["explanations"]) >= 1
