"""Multi-FTA reasoning, treaty party membership, multi-level PSR specificity, and scheme comparison tests (v0.8.0)."""

from dataclasses import replace
from datetime import datetime
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
from tradegraph.scenarios.service import ScenarioService


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"
EX = Namespace("http://www.semanticweb.org/tradegraph/multi-fta-test#")
PREFIXES = f"""
@prefix ex: <{EX}> .
@prefix tg: <{TG}> .
@prefix xsd: <{XSD}> .
"""


def _resolve_java_classpath(tmp_path_factory) -> str:
    if shutil.which("mvn"):
        return ReasoningClient.build_classpath(
            ROOT / "semantic" / "pom.xml", tmp_path_factory.mktemp("multi-fta-cp") / "classpath.txt",
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


def _assess_fta_case(
    tmp_path: Path,
    kb,
    classpath: str,
    *,
    agreement: str,
    dispatch_country: str,
    product_hs: str,
    origin_doc_type: str,
    material_hs: str | None = None,
    bom_doc_type: str = "bomStatement",
    rvc_percent: str | None = None,
    wo_confirmed: bool | None = None,
    process_kind: str | None = None,
    process_satisfied: bool | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = CaseRepository(tmp_path / f"{agreement}-{dispatch_country}-{product_hs}.sqlite3")
    context = AssessmentContext(
        kb.snapshot.sha256,
        ENGINE_PROFILE_ID,
        jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
        procedure_iri=str(TG.importForUse),
        agreement_iri=str(TG[agreement]),
        transaction_at=datetime.fromisoformat("2025-09-15T13:00:00+07:00"),
        evaluation_at=datetime.fromisoformat("2025-12-01T13:00:00+07:00"),
        evidence_scope_id="case-documents",
        product_iri=str(EX.product),
        batch_iri=str(EX.batch),
    )
    base = repo.create_case("", context)
    rev = f"<http://www.semanticweb.org/tradegraph/case/{base.case_id}/revision/2>"
    facts = [f"""
ex:product tg:hasTariffClassification tg:{product_hs} ; tg:hasBatch ex:batch .
ex:batch tg:producedAt ex:facility ; tg:producedBy ex:producer .
ex:facility tg:withinLocation tg:{dispatch_country} .
ex:shipment tg:containsGoods ex:product ; tg:shipmentDispatchCountry tg:{dispatch_country} ;
    tg:shipmentDestinationCountry tg:indonesia ; tg:cargoCategory tg:regularCargo ;
    tg:routeKind tg:directRoute ; tg:shipmentRouteClosed true .
{rev} tg:revisionShipment ex:shipment .
ex:importerAssignment tg:roleActor ex:importer ; tg:hasActorRole tg:roleImporter ; tg:assignedInRevision {rev} .
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
    add_doc("originDoc", origin_doc_type, f"; tg:documentIssuingCountry tg:{dispatch_country} ; tg:submittedForPreference true")

    if material_hs is not None:
        facts.append(f"""
ex:batch tg:usesMaterial ex:material .
ex:material tg:hasTariffClassification tg:{material_hs} .
ex:bom a tg:ComponentList ; tg:componentListForBatch ex:batch ;
    tg:componentListClosed true ; tg:componentEntry ex:entry .
ex:entry tg:entryMaterial ex:material ; tg:nonOriginatingInput true .
""")
        add_doc("bomDoc", bom_doc_type, "; tg:documentComponentList ex:bom")

    if rvc_percent is not None:
        add_doc("rvcDoc", "rvcWorksheet", f"; tg:recordedRvcPercent {rvc_percent}")

    if wo_confirmed is not None:
        add_doc("woDoc", "whollyObtainedStatement", f"; tg:whollyObtainedConfirmed {'true' if wo_confirmed else 'false'}")

    if process_kind is not None and process_satisfied is not None:
        add_doc(
            "procDoc", "processRuleStatement",
            f"; tg:satisfiedProcessKind tg:{process_kind} ; tg:processRuleSatisfied {'true' if process_satisfied else 'false'}",
        )

    triples = tuple(parse_turtle((PREFIXES + "\n".join(facts)).encode("utf-8"), str(EX)))
    revised = repo.revise_case(
        base.case_id, base.revision,
        FactPatch("multi-fta test", add_triples=triples, evidence_claims=tuple(claims), evidence_scope_closed=True),
    )
    service = AssessmentService(repo, ReasoningClient(kb, classpath))
    report = service.assess(AssessmentRequest(kb.snapshot.sha256, revised.case_id, revised.revision))
    return report, service, revised


def test_atiga_vs_rcep_real_psr_crossovers_thailand_to_indonesia(canonical_kb, classpath, tmp_path):
    # 1. Green tea 0902.10.10 (RVC 45%, not WO): ATIGA (RVC40 or CC) PASSES, RCEP (WO only) FAILS.
    tea_atiga, _, _ = _assess_fta_case(
        tmp_path / "tea-atiga", canonical_kb, classpath,
        agreement="atiga", dispatch_country="thailand", product_hs="hs-09021010",
        origin_doc_type="atigaFormD", rvc_percent="45.00", wo_confirmed=False,
    )
    tea_rcep, _, _ = _assess_fta_case(
        tmp_path / "tea-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-09021010",
        origin_doc_type="rcepFormRcep", rvc_percent="45.00", wo_confirmed=False,
    )
    st_tea_a = {f.policy_id: f.status for f in tea_atiga.domain_findings}
    st_tea_r = {f.policy_id: f.status for f in tea_rcep.domain_findings}
    assert st_tea_a["atiga-psr-overall"] == "SATISFIED"
    assert st_tea_a["atiga-psr-origin-evidence"] == "SATISFIED"
    assert st_tea_r["rcep-psr-wo"] == "NOT_SATISFIED"
    assert st_tea_r["rcep-psr-rvc"] == "NOT_APPLICABLE"
    assert st_tea_r["rcep-psr-overall"] == "NOT_SATISFIED"
    assert st_tea_r["rcep-psr-origin-evidence"] == "SATISFIED"

    # 2. Roasted coffee 0901.21.12 from imported green beans 0901.11.20 (CTSH, RVC 35%):
    # ATIGA (RVC40 or CTSH) PASSES; RCEP (RVC40 only) FAILS!
    cof_atiga, _, _ = _assess_fta_case(
        tmp_path / "coffee-atiga", canonical_kb, classpath,
        agreement="atiga", dispatch_country="thailand", product_hs="hs-09012112",
        origin_doc_type="atigaFormD", material_hs="hs-09011120", rvc_percent="35.00",
    )
    cof_rcep, _, _ = _assess_fta_case(
        tmp_path / "coffee-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-09012112",
        origin_doc_type="rcepFormRcep", material_hs="hs-09011120", rvc_percent="35.00",
    )
    assert {f.policy_id: f.status for f in cof_atiga.domain_findings}["atiga-psr-overall"] == "SATISFIED"
    st_cof_r = {f.policy_id: f.status for f in cof_rcep.domain_findings}
    assert st_cof_r["rcep-psr-ctc"] == "NOT_APPLICABLE"
    assert st_cof_r["rcep-psr-rvc"] == "NOT_SATISFIED"
    assert st_cof_r["rcep-psr-overall"] == "NOT_SATISFIED"

    # 3. Cold-rolled steel 7209.26.10 from 7211.13.13 narrow strip (RVC 35%):
    # ATIGA subheading 7209.26 excludes 7208 & 7211 -> FAILS!
    # RCEP HEADING 72.09 excludes 7208 only (allows 7211) -> PASSES!
    steel_atiga, _, _ = _assess_fta_case(
        tmp_path / "steel-atiga", canonical_kb, classpath,
        agreement="atiga", dispatch_country="thailand", product_hs="hs-72092610",
        origin_doc_type="atigaFormD", material_hs="hs-72111313", rvc_percent="35.00",
    )
    steel_rcep, _, _ = _assess_fta_case(
        tmp_path / "steel-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-72092610",
        origin_doc_type="rcepFormRcep", material_hs="hs-72111313", rvc_percent="35.00",
    )
    assert {f.policy_id: f.status for f in steel_atiga.domain_findings}["atiga-psr-overall"] == "NOT_SATISFIED"
    st_st_r = {f.policy_id: f.status for f in steel_rcep.domain_findings}
    assert st_st_r["rcep-psr-ctc"] == "SATISFIED"
    assert st_st_r["rcep-psr-overall"] == "SATISFIED"

    # RCEP heading 72.09 STILL blocks excluded heading 7208 when RVC < 40%:
    steel_rcep_7208, _, _ = _assess_fta_case(
        tmp_path / "steel-rcep-7208", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-72092610",
        origin_doc_type="rcepFormRcep", material_hs="hs-72085410", rvc_percent="35.00",
    )
    assert {f.policy_id: f.status for f in steel_rcep_7208.domain_findings}["rcep-psr-overall"] == "NOT_SATISFIED"

    # 4. Household dishwasher 8422.11.00 from 8422.90.10 parts (RVC 30%):
    # ATIGA (RVC40 or CTH or RVC35+CTSH) FAILS (30% < 35%); RCEP (CTSH or RVC40) PASSES!
    dw_atiga, _, _ = _assess_fta_case(
        tmp_path / "dw-atiga", canonical_kb, classpath,
        agreement="atiga", dispatch_country="thailand", product_hs="hs-84221100",
        origin_doc_type="atigaFormD", material_hs="hs-84229010", rvc_percent="30.00",
    )
    dw_rcep, _, _ = _assess_fta_case(
        tmp_path / "dw-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-84221100",
        origin_doc_type="rcepFormRcep", material_hs="hs-84229010", rvc_percent="30.00",
    )
    assert {f.policy_id: f.status for f in dw_atiga.domain_findings}["atiga-psr-overall"] == "NOT_SATISFIED"
    assert {f.policy_id: f.status for f in dw_rcep.domain_findings}["rcep-psr-overall"] == "SATISFIED"

    # 5. Knitted cotton overcoat 6101.20.00 from Ch.58 fabric (RVC 30%, no cut-and-sewn AMS):
    # ATIGA requires CC + CutAndSewnAMS -> UNDETERMINED; RCEP CHAPTER 61 rule (CC only) -> PASSES!
    coat_atiga, _, _ = _assess_fta_case(
        tmp_path / "coat-atiga", canonical_kb, classpath,
        agreement="atiga", dispatch_country="thailand", product_hs="hs-61012000",
        origin_doc_type="atigaFormD", material_hs="hs-58013610", rvc_percent="30.00",
    )
    coat_rcep, _, _ = _assess_fta_case(
        tmp_path / "coat-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-61012000",
        origin_doc_type="rcepOriginDeclaration", material_hs="hs-58013610", rvc_percent="30.00",
    )
    assert {f.policy_id: f.status for f in coat_atiga.domain_findings}["atiga-psr-overall"] == "UNDETERMINED"
    st_coat_r = {f.policy_id: f.status for f in coat_rcep.domain_findings}
    assert st_coat_r["rcep-psr-overall"] == "SATISFIED"
    assert st_coat_r["rcep-psr-origin-evidence"] == "SATISFIED"


def test_china_party_isolation_origin_proofs_and_acfta_vs_rcep_crossovers(canonical_kb, classpath, tmp_path):
    # 1. China is NOT an ATIGA party -> OUT_OF_COVERAGE!
    cn_atiga, _, _ = _assess_fta_case(
        tmp_path / "cn-atiga", canonical_kb, classpath,
        agreement="atiga", dispatch_country="china", product_hs="hs-58013610",
        origin_doc_type="atigaFormD", rvc_percent="50.00",
    )
    assert cn_atiga.coverage_status == "OUT_OF_COVERAGE"

    # 2. Chenille fabric 5801.36.10 from China using same-chapter 5804.10.11 fabric (RVC 35%):
    # ACFTA Ch.58 (PMK 171/2020 Lampiran A I.3.b: CTH or RVC40) PASSES (5804 -> 5801)!
    # RCEP Ch.58 (Annex 3A p.75: CC only) FAILS (same chapter 58)!
    fab_acfta, _, _ = _assess_fta_case(
        tmp_path / "fab-acfta", canonical_kb, classpath,
        agreement="acfta", dispatch_country="china", product_hs="hs-58013610",
        origin_doc_type="acftaFormE", material_hs="hs-58041011", rvc_percent="35.00",
    )
    fab_rcep, _, _ = _assess_fta_case(
        tmp_path / "fab-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="china", product_hs="hs-58013610",
        origin_doc_type="rcepFormRcep", material_hs="hs-58041011", rvc_percent="35.00",
    )
    st_fab_ac = {f.policy_id: f.status for f in fab_acfta.domain_findings}
    assert st_fab_ac["acfta-psr-ctc"] == "SATISFIED"
    assert st_fab_ac["acfta-psr-overall"] == "SATISFIED"
    assert st_fab_ac["acfta-psr-origin-evidence"] == "SATISFIED"
    assert {f.policy_id: f.status for f in fab_rcep.domain_findings}["rcep-psr-overall"] == "NOT_SATISFIED"

    # 3. Dishwasher 8422.11.00 from China using 8422.90.10 parts (RVC 30%):
    # ACFTA General Rule (PMK 171/2020 Pasal 4(2)(a): RVC40) FAILS; RCEP (CTSH or RVC40) PASSES!
    dw_cn_acfta, _, _ = _assess_fta_case(
        tmp_path / "dw-cn-acfta", canonical_kb, classpath,
        agreement="acfta", dispatch_country="china", product_hs="hs-84221100",
        origin_doc_type="acftaFormE", material_hs="hs-84229010", rvc_percent="30.00",
    )
    dw_cn_rcep, _, _ = _assess_fta_case(
        tmp_path / "dw-cn-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="china", product_hs="hs-84221100",
        origin_doc_type="rcepFormRcep", material_hs="hs-84229010", rvc_percent="30.00",
    )
    assert {f.policy_id: f.status for f in dw_cn_acfta.domain_findings}["acfta-psr-overall"] == "NOT_SATISFIED"
    assert {f.policy_id: f.status for f in dw_cn_rcep.domain_findings}["rcep-psr-overall"] == "SATISFIED"

    # 4. Origin proof document mismatch: submitting ATIGA Form D under ACFTA does NOT satisfy Form E obligation!
    wrong_coo, _, _ = _assess_fta_case(
        tmp_path / "cn-wrong-coo", canonical_kb, classpath,
        agreement="acfta", dispatch_country="china", product_hs="hs-58013610",
        origin_doc_type="atigaFormD", material_hs="hs-58041011", rvc_percent="35.00",
    )
    st_wrong = {f.policy_id: f.status for f in wrong_coo.domain_findings}
    assert st_wrong["acfta-psr-overall"] == "SATISFIED"
    assert st_wrong["acfta-psr-origin-evidence"] == "UNDETERMINED"


def test_japan_ijepa_vs_rcep_double_crossover_and_party_isolation(canonical_kb, classpath, tmp_path):
    # 1. Japan is NOT an ATIGA or ACFTA party -> OUT_OF_COVERAGE!
    jp_acfta, _, _ = _assess_fta_case(
        tmp_path / "jp-acfta", canonical_kb, classpath,
        agreement="acfta", dispatch_country="japan", product_hs="hs-30041015",
        origin_doc_type="acftaFormE", rvc_percent="50.00",
    )
    assert jp_acfta.coverage_status == "OUT_OF_COVERAGE"

    # 2. Animal feed 2309.10.10 from Japan using Ch.23 oil-cake 2306.10.00 (RVC 45%):
    # IJEPA (conjunctive CC + QVC40) FAILS CC; RCEP heading 23.09 (CTH or RVC40) PASSES!
    feed_ijepa, _, _ = _assess_fta_case(
        tmp_path / "feed-ijepa", canonical_kb, classpath,
        agreement="ijepa", dispatch_country="japan", product_hs="hs-23091010",
        origin_doc_type="ijepaFormJiepa", material_hs="hs-23061000", rvc_percent="45.00",
    )
    feed_rcep, _, _ = _assess_fta_case(
        tmp_path / "feed-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="japan", product_hs="hs-23091010",
        origin_doc_type="rcepFormRcep", material_hs="hs-23061000", rvc_percent="45.00",
    )
    assert {f.policy_id: f.status for f in feed_ijepa.domain_findings}["ijepa-psr-overall"] == "NOT_SATISFIED"
    assert {f.policy_id: f.status for f in feed_rcep.domain_findings}["rcep-psr-overall"] == "SATISFIED"

    # 3. Penicillin medicament 3004.10.15 DOUBLE CROSSOVER between IJEPA & RCEP:
    # Direction A: formulated from bulk 3003.10.10 (RVC 30%, no chemical reaction):
    #   IJEPA (CTH except 30.03; QVC40; or CR) FAILS; RCEP heading 30.04 (CTH or RVC40) PASSES!
    med_a_ijepa, _, _ = _assess_fta_case(
        tmp_path / "med-a-ijepa", canonical_kb, classpath,
        agreement="ijepa", dispatch_country="japan", product_hs="hs-30041015",
        origin_doc_type="ijepaFormJiepa", material_hs="hs-30031010", rvc_percent="30.00",
        process_kind="criterionChemicalReaction", process_satisfied=False,
    )
    med_a_rcep, _, _ = _assess_fta_case(
        tmp_path / "med-a-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="japan", product_hs="hs-30041015",
        origin_doc_type="rcepFormRcep", material_hs="hs-30031010", rvc_percent="30.00",
        process_kind="criterionChemicalReaction", process_satisfied=False,
    )
    assert {f.policy_id: f.status for f in med_a_ijepa.domain_findings}["ijepa-psr-overall"] == "NOT_SATISFIED"
    assert {f.policy_id: f.status for f in med_a_rcep.domain_findings}["rcep-psr-overall"] == "SATISFIED"

    # Direction B: synthesized via Chemical Reaction from same-heading 3004.10.19 (RVC 30%):
    #   IJEPA PASSES (Chemical Reaction alternative!); RCEP FAILS (no CR alternative)!
    med_b_ijepa, _, _ = _assess_fta_case(
        tmp_path / "med-b-ijepa", canonical_kb, classpath,
        agreement="ijepa", dispatch_country="japan", product_hs="hs-30041015",
        origin_doc_type="ijepaFormJiepa", material_hs="hs-30041019", rvc_percent="30.00",
        process_kind="criterionChemicalReaction", process_satisfied=True,
    )
    med_b_rcep, _, _ = _assess_fta_case(
        tmp_path / "med-b-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="japan", product_hs="hs-30041015",
        origin_doc_type="rcepFormRcep", material_hs="hs-30041019", rvc_percent="30.00",
        process_kind="criterionChemicalReaction", process_satisfied=True,
    )
    st_b_ij = {f.policy_id: f.status for f in med_b_ijepa.domain_findings}
    assert st_b_ij["ijepa-psr-process"] == "SATISFIED"
    assert st_b_ij["ijepa-psr-overall"] == "SATISFIED"
    assert st_b_ij["ijepa-psr-origin-evidence"] == "SATISFIED"
    assert {f.policy_id: f.status for f in med_b_rcep.domain_findings}["rcep-psr-overall"] == "NOT_SATISFIED"


def test_compare_fta_schemes_catalog_query_and_ijepa_2026_temporal_lineage(canonical_kb):
    owl = canonical_kb.dataset.graph(canonical_kb.owl_projection_ref)
    cmp_q = prepare_local_query(canonical_kb.query_catalog["compare-fta-schemes"].sparql, "SelectQuery")

    def summarize(code: str, dispatch: str, destination: str = "indonesia") -> dict[str, dict]:
        rows = list(owl.query(cmp_q, initBindings={
            "code": TG[code], "dispatch": TG[dispatch], "destination": TG[destination],
        }))
        out: dict[str, dict] = {}
        for r in rows:
            agr = str(r["agreement"]).split("#")[-1]
            entry = out.setdefault(agr, {
                "facility": str(r["facilityCode"]),
                "back_to_back": str(r["backToBackAllowed"]).lower() == "true",
                "psr_level": str(r["psrLevel"]),
                "rule": str(r["conciseRule"]),
                "docs": set(),
                "duty": str(r["dutyPercent"]) if r["dutyPercent"] is not None else None,
            })
            entry["docs"].add(str(r["originDocType"]).split("#")[-1])
        return out

    # Thailand -> Indonesia for steel 7209.26.10: ATIGA (SUBHEADING + 0.00% duty) & RCEP (HEADING) + ACFTA (GENERAL)
    th_steel = summarize("hs-72092610", "thailand")
    assert set(th_steel) == {"atiga", "rcep", "acfta"}
    assert th_steel["atiga"]["psr_level"] == "SUBHEADING"
    assert th_steel["atiga"]["facility"] == "06"
    assert th_steel["atiga"]["duty"] == "0.00"
    assert th_steel["rcep"]["psr_level"] == "HEADING"
    assert th_steel["rcep"]["facility"] == "69"
    assert "72.08" in th_steel["rcep"]["rule"]
    assert th_steel["acfta"]["psr_level"] == "GENERAL"

    # China -> Indonesia for chenille fabric 5801.36.10: ACFTA (CHAPTER, Form E) & RCEP (CHAPTER, Form RCEP), NOT ATIGA!
    cn_fab = summarize("hs-58013610", "china")
    assert set(cn_fab) == {"acfta", "rcep"}
    assert cn_fab["acfta"]["psr_level"] == "CHAPTER"
    assert cn_fab["acfta"]["docs"] == {"acftaFormE"}
    assert cn_fab["acfta"]["facility"] == "54"
    assert cn_fab["rcep"]["psr_level"] == "CHAPTER"
    assert cn_fab["rcep"]["rule"] == "CC"
    assert "rcepFormRcep" in cn_fab["rcep"]["docs"]

    # Japan -> Indonesia for medicament 3004.10.15: IJEPA (SUBHEADING, no Back-to-Back, Form JIEPA) & RCEP (HEADING)
    jp_med = summarize("hs-30041015", "japan")
    assert set(jp_med) == {"ijepa", "rcep"}
    assert jp_med["ijepa"]["psr_level"] == "SUBHEADING"
    assert jp_med["ijepa"]["facility"] == "56"
    assert jp_med["ijepa"]["back_to_back"] is False
    assert jp_med["ijepa"]["docs"] == {"ijepaFormJiepa"}
    assert jp_med["rcep"]["psr_level"] == "HEADING"
    assert jp_med["rcep"]["back_to_back"] is True

    # Verify JDIH Multi-FTA temporal revocation/amendment chain for IJEPA tariff PMKs:
    # 50/PMK.010/2022 -> revoked by PMK 60 TAHUN 2026 (2026-08-01) -> amended by PMK 69 TAHUN 2026 (2026-10-02)
    active_q = prepare_local_query(canonical_kb.query_catalog["active-regulations"].sparql, "SelectQuery")
    active_july = {str(r["number"]) for r in owl.query(active_q, initBindings={"asOfDate": RDFLiteral("2026-07-01")}) if r["number"]}
    active_aug = {str(r["number"]) for r in owl.query(active_q, initBindings={"asOfDate": RDFLiteral("2026-08-15")}) if r["number"]}
    assert "50/PMK.010/2022" in active_july
    assert "PMK 60 TAHUN 2026" not in active_july
    assert "50/PMK.010/2022" not in active_aug
    assert "PMK 60 TAHUN 2026" in active_aug

    lineage_q = prepare_local_query(canonical_kb.query_catalog["regulation-lineage"].sparql, "SelectQuery")
    rcep_origin_lineage = list(owl.query(lineage_q, initBindings={"document": TG["source-id-asal-rcep"]}))
    assert any(r["acting"] == TG["source-id-ska-dab"] and r["kind"] == TG.relPartiallyRevokes for r in rcep_origin_lineage)


def test_scenario_service_branches_steel_shipment_from_atiga_to_rcep(canonical_kb, classpath, tmp_path):
    base_report, service, revised = _assess_fta_case(
        tmp_path / "scenario", canonical_kb, classpath,
        agreement="atiga", dispatch_country="thailand", product_hs="hs-72092610",
        origin_doc_type="atigaFormD", material_hs="hs-72111313", rvc_percent="35.00",
    )
    assert {f.policy_id: f.status for f in base_report.domain_findings}["atiga-psr-overall"] == "NOT_SATISFIED"

    # Branch the exact same shipment into RCEP with an e-Form RCEP document!
    rcep_context = replace(revised.context, agreement_iri=str(TG.rcep))
    rcep_doc_triples = tuple(parse_turtle((PREFIXES + """
ex:rcepDoc a tg:Document ; tg:documentType tg:rcepElectronicFormRcep ;
    tg:documentSubject ex:product ; tg:documentJurisdiction tg:jurisdictionIndonesiaCustoms ;
    tg:documentShipment ex:shipment ; tg:documentBatch ex:batch ;
    tg:documentIssuingCountry tg:thailand ; tg:submittedForPreference true ;
    tg:documentValidFrom "2025-01-01T00:00:00Z"^^xsd:dateTime ;
    tg:documentValidUntil "2027-01-01T00:00:00Z"^^xsd:dateTime .
""").encode("utf-8"), str(EX)))
    rcep_claim = EvidenceClaim(
        str(EX.rcepDocClaim), str(EX.product), str(TG.hasSupportingDocument), str(EX.rcepDoc),
        EX.rcepDoc, verification_status="VERIFIED",
    )
    diff = ScenarioService(service).evaluate_scenario(
        base_report.run_id,
        FactPatch(
            "Switch scheme from ATIGA to RCEP",
            add_triples=rcep_doc_triples,
            evidence_claims=(*revised.evidence_claims, rcep_claim),
            evidence_scope_closed=True,
            context=rcep_context,
        ),
    )
    assert ("agreement_iri", str(TG.atiga), str(TG.rcep)) in diff.context_changes
    st_rcep = {f.policy_id: f.status for f in diff.scenario_report.domain_findings}
    assert st_rcep["rcep-psr-ctc"] == "SATISFIED"
    assert st_rcep["rcep-psr-overall"] == "SATISFIED"
    assert st_rcep["rcep-psr-origin-evidence"] == "SATISFIED"
