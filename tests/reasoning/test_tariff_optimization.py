"""Multi-FTA Tariff Schedule Optimization (partner- & date-aware) and RCEP Tariff Differentials (DV20) tests (v0.10.0)."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import os
import re
import shutil

import pytest
from rdflib import Literal as RDFLiteral, Namespace, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext, AssessmentRequest, EvidenceClaim, FactPatch
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb
from tradegraph.kb.queries import prepare_local_query
from tradegraph.kb.rdf import parse_turtle
from tradegraph.reasoning.client import ReasoningClient


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"
EX = Namespace("urn:tradegraph:tariff-opt-test#")
PREFIXES = f"""
@prefix ex: <{EX}> .
@prefix tg: <{TG}> .
@prefix xsd: <{XSD}> .
"""


def _resolve_java_classpath(tmp_path_factory) -> str:
    if shutil.which("mvn"):
        return ReasoningClient.build_classpath(
            ROOT / "semantic" / "pom.xml", tmp_path_factory.mktemp("tariff-cp") / "classpath.txt",
        )
    classes_dir = ROOT / "semantic" / "target" / "classes"
    tree_file = ROOT / "docs" / "engine-dependency-tree.txt"
    m2 = Path.home() / ".m2" / "repository"
    if not classes_dir.is_dir() or not tree_file.is_file() or not m2.is_dir():
        pytest.skip("Maven or precompiled semantic classes + local .m2 cache are required.")
    entries = [str(classes_dir)]
    for line in tree_file.read_text(encoding="utf-8").splitlines():
        match = re.search(r"([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+):jar:([a-zA-Z0-9_.-]+):(compile|runtime)", line)
        if match:
            group, artifact, version, _ = match.groups()
            jar = m2 / Path(*group.split(".")) / artifact / version / f"{artifact}-{version}.jar"
            if jar.is_file():
                entries.append(str(jar))
    return os.pathsep.join(entries)


@pytest.fixture(scope="module")
def classpath(tmp_path_factory):
    return _resolve_java_classpath(tmp_path_factory)


@pytest.fixture(scope="module")
def canonical_kb():
    return compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)


def _optimize(canonical_kb, code: str, dispatch: str, as_of_date: str, destination: str = "indonesia") -> list[dict]:
    owl = canonical_kb.dataset.graph(canonical_kb.owl_projection_ref)
    q = prepare_local_query(canonical_kb.query_catalog["optimize-fta-tariff"].sparql, "SelectQuery")
    rows = []
    for row in owl.query(q, initBindings={
        "code": TG[code],
        "dispatch": TG[dispatch],
        "destination": TG[destination],
        "asOfDate": RDFLiteral(as_of_date),
    }):
        d = row.asdict()
        rows.append({
            "agreement": str(d["agreement"]).split("#")[-1],
            "facility": str(d["facilityCode"]) if d.get("facilityCode") else None,
            "psr_level": str(d["psrLevel"]) if d.get("psrLevel") else None,
            "rule": str(d["conciseRule"]) if d.get("conciseRule") else None,
            "mfn": Decimal(str(d["mfnDutyPercent"])) if d.get("mfnDutyPercent") is not None else None,
            "effective": Decimal(str(d["effectiveDutyPercent"])) if d.get("effectiveDutyPercent") is not None else None,
            "savings": Decimal(str(d["dutySavingsPercent"])) if d.get("dutySavingsPercent") is not None else None,
            "excluded": str(d.get("excludedToMfn")).lower() == "true",
            "requires_dv20": str(d.get("requiresRcepDv20")).lower() == "true",
            "revoked_on": str(d["tariffRevokedOn"]) if d.get("tariffRevokedOn") else None,
            "superseding_doc": str(d["tariffSupersedingDoc"]).split("#")[-1] if d.get("tariffSupersedingDoc") else None,
        })
    return rows


def test_optimize_fta_tariff_ranks_schemes_across_partners_dates_mfn_and_revocations(canonical_kb):
    # 1. Centrifugal pump 8413.70.11 (China -> ID): RCEP beats ACFTA (0% margin vs 5% MFN) and steps down in FY2026!
    pump_2025 = _optimize(canonical_kb, "hs-84137011", "china", "2025-09-15")
    assert [r["agreement"] for r in pump_2025] == ["rcep", "acfta"]
    assert pump_2025[0]["mfn"] == Decimal("5.00")
    assert pump_2025[0]["effective"] == Decimal("2.50")
    assert pump_2025[0]["savings"] == Decimal("2.50")
    assert pump_2025[0]["requires_dv20"] is False
    assert pump_2025[1]["effective"] == Decimal("5.00") and pump_2025[1]["savings"] == Decimal("0.00")

    pump_2026 = _optimize(canonical_kb, "hs-84137011", "china", "2026-06-15")
    assert pump_2026[0]["agreement"] == "rcep"
    assert pump_2026[0]["effective"] == Decimal("2.00")
    assert pump_2026[0]["savings"] == Decimal("3.00")

    # 2. Dishwasher 8422.11.00 (China -> ID): ACFTA 0% beats RCEP (6% -> 5%) by 10% savings vs MFN!
    dw_2025 = _optimize(canonical_kb, "hs-84221100", "china", "2025-09-15")
    assert [r["agreement"] for r in dw_2025] == ["acfta", "rcep"]
    assert dw_2025[0]["effective"] == Decimal("0.00") and dw_2025[0]["savings"] == Decimal("10.00")
    assert dw_2025[1]["effective"] == Decimal("6.00") and dw_2025[1]["savings"] == Decimal("4.00")

    dw_2026 = _optimize(canonical_kb, "hs-84221100", "china", "2026-06-15")
    assert dw_2026[1]["effective"] == Decimal("5.00") and dw_2026[1]["savings"] == Decimal("5.00")

    # 3. Cold-rolled steel 7209.26.10 (MFN 15% + RCEP Lampiran II DV20!):
    # Thailand -> ID: ATIGA (0%) < ACFTA (5%) < RCEP-ASEAN (6.60% -> 5.90%, requires DV20!)
    th_2025 = _optimize(canonical_kb, "hs-72092610", "thailand", "2025-09-15")
    assert [r["agreement"] for r in th_2025] == ["atiga", "acfta", "rcep"]
    assert th_2025[0]["effective"] == Decimal("0.00") and th_2025[0]["requires_dv20"] is False
    assert th_2025[1]["effective"] == Decimal("5.00") and th_2025[1]["requires_dv20"] is False
    assert th_2025[2]["effective"] == Decimal("6.60") and th_2025[2]["savings"] == Decimal("8.40")
    assert th_2025[2]["requires_dv20"] is True and th_2025[2]["excluded"] is False

    th_2026 = _optimize(canonical_kb, "hs-72092610", "thailand", "2026-06-15")
    assert th_2026[2]["effective"] == Decimal("5.90") and th_2026[2]["savings"] == Decimal("9.10")

    # China -> ID: ACFTA (5%) beats RCEP-China (excluded to MFN 15%, 0% savings, requires DV20!)
    cn_steel = _optimize(canonical_kb, "hs-72092610", "china", "2025-09-15")
    assert [r["agreement"] for r in cn_steel] == ["acfta", "rcep"]
    assert cn_steel[0]["effective"] == Decimal("5.00") and cn_steel[0]["savings"] == Decimal("10.00")
    assert cn_steel[1]["excluded"] is True and cn_steel[1]["effective"] == Decimal("15.00")
    assert cn_steel[1]["savings"] == Decimal("0.00") and cn_steel[1]["requires_dv20"] is True

    # Japan -> ID: both IJEPA and RCEP-Japan are excluded to MFN (15%, 0% savings)!
    jp_steel = _optimize(canonical_kb, "hs-72092610", "japan", "2025-09-15")
    assert {r["agreement"] for r in jp_steel} == {"ijepa", "rcep"}
    assert all(r["excluded"] is True and r["effective"] == Decimal("15.00") and r["savings"] == Decimal("0.00") for r in jp_steel)

    # 4. Penicillin 3004.10.15 (Japan -> ID): IJEPA (0%) beats RCEP (2%), AND JDIH revocation surfaces on 2026-08-15!
    med_jun = _optimize(canonical_kb, "hs-30041015", "japan", "2026-06-15")
    assert [r["agreement"] for r in med_jun] == ["ijepa", "rcep"]
    assert med_jun[0]["effective"] == Decimal("0.00") and med_jun[0]["revoked_on"] is None
    assert med_jun[1]["effective"] == Decimal("2.00") and med_jun[1]["revoked_on"] is None

    med_aug = _optimize(canonical_kb, "hs-30041015", "japan", "2026-08-15")
    assert med_aug[0]["agreement"] == "ijepa"
    assert med_aug[0]["revoked_on"] == "2026-08-01"
    assert "pmk-60-tahun-2026" in (med_aug[0]["superseding_doc"] or "")
    assert med_aug[1]["agreement"] == "rcep" and med_aug[1]["revoked_on"] is None


def _assess_rcep_dv20_case(
    tmp_path: Path,
    kb,
    classpath: str,
    *,
    product_hs: str,
    material_hs: str,
    material_origin_country: str | None = None,
    dv20_percent: str | None = None,
    highest_contributor: str | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = CaseRepository(tmp_path / f"dv20-{product_hs}.sqlite3")
    context = AssessmentContext(
        kb.snapshot.sha256,
        ENGINE_PROFILE_ID,
        jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
        procedure_iri=str(TG.importForUse),
        agreement_iri=str(TG.rcep),
        transaction_at=datetime.fromisoformat("2025-09-15T13:00:00+07:00"),
        evaluation_at=datetime.fromisoformat("2025-12-01T13:00:00+07:00"),
        evidence_scope_id="case-documents",
        product_iri=str(EX.product),
        batch_iri=str(EX.batch),
    )
    base = repo.create_case("", context)
    rev = f"<urn:tradegraph:case:{base.case_id}:revision:2>"

    cumul = ""
    if material_origin_country is not None:
        cumul = f" ; tg:materialOriginCountry tg:{material_origin_country} ; tg:materialOriginProofVerified true"

    facts = [f"""
ex:product tg:hasTariffClassification tg:{product_hs} ; tg:hasBatch ex:batch .
ex:batch tg:producedAt ex:facility ; tg:producedBy ex:producer ; tg:usesMaterial ex:material .
ex:material tg:hasTariffClassification tg:{material_hs} .
ex:facility tg:withinLocation tg:thailand .
ex:shipment tg:containsGoods ex:product ; tg:shipmentDispatchCountry tg:thailand ;
    tg:shipmentDestinationCountry tg:indonesia ; tg:cargoCategory tg:regularCargo ;
    tg:routeKind tg:directRoute ; tg:shipmentRouteClosed true .
{rev} tg:revisionShipment ex:shipment .
ex:importerAssignment tg:roleActor ex:importer ; tg:hasActorRole tg:roleImporter ; tg:assignedInRevision {rev} .
ex:bom a tg:ComponentList ; tg:componentListForBatch ex:batch ;
    tg:componentListClosed true ; tg:componentEntry ex:entry .
ex:entry tg:entryMaterial ex:material ; tg:nonOriginatingInput true {cumul} .
"""]
    claims = []

    def add_doc(name: str, doc_type: str, extra: str = ""):
        facts.append(f"""
ex:{name} a tg:Document ; tg:documentType tg:{doc_type} ;
    tg:documentSubject ex:product ; tg:documentJurisdiction tg:jurisdictionIndonesiaCustoms ;
    tg:documentShipment ex:shipment ; tg:documentBatch ex:batch ;
    tg:documentValidFrom "2025-01-01T00:00:00Z"^^xsd:dateTime ;
    tg:documentValidUntil "2027-01-01T00:00:00Z"^^xsd:dateTime {extra} .
""")
        claims.append(EvidenceClaim(
            str(EX[f"{name}Claim"]), str(EX.product), str(TG.hasSupportingDocument), str(EX[name]),
            EX[name], verification_status="VERIFIED",
        ))

    add_doc("transport", "transportRecord")
    add_doc("originDoc", "rcepFormRcep", "; tg:documentIssuingCountry tg:thailand ; tg:submittedForPreference true")
    add_doc("bomDoc", "bomStatement", "; tg:documentComponentList ex:bom")
    add_doc("rvcDoc", "rvcWorksheet", "; tg:recordedRvcPercent 30.00")

    if dv20_percent is not None:
        extra = f"; tg:recordedDomesticValueAdditionPercent {dv20_percent}"
        if highest_contributor is not None:
            extra += f" ; tg:highestOriginatingContributorCountry tg:{highest_contributor}"
        add_doc("dv20Doc", "dv20Statement", extra)

    triples = tuple(parse_turtle((PREFIXES + "\n".join(facts)).encode("utf-8"), str(EX)))
    revised = repo.revise_case(
        base.case_id, base.revision,
        FactPatch("rcep dv20 test", add_triples=triples, evidence_claims=tuple(claims), evidence_scope_closed=True),
    )
    service = AssessmentService(repo, ReasoningClient(kb, classpath))
    return service.assess(AssessmentRequest(kb.snapshot.sha256, revised.case_id, revised.revision))


def test_rcep_tariff_differential_dv20_policy_end_to_end(canonical_kb, classpath, tmp_path):
    # Cold-rolled steel 7209.26.10 (in RCEP Lampiran II / Appendix to Annex I!) produced in Thailand
    # using cumulated Japanese originating 7208.54.10 hot-rolled coil:
    # 1. DV20 = 25.00% (>= 20%) -> SATISFIED (Thailand is RCEP Country of Origin -> ASEAN 6.60%/5.90% schedule!)
    st_pass = {f.policy_id: f.status for f in _assess_rcep_dv20_case(
        tmp_path / "steel-dv20-pass", canonical_kb, classpath,
        product_hs="hs-72092610", material_hs="hs-72085410",
        material_origin_country="japan", dv20_percent="25.00", highest_contributor="japan",
    ).domain_findings}
    assert st_pass["rcep-psr-overall"] == "SATISFIED"
    assert st_pass["rcep-tariff-differential-dv20"] == "SATISFIED"

    # 2. DV20 = 15.00% (< 20%) -> NOT_SATISFIED (origin still SATISFIED via RCEP cumulation,
    #    but RCEP Country of Origin falls back to highest originating contributor Japan -> MFN 15% per PMK 209/2022 Pasal 14(5)(b)!)
    st_fail = {f.policy_id: f.status for f in _assess_rcep_dv20_case(
        tmp_path / "steel-dv20-fail", canonical_kb, classpath,
        product_hs="hs-72092610", material_hs="hs-72085410",
        material_origin_country="japan", dv20_percent="15.00", highest_contributor="japan",
    ).domain_findings}
    assert st_fail["rcep-psr-overall"] == "SATISFIED"
    assert st_fail["rcep-tariff-differential-dv20"] == "NOT_SATISFIED"

    # 3. No DV20 statement submitted for Appendix product 7209.26.10 -> UNDETERMINED!
    st_missing = {f.policy_id: f.status for f in _assess_rcep_dv20_case(
        tmp_path / "steel-dv20-missing", canonical_kb, classpath,
        product_hs="hs-72092610", material_hs="hs-72085410",
        material_origin_country="japan", dv20_percent=None,
    ).domain_findings}
    assert st_missing["rcep-psr-overall"] == "SATISFIED"
    assert st_missing["rcep-tariff-differential-dv20"] == "UNDETERMINED"

    # 4. Non-Appendix product 8413.70.11 under RCEP -> NOT_APPLICABLE!
    st_na = {f.policy_id: f.status for f in _assess_rcep_dv20_case(
        tmp_path / "pump-dv20-na", canonical_kb, classpath,
        product_hs="hs-84137011", material_hs="hs-84139130",
        dv20_percent=None,
    ).domain_findings}
    assert st_na["rcep-psr-overall"] == "SATISFIED"
    assert st_na["rcep-tariff-differential-dv20"] == "NOT_APPLICABLE"
