"""Expert-level Multi-FTA Rules of Origin tests: Regional Cumulation, De Minimis (FOB & Weight), and Minimal Operations (v0.9.0)."""

from datetime import datetime
from pathlib import Path
import os
import re
import shutil

import pytest
from rdflib import Namespace, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext, AssessmentRequest, EvidenceClaim, FactPatch
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb
from tradegraph.kb.rdf import parse_turtle
from tradegraph.reasoning.client import ReasoningClient


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"
EX = Namespace("http://www.semanticweb.org/tradegraph/expert-roo-test#")
PREFIXES = f"""
@prefix ex: <{EX}> .
@prefix tg: <{TG}> .
@prefix xsd: <{XSD}> .
"""


def _resolve_java_classpath(tmp_path_factory) -> str:
    if shutil.which("mvn"):
        return ReasoningClient.build_classpath(
            ROOT / "semantic" / "pom.xml", tmp_path_factory.mktemp("roo-cp") / "classpath.txt",
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


def _assess_expert_case(
    tmp_path: Path,
    kb,
    classpath: str,
    *,
    agreement: str,
    dispatch_country: str,
    product_hs: str,
    origin_doc_type: str,
    material_hs: str,
    material_origin_country: str | None = None,
    material_origin_verified: bool | None = None,
    rvc_percent: str | None = None,
    de_minimis_fob: tuple[str, str] | None = None,
    de_minimis_weight: tuple[str, str] | None = None,
    performed_operations: tuple[str, ...] | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = CaseRepository(tmp_path / f"{agreement}-{product_hs}.sqlite3")
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

    cumul_triples = ""
    if material_origin_country is not None:
        cumul_triples += f" ; tg:materialOriginCountry tg:{material_origin_country}"
    if material_origin_verified is not None:
        cumul_triples += f" ; tg:materialOriginProofVerified {'true' if material_origin_verified else 'false'}"

    facts = [f"""
ex:usd a tg:Currency .
ex:product tg:hasTariffClassification tg:{product_hs} ; tg:hasBatch ex:batch .
ex:batch tg:producedAt ex:facility ; tg:producedBy ex:producer ; tg:usesMaterial ex:material .
ex:material tg:hasTariffClassification tg:{material_hs} .
ex:facility tg:withinLocation tg:{dispatch_country} .
ex:shipment tg:containsGoods ex:product ; tg:shipmentDispatchCountry tg:{dispatch_country} ;
    tg:shipmentDestinationCountry tg:indonesia ; tg:cargoCategory tg:regularCargo ;
    tg:routeKind tg:directRoute ; tg:shipmentRouteClosed true .
{rev} tg:revisionShipment ex:shipment .
ex:importerAssignment tg:roleActor ex:importer ; tg:hasActorRole tg:roleImporter ; tg:assignedInRevision {rev} .
ex:bom a tg:ComponentList ; tg:componentListForBatch ex:batch ;
    tg:componentListClosed true ; tg:componentEntry ex:entry .
ex:entry tg:entryMaterial ex:material ; tg:nonOriginatingInput true {cumul_triples} .
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
    add_doc("bomDoc", "bomStatement", "; tg:documentComponentList ex:bom")

    if rvc_percent is not None:
        add_doc("rvcDoc", "rvcWorksheet", f"; tg:recordedRvcPercent {rvc_percent}")

    if de_minimis_fob is not None or de_minimis_weight is not None:
        dm_parts = ["; tg:documentComponentList ex:bom"]
        if de_minimis_fob is not None:
            fob, failed_val = de_minimis_fob
            dm_parts.append(
                f"; tg:valuationScopeClosed true ; tg:valuationCurrency ex:usd "
                f"; tg:fobValue {fob} ; tg:failedCtcMaterialValue {failed_val}"
            )
        if de_minimis_weight is not None:
            total_wt, failed_wt = de_minimis_weight
            dm_parts.append(
                f"; tg:weightScopeClosed true ; tg:totalProductWeight {total_wt} "
                f"; tg:failedCtcMaterialWeight {failed_wt}"
            )
        add_doc("dmDoc", "deMinimisValueStatement", " ".join(dm_parts))

    if performed_operations is not None:
        ops_csv = ", ".join(f"tg:{op}" for op in performed_operations)
        add_doc("procOpsDoc", "productionProcessStatement", f"; tg:processScopeClosed true ; tg:performedOperation {ops_csv}")

    triples = tuple(parse_turtle((PREFIXES + "\n".join(facts)).encode("utf-8"), str(EX)))
    revised = repo.revise_case(
        base.case_id, base.revision,
        FactPatch("expert roo test", add_triples=triples, evidence_claims=tuple(claims), evidence_scope_closed=True),
    )
    service = AssessmentService(repo, ReasoningClient(kb, classpath))
    return service.assess(AssessmentRequest(kb.snapshot.sha256, revised.case_id, revised.revision))


def test_regional_cumulation_flips_origin_across_atiga_acfta_and_rcep(canonical_kb, classpath, tmp_path):
    # A. Cold-rolled steel 7209.26.10 made in Thailand from JAPANESE originating 7208.54.10 hot-rolled coil (RVC 30%):
    # Both ATIGA & RCEP exclude 7208 from CTH, BUT Japan is an RCEP Party (cumulates!) and NOT an ATIGA Party!
    st_atiga = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "jp-coil-atiga", canonical_kb, classpath,
        agreement="atiga", dispatch_country="thailand", product_hs="hs-72092610",
        origin_doc_type="atigaFormD", material_hs="hs-72085410",
        material_origin_country="japan", material_origin_verified=True, rvc_percent="30.00",
    ).domain_findings}
    st_rcep = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "jp-coil-rcep", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-72092610",
        origin_doc_type="rcepFormRcep", material_hs="hs-72085410",
        material_origin_country="japan", material_origin_verified=True, rvc_percent="30.00",
    ).domain_findings}
    assert st_atiga["atiga-psr-ctc"] == "NOT_SATISFIED"
    assert st_atiga["atiga-psr-overall"] == "NOT_SATISFIED"
    assert st_rcep["rcep-psr-ctc"] == "SATISFIED"
    assert st_rcep["rcep-psr-overall"] == "SATISFIED"

    # Unverified Japanese origin proof MUST NOT cumulate under RCEP!
    st_rcep_unver = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "jp-coil-rcep-unver", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-72092610",
        origin_doc_type="rcepFormRcep", material_hs="hs-72085410",
        material_origin_country="japan", material_origin_verified=False, rvc_percent="30.00",
    ).domain_findings}
    assert st_rcep_unver["rcep-psr-ctc"] == "NOT_SATISFIED"
    assert st_rcep_unver["rcep-psr-overall"] == "NOT_SATISFIED"

    # B. Fabric 5801.36.10 made in Thailand from CHINESE vs JAPANESE originating same-heading 5801.36.90 (RVC 30%):
    # Under ACFTA (Ch.58 CTH or RVC40): China cumulates (PASS!), Japan does NOT cumulate (FAIL!).
    st_acfta_cn = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "cn-fab-acfta", canonical_kb, classpath,
        agreement="acfta", dispatch_country="thailand", product_hs="hs-58013610",
        origin_doc_type="acftaFormE", material_hs="hs-58013690",
        material_origin_country="china", material_origin_verified=True, rvc_percent="30.00",
    ).domain_findings}
    st_acfta_jp = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "jp-fab-acfta", canonical_kb, classpath,
        agreement="acfta", dispatch_country="thailand", product_hs="hs-58013610",
        origin_doc_type="acftaFormE", material_hs="hs-58013690",
        material_origin_country="japan", material_origin_verified=True, rvc_percent="30.00",
    ).domain_findings}
    assert st_acfta_cn["acfta-psr-ctc"] == "SATISFIED"
    assert st_acfta_cn["acfta-psr-overall"] == "SATISFIED"
    assert st_acfta_jp["acfta-psr-ctc"] == "NOT_SATISFIED"
    assert st_acfta_jp["acfta-psr-overall"] == "NOT_SATISFIED"


def test_multi_fta_de_minimis_value_vs_textile_weight_and_ijepa_exclusions(canonical_kb, classpath, tmp_path):
    # A. Textile 5801.36.10 (Ch.58) with same-chapter input (fails CC): 14% of FOB (>10%), but 8.5% of weight (<=10%)!
    # RCEP allows 10% weight for Ch.50-63 -> RESCUES CC & overall PSR!
    # ATIGA allows 10% FOB ONLY -> FAILS De Minimis & CTC!
    st_rcep_wt = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "dm-rcep-wt", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-58013610",
        origin_doc_type="rcepFormRcep", material_hs="hs-58041011", rvc_percent="30.00",
        de_minimis_fob=("1000.00", "140.00"), de_minimis_weight=("100.00", "8.50"),
    ).domain_findings}
    st_atiga_wt = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "dm-atiga-wt", canonical_kb, classpath,
        agreement="atiga", dispatch_country="thailand", product_hs="hs-58013610",
        origin_doc_type="atigaFormD", material_hs="hs-58041011", rvc_percent="30.00",
        de_minimis_fob=("1000.00", "140.00"), de_minimis_weight=("100.00", "8.50"),
    ).domain_findings}
    assert st_rcep_wt["rcep-psr-de-minimis"] == "SATISFIED"
    assert st_rcep_wt["rcep-psr-ctc"] == "SATISFIED"
    assert st_rcep_wt["rcep-psr-overall"] == "SATISFIED"
    assert st_atiga_wt["atiga-psr-de-minimis"] == "NOT_SATISFIED"
    assert st_atiga_wt["atiga-psr-ctc"] == "NOT_SATISFIED"

    # B. Animal feed 2309.10.10 (Ch.23) from Japan with 8% FOB same-chapter 2306 input (RVC 45%):
    # IJEPA De Minimis EXCLUDES Ch.01-27 (only covers Ch.28-49 & 64-97) -> NOT_APPLICABLE, fails CC+QVC40!
    st_ijepa_ch23 = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "dm-ijepa-ch23", canonical_kb, classpath,
        agreement="ijepa", dispatch_country="japan", product_hs="hs-23091010",
        origin_doc_type="ijepaFormJiepa", material_hs="hs-23061000", rvc_percent="45.00",
        de_minimis_fob=("1000.00", "80.00"),
    ).domain_findings}
    assert st_ijepa_ch23["ijepa-psr-de-minimis"] == "NOT_APPLICABLE"
    assert st_ijepa_ch23["ijepa-psr-ctc"] == "NOT_SATISFIED"
    assert st_ijepa_ch23["ijepa-psr-overall"] == "NOT_SATISFIED"

    # C. Medicament 3004.10.15 (Ch.30, inside IJEPA Ch.28-49) with excluded 3003.10.10 input = 8% FOB (RVC 30%):
    # IJEPA 10% FOB De Minimis RESCUES CTC & overall PSR; at 12% FOB it FAILS!
    st_ijepa_ch30_ok = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "dm-ijepa-ch30-ok", canonical_kb, classpath,
        agreement="ijepa", dispatch_country="japan", product_hs="hs-30041015",
        origin_doc_type="ijepaFormJiepa", material_hs="hs-30031010", rvc_percent="30.00",
        de_minimis_fob=("1000.00", "80.00"),
    ).domain_findings}
    st_ijepa_ch30_fail = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "dm-ijepa-ch30-fail", canonical_kb, classpath,
        agreement="ijepa", dispatch_country="japan", product_hs="hs-30041015",
        origin_doc_type="ijepaFormJiepa", material_hs="hs-30031010", rvc_percent="30.00",
        de_minimis_fob=("1000.00", "120.00"),
    ).domain_findings}
    assert st_ijepa_ch30_ok["ijepa-psr-de-minimis"] == "SATISFIED"
    assert st_ijepa_ch30_ok["ijepa-psr-ctc"] == "SATISFIED"
    assert st_ijepa_ch30_ok["ijepa-psr-overall"] == "SATISFIED"
    assert st_ijepa_ch30_fail["ijepa-psr-de-minimis"] == "NOT_SATISFIED"
    assert st_ijepa_ch30_fail["ijepa-psr-ctc"] == "NOT_SATISFIED"


def test_minimal_operations_defeasibly_disqualifies_passing_ctc(canonical_kb, classpath, tmp_path):
    # A. Travel set 9605.00.00 from Japan using Ch.84 component (CC satisfied on paper!),
    # BUT factory only assembled components into a set (minimalSimpleSetAssembly, PMK 73/2021 Lamp. A IV.2.f):
    st_set_fail = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "min-ijepa-fail", canonical_kb, classpath,
        agreement="ijepa", dispatch_country="japan", product_hs="hs-96050000",
        origin_doc_type="ijepaFormJiepa", material_hs="hs-84139130",
        performed_operations=("minimalSimpleSetAssembly", "minimalPackaging"),
    ).domain_findings}
    assert st_set_fail["ijepa-psr-minimal-ops"] == "NOT_SATISFIED"
    assert st_set_fail["ijepa-psr-ctc"] == "NOT_SATISFIED"
    assert st_set_fail["ijepa-psr-overall"] == "NOT_SATISFIED"

    # With substantialManufacturing + packaging -> NOT minimal-only -> PASSES!
    st_set_ok = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "min-ijepa-ok", canonical_kb, classpath,
        agreement="ijepa", dispatch_country="japan", product_hs="hs-96050000",
        origin_doc_type="ijepaFormJiepa", material_hs="hs-84139130",
        performed_operations=("substantialManufacturing", "minimalPackaging"),
    ).domain_findings}
    assert st_set_ok["ijepa-psr-minimal-ops"] == "SATISFIED"
    assert st_set_ok["ijepa-psr-ctc"] == "SATISFIED"
    assert st_set_ok["ijepa-psr-overall"] == "SATISFIED"

    # B. Dishwasher 8422.11.00 from Thailand under RCEP (CTSH passes on paper),
    # BUT factory only did labeling & packaging (PMK 209/2022 Lamp. A IV.2.b,d) -> DISQUALIFIED!
    st_rcep_min = {f.policy_id: f.status for f in _assess_expert_case(
        tmp_path / "min-rcep-fail", canonical_kb, classpath,
        agreement="rcep", dispatch_country="thailand", product_hs="hs-84221100",
        origin_doc_type="rcepFormRcep", material_hs="hs-84229010", rvc_percent="45.00",
        performed_operations=("minimalLabeling", "minimalPackaging"),
    ).domain_findings}
    assert st_rcep_min["rcep-psr-minimal-ops"] == "NOT_SATISFIED"
    assert st_rcep_min["rcep-psr-ctc"] == "NOT_SATISFIED"
    assert st_rcep_min["rcep-psr-overall"] == "NOT_SATISFIED"
