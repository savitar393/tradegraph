"""Declarative Temporary Import (Impor Sementara — PMK 178/2017 jo. PMK 106/2019 & Permendag 16/2025 Pasal 47) service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from rdflib import Namespace, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import (
    AssessmentContext,
    AssessmentRequest,
    EvidenceClaim,
    FactPatch,
    PlanRequest,
    PlanningLimits,
)
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG
from tradegraph.kb.rdf import parse_turtle
from tradegraph.optimization.service import (
    FindingExplanationSummary,
    LartasSummary,
    MultiFtaOptimizer,
    PlanStepSummary,
    ProvenanceCitation,
    ShipmentSpec,
)
from tradegraph.planning.engine import PlanningEngine

TEMP_EX = Namespace("http://www.semanticweb.org/tradegraph/temp-import#")


@dataclass(frozen=True, slots=True)
class TemporaryImportSpec:
    hs_code: str = "8413.70.11"
    dispatch_country: str = "TH"
    destination_country: str = "ID"
    as_of_date: str = "2025-09-15"
    procedure: str = "temporaryImport"  # temporaryImport | reExportTemporaryImport
    intended_use: str = "exhibitionUse"  # exhibitionUse | conferenceUse | commercialResaleUse
    proposed_use: str | None = None
    requested_settlement: str = "reexportSettlement"  # reexportSettlement | domesticSettlement
    settlement_ground: str = "ordinaryCommercialTransfer"  # ordinaryCommercialTransfer | governmentProject
    identifiable_for_reexport: bool = True
    consumed_in_use: bool = False
    essential_change_in_use: bool = False
    permit_end_date: str = "2025-10-15"
    permit_extended: bool = False
    exhibition_count: int = 1
    provided_docs: tuple[str, ...] = ()
    change_approval_date: str | None = None
    notice_date: str | None = None
    realization_date: str | None = None
    include_plan: bool = True
    include_explain: bool = True


@dataclass(frozen=True, slots=True)
class TemporaryImportPolicyDetail:
    policy_id: str
    policy_label: str
    policy_kind: str
    status: str
    source_clauses: tuple[str, ...]
    citations: tuple[ProvenanceCitation, ...]
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_label": self.policy_label,
            "policy_kind": self.policy_kind,
            "status": self.status,
            "source_clauses": list(self.source_clauses),
            "citations": [c.to_dict() for c in self.citations],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class TemporaryImportEvaluationResult:
    kb_version: str
    kb_sha256: str
    hs_code: str
    hs_iri: str
    dispatch_country: str
    destination_country: str
    as_of_date: str
    procedure: str
    intended_use: str
    proposed_use: str
    requested_settlement: str
    permit_end_date: str
    coverage_status: str
    overall_status: str  # ELIGIBLE | ACTION_REQUIRED | INELIGIBLE | OUT_OF_COVERAGE | PROHIBITED
    lartas: LartasSummary
    policies: tuple[TemporaryImportPolicyDetail, ...]
    plan_steps: tuple[PlanStepSummary, ...]
    citations: tuple[ProvenanceCitation, ...]
    explanations: tuple[FindingExplanationSummary, ...]
    diagnostics: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "kb_version": self.kb_version,
            "kb_sha256": self.kb_sha256,
            "hs_code": self.hs_code,
            "hs_iri": self.hs_iri,
            "dispatch_country": self.dispatch_country,
            "destination_country": self.destination_country,
            "as_of_date": self.as_of_date,
            "procedure": self.procedure,
            "intended_use": self.intended_use,
            "proposed_use": self.proposed_use,
            "requested_settlement": self.requested_settlement,
            "permit_end_date": self.permit_end_date,
            "coverage_status": self.coverage_status,
            "overall_status": self.overall_status,
            "lartas": self.lartas.to_dict(),
            "policies": [p.to_dict() for p in self.policies],
            "plan_steps": [s.to_dict() for s in self.plan_steps],
            "citations": [c.to_dict() for c in self.citations],
            "explanations": [e.to_dict() for e in self.explanations],
            "diagnostics": list(self.diagnostics),
        }


class TemporaryImportAdvisor:
    """Evaluates Indonesian Temporary Import & Re-Export rules directly via KB policies, PlanningEngine & ProvenanceService."""

    def __init__(self, optimizer: MultiFtaOptimizer):
        self._opt = optimizer
        self.kb = optimizer.kb
        self._owl = optimizer._owl

    def evaluate(self, spec: TemporaryImportSpec) -> TemporaryImportEvaluationResult:
        hs_iri = self._opt.resolve_hs_iri(spec.hs_code)
        dispatch_iri = self._opt.resolve_country_iri(spec.dispatch_country)
        dest_iri = self._opt.resolve_country_iri(spec.destination_country)
        if str(dest_iri) != str(TG.indonesia):
            raise ValueError("Currently TemporaryImportAdvisor supports Indonesian customs destination (indonesia / ID).")

        hs_code_val = str(self._owl.value(hs_iri, TG.codeValue) or spec.hs_code)
        hs_local = str(hs_iri).split("#")[-1]
        disp_local = str(dispatch_iri).split("#")[-1]
        dest_local = str(dest_iri).split("#")[-1]
        proposed_use = spec.proposed_use or spec.intended_use

        # Check absolute import prohibition first
        lartas = self._opt._assess_lartas(
            hs_iri,
            TG.jurisdictionIndonesiaCustoms,
            ShipmentSpec(
                hs_code=hs_code_val,
                dispatch_country=disp_local,
                destination_country=dest_local,
                as_of_date=spec.as_of_date,
                include_explain=spec.include_explain,
            ),
        )
        if lartas.prohibited:
            return TemporaryImportEvaluationResult(
                kb_version=self.kb.snapshot.version,
                kb_sha256=self.kb.snapshot.sha256,
                hs_code=hs_code_val,
                hs_iri=str(hs_iri),
                dispatch_country=disp_local,
                destination_country=dest_local,
                as_of_date=spec.as_of_date,
                procedure=spec.procedure,
                intended_use=spec.intended_use,
                proposed_use=proposed_use,
                requested_settlement=spec.requested_settlement,
                permit_end_date=spec.permit_end_date,
                coverage_status="PROHIBITED",
                overall_status="PROHIBITED",
                lartas=lartas,
                policies=(),
                plan_steps=(),
                citations=lartas.citations,
                explanations=(),
                diagnostics=tuple(lartas.diagnostics) or ("Barang termasuk kategori DILARANG IMPOR sehingga tidak dapat menggunakan fasilitas Impor Sementara.",),
            )

        tx_dt = datetime.fromisoformat(f"{spec.as_of_date}T13:00:00+07:00")
        permit_end_dt = datetime.fromisoformat(f"{spec.permit_end_date}T00:00:00+07:00")
        eval_dt = datetime.fromisoformat("2025-12-01T13:00:00+07:00")
        if tx_dt > eval_dt:
            eval_dt = tx_dt + timedelta(days=30)

        context = AssessmentContext(
            self.kb.snapshot.sha256,
            ENGINE_PROFILE_ID,
            jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
            procedure_iri=str(TG[spec.procedure]),
            agreement_iri=None,
            transaction_at=tx_dt,
            evaluation_at=eval_dt,
            evidence_scope_id="case-documents",
            product_iri=str(TEMP_EX.product),
            batch_iri=str(TEMP_EX.batch),
        )

        with TemporaryDirectory(prefix="tradegraph-temp-import-") as tmp:
            repo = CaseRepository(Path(tmp) / "temp_import.sqlite3")
            base = repo.create_case("", context)
            rev = f"<http://www.semanticweb.org/tradegraph/case/{base.case_id}/revision/2>"

            prefixes = f"@prefix ex: <{TEMP_EX}> .\n@prefix tg: <{TG}> .\n@prefix xsd: <{XSD}> .\n"
            facts = [f"""
ex:product tg:hasTariffClassification tg:{hs_local} ; tg:hasBatch ex:batch ;
    tg:identifiableForReexport {"true" if spec.identifiable_for_reexport else "false"} ;
    tg:consumedInUse {"true" if spec.consumed_in_use else "false"} ;
    tg:essentialChangeInUse {"true" if spec.essential_change_in_use else "false"} ;
    tg:importCategory tg:unrestrictedImport .
ex:batch tg:usesMaterial ex:material ; tg:producedAt ex:facility ; tg:producedBy ex:producer .
ex:facility tg:withinLocation tg:{disp_local} .
ex:material tg:suppliedBy ex:supplier .
ex:shipment tg:containsGoods ex:product ; tg:shipmentDispatchCountry tg:{disp_local} ;
    tg:shipmentDestinationCountry tg:{dest_local} ; tg:hasTransportLeg ex:leg ; tg:cargoCategory tg:regularCargo ;
    tg:temporaryUsePermitStart "{spec.as_of_date}T00:00:00+07:00"^^xsd:dateTime ;
    tg:temporaryUsePermitEnd "{spec.permit_end_date}T00:00:00+07:00"^^xsd:dateTime ;
    tg:permitExtended {"true" if spec.permit_extended else "false"} ;
    tg:exhibitionCount {max(1, int(spec.exhibition_count))} .
ex:leg tg:departsFrom tg:{disp_local} ; tg:arrivesAt tg:{dest_local} .
{rev} tg:revisionShipment ex:shipment ;
    tg:intendedUse tg:{spec.intended_use} ;
    tg:proposedUse tg:{proposed_use} ;
    tg:requestedSettlement tg:{spec.requested_settlement} .
ex:importerAssignment tg:roleActor ex:importer ; tg:hasActorRole tg:roleImporter ; tg:assignedInRevision {rev} .
ex:exporterAssignment tg:roleActor ex:exporter ; tg:hasActorRole tg:roleExporter ; tg:assignedInRevision {rev} .
"""]

            if spec.requested_settlement == "domesticSettlement":
                if spec.settlement_ground == "governmentProject":
                    facts.append(f"{rev} tg:settlementGround tg:governmentProject .")
                else:
                    facts.append(f"{rev} tg:settlementGround tg:ordinaryCommercialTransfer ; tg:settlementGroundsClosed true .")

            claims: list[EvidenceClaim] = []
            provided = {d.strip() for d in spec.provided_docs if d.strip()}

            def add_doc(name: str, kind: str, *, extra: str = "") -> None:
                facts.append(f"""
ex:{name} a tg:Document ; tg:documentType tg:{kind} ;
    tg:documentSubject ex:product ; tg:documentJurisdiction tg:jurisdictionIndonesiaCustoms ;
    tg:documentShipment ex:shipment ; tg:documentBatch ex:batch ;
    tg:documentValidFrom "2024-01-01T00:00:00Z"^^xsd:dateTime ;
    tg:documentValidUntil "2028-12-31T00:00:00Z"^^xsd:dateTime {extra} .
""")
                claims.append(EvidenceClaim(
                    str(TEMP_EX[f"{name}Claim"]),
                    str(TEMP_EX.product),
                    str(TG.hasSupportingDocument),
                    str(TEMP_EX[name]),
                    TEMP_EX[name],
                    verification_status="VERIFIED",
                ))

            if "temporaryImportPermit" in provided:
                add_doc("permit", "temporaryImportPermit", extra="; tg:documentHolder ex:importer")
            if "temporaryImportGuarantee" in provided:
                add_doc("guarantee", "temporaryImportGuarantee", extra="; tg:documentHolder ex:importer")
            if "reexportPlan" in provided:
                add_doc("plan", "reexportPlan")
            if "exhibitionStorageRecord" in provided:
                add_doc("storage", "exhibitionStorageRecord", extra="; tg:customsSupervisedStorage true")
            if "domesticSettlementDecision" in provided:
                ground = "governmentProject" if spec.settlement_ground == "governmentProject" else "ordinaryCommercialTransfer"
                add_doc(
                    "domestic",
                    "domesticSettlementDecision",
                    extra=f"; tg:documentHolder ex:importer ; tg:documentSettlementGround tg:{ground}",
                )
            if "changeOfUseApproval" in provided:
                if spec.change_approval_date:
                    ch_ts = f"{spec.change_approval_date}T10:00:00+07:00"
                else:
                    candidate = tx_dt + timedelta(days=5)
                    if candidate >= permit_end_dt:
                        candidate = tx_dt + timedelta(hours=1)
                    ch_ts = candidate.isoformat()
                add_doc(
                    "changeApproval",
                    "changeOfUseApproval",
                    extra=(
                        f'; tg:documentHolder ex:importer ; tg:documentProposedUse tg:{proposed_use} ; '
                        f'tg:documentEventAt "{ch_ts}"^^xsd:dateTime'
                    ),
                )
            if "temporaryReexportNotice" in provided:
                notice_dt = spec.notice_date or (permit_end_dt - timedelta(days=5)).strftime("%Y-%m-%d")
                add_doc(
                    "notice",
                    "temporaryReexportNotice",
                    extra=f'; tg:documentHolder ex:importer ; tg:documentEventAt "{notice_dt}T10:00:00+07:00"^^xsd:dateTime',
                )
            if "temporaryReexportRealization" in provided:
                real_dt = spec.realization_date or (permit_end_dt + timedelta(days=15)).strftime("%Y-%m-%d")
                add_doc(
                    "exit",
                    "temporaryReexportRealization",
                    extra=f'; tg:documentHolder ex:importer ; tg:documentEventAt "{real_dt}T10:00:00+07:00"^^xsd:dateTime',
                )

            triples = tuple(parse_turtle((prefixes + "\n".join(facts)).encode("utf-8"), "http://www.semanticweb.org/tradegraph/temp-import/"))
            revised = repo.revise_case(
                base.case_id,
                base.revision,
                FactPatch(
                    "Add temporary import facts and evidence",
                    add_triples=triples,
                    evidence_claims=tuple(claims),
                ),
            )
            service = AssessmentService(repo, self._opt._client)
            report = service.assess(AssessmentRequest(self.kb.snapshot.sha256, revised.case_id, revised.revision))

            coverage_status = report.coverage_status or "OUT_OF_COVERAGE"
            policies: list[TemporaryImportPolicyDetail] = []
            clause_ids: set[str] = set()
            explanations: list[FindingExplanationSummary] = []
            diagnostics: list[str] = []

            if coverage_status == "OUT_OF_COVERAGE":
                diagnostics.append(
                    f"Kombinasi HS {hs_code_val}, rute {disp_local.upper()} → ID, tujuan {spec.intended_use}, "
                    f"dan tanggal {spec.as_of_date} berada di luar cakupan terverifikasi Impor Sementara KB "
                    f"(terverifikasi saat ini: HS 8413.70.11, TH → ID, Pameran/exhibitionUse, 2025-09-15)."
                )
            else:
                for finding in report.domain_findings:
                    pdef = self.kb.policy_catalog.get(finding.policy_id)
                    c_slugs = tuple(sorted({str(c).split("#")[-1] for c in finding.source_clauses}))
                    clause_ids.update(c_slugs)
                    p_cits = self._opt.resolve_citations(c_slugs) if spec.include_explain else ()
                    policies.append(TemporaryImportPolicyDetail(
                        policy_id=finding.policy_id,
                        policy_label=pdef.label if pdef is not None else finding.policy_id,
                        policy_kind=finding.kind,
                        status=finding.status,
                        source_clauses=c_slugs,
                        citations=p_cits,
                        diagnostics=tuple(finding.diagnostics),
                    ))
                    if spec.include_explain and report.execution_status == "CONVERGED" and finding.finding_id is not None:
                        if finding.status != "NOT_APPLICABLE" or finding.policy_id == "temporary-general-import-controls":
                            exp = service.provenance.explain(finding.finding_id)
                            explanations.append(self._opt.serialize_explanation(finding, exp))

            plan_steps: list[PlanStepSummary] = []
            if (
                spec.include_plan
                and coverage_status != "OUT_OF_COVERAGE"
                and report.execution_status == "CONVERGED"
                and spec.requested_settlement == "reexportSettlement"
            ):
                goal_iri = str(TG["goal-reexport-preparation"])
                if goal_iri in self.kb.goal_catalog:
                    planner = PlanningEngine(service)
                    candidates = planner.search_plans(PlanRequest(
                        base_run_id=report.run_id,
                        goal_iri=goal_iri,
                        allowed_action_ids=(),
                        limits=PlanningLimits(max_depth=5, max_states=16, timeout_ms=45000),
                        objective_id="fewest-actions",
                    ))
                    if candidates:
                        for step in candidates[0].steps:
                            action = self.kb.action_catalog.get(step.action_id)
                            label = action.label if action else step.action_id
                            added = tuple(
                                f"{str(p).split('#')[-1]} -> {str(o).split('#')[-1]}"
                                for _, p, o in step.effects.added
                            )
                            c_slugs = tuple(str(c).split("#")[-1] for c in step.source_clauses)
                            clause_ids.update(c_slugs)
                            plan_steps.append(PlanStepSummary(step.action_id, label, added, c_slugs))

            statuses = {p.policy_id: p.status for p in policies}
            if coverage_status == "OUT_OF_COVERAGE":
                overall = "OUT_OF_COVERAGE"
            elif any(p.status == "NOT_SATISFIED" for p in policies):
                overall = "INELIGIBLE"
                for p in policies:
                    if p.status == "NOT_SATISFIED":
                        diagnostics.append(f"Kebijakan tidak terpenuhi: {p.policy_label} ({p.policy_id}).")
            elif any(p.status == "UNDETERMINED" for p in policies):
                overall = "ACTION_REQUIRED"
                for p in policies:
                    if p.status == "UNDETERMINED":
                        diagnostics.append(f"Perlu melengkapi bukti/dokumen: {p.policy_label} ({p.policy_id}).")
            else:
                overall = "ELIGIBLE"
                if statuses.get("temporary-general-import-controls") == "NOT_APPLICABLE":
                    diagnostics.append(
                        "Pengecualian ketentuan Lartas impor umum berlaku (Permendag 16/2025 Pasal 47 ayat 1) selama barang digunakan sesuai izin Impor Sementara."
                    )

            all_citations = self._opt.resolve_citations(tuple(sorted(clause_ids))) if spec.include_explain else ()
            return TemporaryImportEvaluationResult(
                kb_version=self.kb.snapshot.version,
                kb_sha256=self.kb.snapshot.sha256,
                hs_code=hs_code_val,
                hs_iri=str(hs_iri),
                dispatch_country=disp_local,
                destination_country=dest_local,
                as_of_date=spec.as_of_date,
                procedure=spec.procedure,
                intended_use=spec.intended_use,
                proposed_use=proposed_use,
                requested_settlement=spec.requested_settlement,
                permit_end_date=spec.permit_end_date,
                coverage_status=coverage_status,
                overall_status=overall,
                lartas=lartas,
                policies=tuple(policies),
                plan_steps=tuple(plan_steps),
                citations=all_citations,
                explanations=tuple(explanations),
                diagnostics=tuple(diagnostics),
            )
