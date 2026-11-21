from datetime import datetime
import json
from pathlib import Path

import pytest
from rdflib import Namespace, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentContext, AssessmentRequest, EvidenceClaim, FactPatch
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG, compile_kb
from tradegraph.kb.rdf import parse_turtle
from tradegraph.reasoning.client import ReasoningClient


ROOT = Path(__file__).resolve().parents[2]
KB = ROOT / "knowledge/tradegraph.ttl"
EX = Namespace("http://www.semanticweb.org/tradegraph/domain-test#")
CASES = tuple(json.loads(line) for line in (ROOT / "eval/cases.jsonl").read_text(encoding="utf-8").splitlines())
PREFIXES = f"""
@prefix ex: <{EX}> .
@prefix tg: <{TG}> .
@prefix xsd: <{XSD}> .
"""


@pytest.fixture(scope="session")
def compiled():
    return compile_kb(KB, ENGINE_PROFILE_ID)


@pytest.fixture(scope="session")
def classpath(tmp_path_factory):
    return ReasoningClient.build_classpath(
        ROOT / "semantic/pom.xml", tmp_path_factory.mktemp("domain-build") / "classpath.txt",
    )


@pytest.fixture
def run_case(tmp_path, compiled, classpath):
    repository = CaseRepository(tmp_path / "cases.sqlite3")

    def run(spec, *, kb=compiled, include_provenance=False):
        scenario = spec["scenario"]
        variant = spec["variant"]
        coffee = scenario == "coffee"
        jurisdiction = "jurisdictionSingapore" if coffee and variant != "id" else "jurisdictionIndonesiaCustoms"
        procedure = {
            "pump": "importForUse", "exhibition": "temporaryImport", "coffee": "commercialExport",
        }[scenario]
        if variant in {"reexport", "late"}:
            procedure = "reExportTemporaryImport"
        date = {"outside": "2025-09-16"}.get(variant, "2026-11-15" if coffee else "2025-09-15")
        offset = "+08:00" if jurisdiction == "jurisdictionSingapore" else "+07:00"
        context = AssessmentContext(
            kb.snapshot.sha256, ENGINE_PROFILE_ID, jurisdiction_iri=str(TG[jurisdiction]),
            procedure_iri=str(TG[procedure]), agreement_iri=str(TG.atiga) if scenario == "pump" else None,
            transaction_at=datetime.fromisoformat(f"{date}T13:00:00{offset}"),
            evaluation_at=datetime.fromisoformat("2025-12-01T13:00:00+07:00") if not coffee else None,
            evidence_scope_id="case-documents", product_iri=str(EX.product), batch_iri=str(EX.batch),
        )
        dispatch, destination = ("indonesia", "singapore") if coffee else ("thailand", "indonesia")
        hs = "hs-09012111" if coffee else "hs-84137011"
        base = repository.create_case("", context)
        revision = f"<http://www.semanticweb.org/tradegraph/case/{base.case_id}/revision/2>"
        facts = [f"""
ex:product tg:hasTariffClassification tg:{hs} ; tg:hasBatch ex:batch .
ex:batch tg:usesMaterial ex:material ; tg:producedAt ex:facility ; tg:producedBy ex:producer .
ex:facility tg:withinLocation tg:{dispatch} .
ex:material tg:suppliedBy ex:supplier .
ex:shipment tg:containsGoods ex:product ; tg:shipmentDispatchCountry tg:{dispatch} ;
    tg:shipmentDestinationCountry tg:{destination} ; tg:hasTransportLeg ex:leg ; tg:cargoCategory tg:regularCargo .
ex:leg tg:departsFrom tg:{dispatch} ; tg:arrivesAt tg:{destination} .
{revision} tg:revisionShipment ex:shipment .
ex:importerAssignment tg:roleActor ex:importer ; tg:hasActorRole tg:roleImporter ; tg:assignedInRevision {revision} .
ex:exporterAssignment tg:roleActor ex:exporter ; tg:hasActorRole tg:roleExporter ; tg:assignedInRevision {revision} .
"""]
        claims = []

        def document(name, kind, *, extra="", verified=True, shipment="shipment"):
            facts.append(f"""
ex:{name} a tg:Document ; tg:documentType tg:{kind} ;
    tg:documentSubject ex:product ; tg:documentJurisdiction tg:{jurisdiction} ;
    tg:documentShipment ex:{shipment} ; tg:documentBatch ex:batch ;
    tg:documentValidFrom "2025-01-01T00:00:00Z"^^xsd:dateTime ;
    tg:documentValidUntil "2027-01-01T00:00:00Z"^^xsd:dateTime {extra} .
""")
            claims.append(EvidenceClaim(
                str(EX[f"{name}Claim"]), str(EX.product), str(TG.hasSupportingDocument), str(EX[name]),
                EX[name], verification_status="VERIFIED" if verified else "UNVERIFIED",
            ))

        if scenario == "pump":
            production = variant == "own-production"
            de_minimis = variant.startswith("de-minimis")
            facts.append(f"""
{revision} tg:intendedUse tg:{"productionInputUse" if production else "commercialResaleUse"} .
ex:material tg:hasTariffClassification tg:{"hs-84137011" if variant == "direct-same-subheading" or de_minimis else "hs-84139130"} .
ex:bom a tg:ComponentList ; tg:componentListForBatch ex:batch ;
    tg:componentListClosed {"false" if variant == "partial" else "true"} ; tg:componentEntry ex:entry .
ex:entry tg:entryMaterial ex:material ; tg:nonOriginatingInput true .
""")
            if variant == "invalid-policy-edit":
                facts.append("tg:pumpPsrThreshold tg:minimumRvcPercent 10.00 .")
            document("worksheet", "rvcWorksheet", extra=f"; tg:recordedRvcPercent {spec['rvc']}",
                     verified=variant != "partial")
            document("bomCertificate", "atigaBomStatement", extra="; tg:documentComponentList ex:bom")
            document("formd", "atigaOriginDeclaration" if variant == "origin-declaration" else "atigaFormD",
                     extra="; tg:documentIssuingCountry tg:thailand ; tg:submittedForPreference true",
                     verified=variant != "partial", shipment="otherShipment" if variant == "wrong-shipment" else "shipment")
            document("api", "producerImporterId" if production else "generalImporterId",
                     extra="; tg:documentHolder ex:importer")
            if de_minimis:
                value = "10.00" if variant == "de-minimis-at-limit" else "10.00000000000000000000000000000001"
                document("deMinimis", "deMinimisValueStatement", extra=(
                    f"; tg:fobValue 100.00 ; tg:failedCtcMaterialValue {value} ; tg:valuationCurrency ex:thb ; "
                    "tg:valuationScopeClosed true ; tg:documentComponentList ex:bom"
                ))
            route = "direct" if variant in {"direct-same-subheading", "outside"} else "transit"
            if route == "direct":
                facts.append("ex:shipment tg:routeKind tg:directRoute ; tg:shipmentRouteClosed true .")
            else:
                entered = "true" if variant == "transit-commerce" else "false"
                facts.append(f"""
ex:shipment tg:routeKind tg:transitRoute ; tg:shipmentRouteClosed true ; tg:hasTransitStop ex:stop .
ex:stop tg:transitCountry tg:singapore ; tg:transitJustification tg:transportRequirement ;
    tg:enteredTransitCommerce {entered} ; tg:transitPreservationOnly true .
""")
            if variant != "partial":
                document("transport", "transportRecord",
                         shipment="otherShipment" if variant == "wrong-shipment" else "shipment")
        elif scenario == "exhibition":
            domestic = variant in {"sale", "sale-only", "government"}
            proposed = {"change-approved": "conferenceUse", "sale": "commercialResaleUse",
                        "sale-only": "commercialResaleUse"}.get(variant, "exhibitionUse")
            facts.append(f"""
{revision} tg:intendedUse tg:exhibitionUse ; tg:proposedUse tg:{proposed} ;
    tg:requestedSettlement tg:{"domesticSettlement" if domestic else "reexportSettlement"} .
ex:product tg:identifiableForReexport true ; tg:consumedInUse {"true" if variant == "consumable" else "false"} ;
    tg:essentialChangeInUse false ; tg:importCategory tg:unrestrictedImport .
ex:shipment tg:temporaryUsePermitStart "2025-09-15T00:00:00+07:00"^^xsd:dateTime ;
    tg:temporaryUsePermitEnd "2025-10-15T00:00:00+07:00"^^xsd:dateTime ;
    tg:permitExtended {"true" if variant == "extended" else "false"} ;
    tg:exhibitionCount {2 if variant.startswith("multiple") else 1} .
""")
            document("permit", "temporaryImportPermit", extra="; tg:documentHolder ex:importer")
            document("guarantee", "temporaryImportGuarantee", extra="; tg:documentHolder ex:importer")
            document("plan", "reexportPlan")
            if variant == "multiple":
                document("storage", "exhibitionStorageRecord", extra="; tg:customsSupervisedStorage true")
            if variant == "sale-only":
                facts.append(f"{revision} tg:settlementGround tg:ordinaryCommercialTransfer ; tg:settlementGroundsClosed true .")
            if variant == "government":
                facts.append(f"{revision} tg:settlementGround tg:governmentProject .")
                document("domestic", "domesticSettlementDecision", extra=(
                    "; tg:documentHolder ex:importer ; tg:documentSettlementGround tg:governmentProject"
                ))
            if variant == "change-approved":
                document("changeApproval", "changeOfUseApproval", extra=(
                    '; tg:documentHolder ex:importer ; tg:documentProposedUse tg:conferenceUse ; '
                    'tg:documentEventAt "2025-10-01T10:00:00+07:00"^^xsd:dateTime'
                ))
            if variant in {"reexport", "late"}:
                notice = "2025-10-20" if variant == "late" else "2025-10-10"
                exit_date = "2025-11-30" if variant == "late" else "2025-11-02"
                document("notice", "temporaryReexportNotice", extra=(
                    f'; tg:documentHolder ex:importer ; tg:documentEventAt "{notice}T10:00:00+07:00"^^xsd:dateTime'
                ))
                document("exit", "temporaryReexportRealization", extra=(
                    f'; tg:documentHolder ex:importer ; tg:documentEventAt "{exit_date}T10:00:00+07:00"^^xsd:dateTime'
                ))
        elif variant == "id":
            document("peb", "idExportDeclaration", extra="; tg:documentHolder ex:exporter")
        else:
            document("sfa", "sgProcessedFoodRegistration", extra="; tg:documentHolder ex:importer")
            if variant != "sg-missing":
                document("ccp", "sgCargoClearancePermit", extra="; tg:documentHolder ex:importer")

        graph = parse_turtle((PREFIXES + "\n".join(facts)).encode(), "http://www.semanticweb.org/tradegraph/domain-test/")
        case = repository.revise_case(
            base.case_id, base.revision, FactPatch(
                "Add pinned shipment, actors and evidence", add_triples=tuple(graph), evidence_claims=tuple(claims),
            ),
        )
        client = ReasoningClient(kb, classpath)
        service = AssessmentService(repository, client)
        report = service.assess(
            AssessmentRequest(kb.snapshot.sha256, case.case_id, case.revision, ("CQ01",)),
        )
        return (report, service.provenance) if include_provenance else report

    return run
