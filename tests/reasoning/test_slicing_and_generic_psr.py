"""Verify reference slicing scalability and generalized multi-sector ATIGA PSR reasoning."""

from datetime import datetime
from pathlib import Path
import os
import re
import shutil

import pytest
from rdflib import Graph, Namespace, RDF, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext, AssessmentRequest, EvidenceClaim, FactPatch
from tradegraph.graph.store import project_case_graphs
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb, select_rules
from tradegraph.kb.rdf import parse_turtle
from tradegraph.reasoning.client import ReasoningClient
from tradegraph.reasoning.slicing import slice_owl_for_case


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"
STAGE1_TTL = ROOT / "sources" / "staging" / "stage1.ttl"
EX = Namespace("http://www.semanticweb.org/tradegraph/psr-test#")
PREFIXES = f"""
@prefix ex: <{EX}> .
@prefix tg: <{TG}> .
@prefix xsd: <{XSD}> .
"""


def _resolve_java_classpath(tmp_path_factory) -> str:
    if shutil.which("mvn"):
        return ReasoningClient.build_classpath(
            ROOT / "semantic" / "pom.xml", tmp_path_factory.mktemp("psr-cp") / "classpath.txt",
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
def merged_stage1_kb(tmp_path_factory):
    # Canonical KB v0.7.0 already embeds Stage 1 PSRs, tariffs, and JDIH legal relations.
    graph = parse_turtle(CANONICAL_KB.read_bytes(), CANONICAL_KB.resolve().as_uri())
    # Add 8-digit test HS codes for subheadings tested across all PSR archetypes
    extra = parse_turtle(b"""
@prefix : <http://www.semanticweb.org/tradegraph/kb#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
:hs-01012100 a owl:NamedIndividual, :HSCode ; :codeValue "0101.21.00" ; :tariffSubheading :hs6-010121 .
:hs-58013600 a owl:NamedIndividual, :HSCode ; :codeValue "5801.36.00" ; :tariffSubheading :hs6-580136 .
:hs-72092610 a owl:NamedIndividual, :HSCode ; :codeValue "7209.26.10" ; :tariffSubheading :hs6-720926 .
:hs-84221100 a owl:NamedIndividual, :HSCode ; :codeValue "8422.11.00" ; :tariffSubheading :hs6-842211 .
:hs6-842290 a owl:NamedIndividual, :HSSubheading ; :subheadingCode "842290" ; :subheadingHeading :hs4-8422 .
:hs-84229010 a owl:NamedIndividual, :HSCode ; :codeValue "8422.90.10" ; :tariffSubheading :hs6-842290 .
:hs-61012000 a owl:NamedIndividual, :HSCode ; :codeValue "6101.20.00" ; :tariffSubheading :hs6-610120 .
:hs-15011000 a owl:NamedIndividual, :HSCode ; :codeValue "1501.10.00" ; :tariffSubheading :hs6-150110 .
""", "http://www.semanticweb.org/tradegraph/psr-test/extra")
    for triple in extra:
        graph.add(triple)
    out = tmp_path_factory.mktemp("stage1-kb") / "merged.ttl"
    graph.serialize(out, format="turtle", encoding="utf-8")
    return compile_kb(out, ENGINE_PROFILE_ID)


def _assess_generic_case(
    tmp_path,
    kb,
    classpath: str,
    *,
    product_hs: str,
    material_hs: str | None = None,
    rvc_percent: str | None = None,
    bom_closed: bool = True,
    wo_confirmed: bool | None = None,
    process_kind: str | None = None,
    process_satisfied: bool | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = CaseRepository(tmp_path / f"case-{product_hs}.sqlite3")
    context = AssessmentContext(
        kb.snapshot.sha256,
        ENGINE_PROFILE_ID,
        jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
        procedure_iri=str(TG.importForUse),
        agreement_iri=str(TG.atiga),
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
ex:facility tg:withinLocation tg:thailand .
ex:shipment tg:containsGoods ex:product ; tg:shipmentDispatchCountry tg:thailand ;
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
    add_doc("formd", "atigaFormD", "; tg:documentIssuingCountry tg:thailand ; tg:submittedForPreference true")

    if material_hs is not None:
        facts.append(f"""
ex:batch tg:usesMaterial ex:material .
ex:material tg:hasTariffClassification tg:{material_hs} .
ex:bom a tg:ComponentList ; tg:componentListForBatch ex:batch ;
    tg:componentListClosed {"true" if bom_closed else "false"} ; tg:componentEntry ex:entry .
ex:entry tg:entryMaterial ex:material ; tg:nonOriginatingInput true .
""")
        add_doc("bomDoc", "atigaBomStatement", "; tg:documentComponentList ex:bom")

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
        FactPatch("generic psr test", add_triples=triples, evidence_claims=tuple(claims), evidence_scope_closed=True),
    )
    service = AssessmentService(repo, ReasoningClient(kb, classpath))
    return service.assess(AssessmentRequest(kb.snapshot.sha256, revised.case_id, revised.revision))


def test_reference_slicing_prunes_unrelated_bulk_hs_and_keeps_case_closure(merged_stage1_kb, tmp_path):
    schema = merged_stage1_kb.dataset.graph(merged_stage1_kb.owl_projection_ref)
    repo = CaseRepository(tmp_path / "slice.sqlite3")
    context = AssessmentContext(
        merged_stage1_kb.snapshot.sha256, ENGINE_PROFILE_ID,
        jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
        procedure_iri=str(TG.importForUse), agreement_iri=str(TG.atiga),
        transaction_at=datetime.fromisoformat("2025-09-15T13:00:00+07:00"),
        product_iri=str(EX.product),
    )
    case = repo.create_case(PREFIXES + "ex:product tg:hasTariffClassification tg:hs-72092610 .", context)
    graphs = project_case_graphs(case)
    rules, _ = select_rules(merged_stage1_kb, context)

    sliced = slice_owl_for_case(schema, graphs.facts, graphs.context, rules)
    # Massive reduction: stage1 merged schema (>8,000 triples) slices down below 2,000 triples
    assert len(schema) > 7500
    assert len(sliced) < len(schema) * 0.30

    # Target HS 4-level hierarchy + PSR + heading exclusions are preserved
    assert (TG["hs-72092610"], TG.tariffSubheading, TG["hs6-720926"]) in sliced
    assert (TG["hs6-720926"], TG.subheadingHeading, TG["hs4-7209"]) in sliced
    assert (TG["hs4-7209"], TG.headingChapter, TG["hs2-72"]) in sliced
    assert (TG["psr-atiga-720926"], TG.psrForSubheading, TG["hs6-720926"]) in sliced
    assert (TG["psr-atiga-720926-alt-2"], TG.excludesHeading, TG["hs4-7208"]) in sliced
    # Unrelated staged HS codes & PSRs are pruned
    assert (TG["hs6-580136"], RDF.type, TG.HSSubheading) not in sliced
    assert (TG["psr-atiga-580136"], RDF.type, TG.ProductSpecificRule) not in sliced


def test_steel_720926_cth_with_heading_exclusions_and_rvc_disjunction(merged_stage1_kb, classpath, tmp_path):
    # 1. Input from excluded heading 7208 + low RVC -> both alternatives fail -> overall NOT_SATISFIED
    r_fail = _assess_generic_case(
        tmp_path / "fail", merged_stage1_kb, classpath,
        product_hs="hs-72092610", material_hs="hs-72085410", rvc_percent="35.00",
    )
    assert r_fail.execution_status == "CONVERGED", r_fail.diagnostics
    # Verify OWL 2 DL property chains derived 4-digit heading and 2-digit chapter
    inferred = {f.triple for f in r_fail.findings}
    assert (EX.product, TG.classifiedSubheading, TG["hs6-720926"]) in inferred
    assert (EX.product, TG.classifiedHeading, TG["hs4-7209"]) in inferred
    assert (EX.product, TG.classifiedChapter, TG["hs2-72"]) in inferred
    assert (EX.material, TG.classifiedHeading, TG["hs4-7208"]) in inferred

    st_fail = {f.policy_id: f.status for f in r_fail.domain_findings}
    assert st_fail["atiga-psr-wo"] == "NOT_APPLICABLE"
    assert st_fail["atiga-psr-process"] == "NOT_APPLICABLE"
    assert st_fail["atiga-psr-ctc"] == "NOT_SATISFIED"
    assert st_fail["atiga-psr-rvc"] == "NOT_SATISFIED"
    assert st_fail["atiga-psr-overall"] == "NOT_SATISFIED"

    # 2. Same excluded input 7208, but RVC missing -> overall MUST remain UNDETERMINED (open world)
    r_unk = _assess_generic_case(
        tmp_path / "unk", merged_stage1_kb, classpath,
        product_hs="hs-72092610", material_hs="hs-72085410", rvc_percent=None,
    )
    st_unk = {f.policy_id: f.status for f in r_unk.domain_findings}
    assert st_unk["atiga-psr-ctc"] == "NOT_SATISFIED"
    assert st_unk["atiga-psr-rvc"] == "UNDETERMINED"
    assert st_unk["atiga-psr-overall"] == "UNDETERMINED"

    # 3. Excluded input 7208, but RVC = 40.00% -> RVC alternative satisfies overall PSR
    r_rvc = _assess_generic_case(
        tmp_path / "rvc", merged_stage1_kb, classpath,
        product_hs="hs-72092610", material_hs="hs-72085410", rvc_percent="40.00",
    )
    st_rvc = {f.policy_id: f.status for f in r_rvc.domain_findings}
    assert st_rvc["atiga-psr-ctc"] == "NOT_SATISFIED"
    assert st_rvc["atiga-psr-rvc"] == "SATISFIED"
    assert st_rvc["atiga-psr-overall"] == "SATISFIED"

    # 4. Non-excluded heading input (8413) with no RVC -> CTH passes -> overall SATISFIED
    r_cth = _assess_generic_case(
        tmp_path / "cth", merged_stage1_kb, classpath,
        product_hs="hs-72092610", material_hs="hs-84139130", rvc_percent=None,
    )
    st_cth = {f.policy_id: f.status for f in r_cth.domain_findings}
    assert st_cth["atiga-psr-ctc"] == "SATISFIED"
    assert st_cth["atiga-psr-overall"] == "SATISFIED"


def test_textile_580136_process_rule_alternative(merged_stage1_kb, classpath, tmp_path):
    # Same-chapter textile input fails CC, RVC 30% fails RVC40, but Textile Process Rule passes!
    report = _assess_generic_case(
        tmp_path, merged_stage1_kb, classpath,
        product_hs="hs-58013600", material_hs="hs-58013600", rvc_percent="30.00",
        process_kind="criterionTextileProcess", process_satisfied=True,
    )
    assert report.execution_status == "CONVERGED", report.diagnostics
    status = {f.policy_id: f.status for f in report.domain_findings}
    assert status["atiga-psr-ctc"] == "NOT_SATISFIED"
    assert status["atiga-psr-rvc"] == "NOT_SATISFIED"
    assert status["atiga-psr-process"] == "SATISFIED"
    assert status["atiga-psr-overall"] == "SATISFIED"


def test_livestock_010121_wholly_obtained_and_tea_090210_cc(merged_stage1_kb, classpath, tmp_path):
    # WO satisfied
    r_wo = _assess_generic_case(
        tmp_path / "wo-ok", merged_stage1_kb, classpath, product_hs="hs-01012100", wo_confirmed=True,
    )
    st_wo = {f.policy_id: f.status for f in r_wo.domain_findings}
    assert st_wo["atiga-psr-wo"] == "SATISFIED"
    assert st_wo["atiga-psr-rvc"] == "NOT_APPLICABLE"
    assert st_wo["atiga-psr-ctc"] == "NOT_APPLICABLE"
    assert st_wo["atiga-psr-overall"] == "SATISFIED"

    # WO missing -> UNDETERMINED
    r_wo_unk = _assess_generic_case(
        tmp_path / "wo-unk", merged_stage1_kb, classpath, product_hs="hs-01012100", wo_confirmed=None,
    )
    st_wo_unk = {f.policy_id: f.status for f in r_wo_unk.domain_findings}
    assert st_wo_unk["atiga-psr-wo"] == "UNDETERMINED"
    assert st_wo_unk["atiga-psr-overall"] == "UNDETERMINED"

    # Tea 0902.10 (RVC40 or CC): same-chapter coffee input (09) fails CC
    r_cc_fail = _assess_generic_case(
        tmp_path / "tea-same-chap", merged_stage1_kb, classpath,
        product_hs="hs-09021010", material_hs="hs-09012111",
    )
    st_cc_fail = {f.policy_id: f.status for f in r_cc_fail.domain_findings}
    assert st_cc_fail["atiga-psr-ctc"] == "NOT_SATISFIED"
    assert st_cc_fail["atiga-psr-overall"] == "UNDETERMINED"

    # Tea 0902.10: different-chapter input (84) passes CC
    r_cc_pass = _assess_generic_case(
        tmp_path / "tea-diff-chap", merged_stage1_kb, classpath,
        product_hs="hs-09021010", material_hs="hs-84139130",
    )
    st_cc_pass = {f.policy_id: f.status for f in r_cc_pass.domain_findings}
    assert st_cc_pass["atiga-psr-ctc"] == "SATISFIED"
    assert st_cc_pass["atiga-psr-overall"] == "SATISFIED"


def test_slicing_keeps_hermit_fast_on_2000_hs_scale_kb(classpath, tmp_path):
    # Stress test: append 2,000 synthetic HS subheadings + PSRs (>20,000 triples) and run real HermiT
    lines = [CANONICAL_KB.read_text(encoding="utf-8")]
    lines.append("\n# Synthetic scale test entries\n")
    for idx in range(2000):
        ch = f"{10 + (idx // 100):02d}"
        hd = f"{ch}{idx % 100:02d}"
        sub = f"{hd}10"
        code = f"{sub}00"
        lines.append(
            f":hs2-{ch} a owl:NamedIndividual, :HSChapter ; :chapterCode \"{ch}\" .\n"
            f":hs4-{hd} a owl:NamedIndividual, :HSHeading ; :headingCode \"{hd}\" ; :headingChapter :hs2-{ch} .\n"
            f":hs6-{sub} a owl:NamedIndividual, :HSSubheading ; :subheadingCode \"{sub}\" ; :subheadingHeading :hs4-{hd} .\n"
            f":hs-{code} a owl:NamedIndividual, :HSCode ; :codeValue \"{hd}.{sub[4:]}.00\" ; :tariffSubheading :hs6-{sub} .\n"
            f":psr-atiga-{sub} a owl:NamedIndividual, :ProductSpecificRule ; :psrAgreement :atiga ; "
            f":psrForSubheading :hs6-{sub} ; :hasPsrAlternative :psr-atiga-{sub}-alt-1 .\n"
            f":psr-atiga-{sub}-alt-1 a owl:NamedIndividual, :OriginRuleAlternative ; :requiresCriterionKind :criterionCTH .\n"
        )
    scale_path = tmp_path / "scale2000.ttl"
    scale_path.write_text("".join(lines), encoding="utf-8")
    scale_kb = compile_kb(scale_path, ENGINE_PROFILE_ID)
    assert len(scale_kb.dataset.graph(scale_kb.owl_projection_ref)) > 20_000

    report = _assess_generic_case(
        tmp_path / "scale-run", scale_kb, classpath,
        product_hs="hs-10001000", material_hs="hs-84139130",
    )
    assert report.execution_status == "CONVERGED", report.diagnostics
    status = {f.policy_id: f.status for f in report.domain_findings}
    assert status["atiga-psr-ctc"] == "SATISFIED"
    assert status["atiga-psr-overall"] == "SATISFIED"


def test_conjunctive_rvc35_ctsh_garment_cut_and_sewn_and_refining(merged_stage1_kb, classpath, tmp_path):
    # 1. Dishwasher 8422.11 (RVC40 or CTH or RVC35+CTSH): same-heading part 8422.90 + RVC 36% passes RVC35+CTSH!
    r_pass = _assess_generic_case(
        tmp_path / "rvc35-pass", merged_stage1_kb, classpath,
        product_hs="hs-84221100", material_hs="hs-84229010", rvc_percent="36.00",
    )
    st_pass = {f.policy_id: f.status for f in r_pass.domain_findings}
    assert st_pass["atiga-psr-overall"] == "SATISFIED"

    # Same-heading part 8422.90 + RVC 34% fails RVC40, CTH, and RVC35+CTSH -> NOT_SATISFIED
    r_fail = _assess_generic_case(
        tmp_path / "rvc35-fail", merged_stage1_kb, classpath,
        product_hs="hs-84221100", material_hs="hs-84229010", rvc_percent="34.00",
    )
    st_fail = {f.policy_id: f.status for f in r_fail.domain_findings}
    assert st_fail["atiga-psr-overall"] == "NOT_SATISFIED"

    # 2. Garment 6101.20 (RVC40 or CC and cut and sewn in any AMS or textile process rule):
    # CC alone without cut-and-sewn evidence MUST stay UNDETERMINED for overall PSR!
    r_cc_only = _assess_generic_case(
        tmp_path / "garment-cc-only", merged_stage1_kb, classpath,
        product_hs="hs-61012000", material_hs="hs-58013600",
    )
    st_cc_only = {f.policy_id: f.status for f in r_cc_only.domain_findings}
    assert st_cc_only["atiga-psr-ctc"] == "SATISFIED"
    assert st_cc_only["atiga-psr-overall"] == "UNDETERMINED"

    # CC + cut-and-sewn evidence satisfies the conjunctive alternative!
    r_garment_ok = _assess_generic_case(
        tmp_path / "garment-ok", merged_stage1_kb, classpath,
        product_hs="hs-61012000", material_hs="hs-58013600",
        process_kind="criterionCutAndSewnAMS", process_satisfied=True,
    )
    st_garment_ok = {f.policy_id: f.status for f in r_garment_ok.domain_findings}
    assert st_garment_ok["atiga-psr-process"] == "SATISFIED"
    assert st_garment_ok["atiga-psr-overall"] == "SATISFIED"

    # 3. Refined oil/fat 1501.10 (RVC40 or CC or refining): refining process alone satisfies PSR!
    r_ref = _assess_generic_case(
        tmp_path / "refining-ok", merged_stage1_kb, classpath,
        product_hs="hs-15011000", process_kind="criterionRefiningProcess", process_satisfied=True,
    )
    st_ref = {f.policy_id: f.status for f in r_ref.domain_findings}
    assert st_ref["atiga-psr-process"] == "SATISFIED"
    assert st_ref["atiga-psr-overall"] == "SATISFIED"


def test_temporal_legal_validity_and_regulation_lineage_queries():
    from rdflib import Literal as RDFLiteral
    from tradegraph.kb.queries import prepare_local_query

    kb = compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)
    owl = kb.dataset.graph(kb.owl_projection_ref)

    # CQ17 regulation-lineage for PMK 131/2020: amended by PMK 81/2022 & partially revoked by PMK 35/2023
    lineage_q = prepare_local_query(kb.query_catalog["regulation-lineage"].sparql, "SelectQuery")
    pmk131_rows = list(owl.query(lineage_q, initBindings={"document": TG["source-id-asal-atiga"]}))
    pmk131_pairs = {(row["acting"], row["kind"], str(row["effectiveDate"])) for row in pmk131_rows}
    assert (TG["source-id-asal-atiga-amend"], TG.relAmends, "2022-05-17") in pmk131_pairs
    assert (TG["source-id-ska-dab"], TG.relPartiallyRevokes, "2023-04-28") in pmk131_pairs

    # CQ17 regulation-lineage for Permendag 16/2025: amended by JDIH-scraped Permendag 18 Tahun 2026!
    permendag16_rows = list(owl.query(lineage_q, initBindings={"document": TG["source-id-impor-general"]}))
    assert any(row["kind"] == TG.relAmends and str(row["effectiveDate"]) == "2026-06-04" for row in permendag16_rows)

    # CQ04 active-regulations temporal filter: PMK 61/2026 (eff. 2026-08-01) is absent on 2025-09-15 and active on 2026-08-15
    active_q = prepare_local_query(kb.query_catalog["active-regulations"].sparql, "SelectQuery")
    active_2025 = {str(row["number"]) for row in owl.query(active_q, initBindings={"asOfDate": RDFLiteral("2025-09-15")}) if row["number"]}
    active_2026 = {str(row["number"]) for row in owl.query(active_q, initBindings={"asOfDate": RDFLiteral("2026-08-15")}) if row["number"]}
    assert "PMK 35 TAHUN 2023" in active_2025
    assert "Permendag 16 Tahun 2025" in active_2025
    assert "PMK 61 TAHUN 2026" not in active_2025
    assert "PMK 61 TAHUN 2026" in active_2026
    assert "51/PMK.010/2022" not in active_2026
