"""End-to-end Multi-FTA optimization combining KB tariff schedules, HermiT RoO, Lartas/NTM, landed taxes, provenance, and planning."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from rdflib import Literal as RDFLiteral, Namespace, RDF, RDFS, URIRef, XSD

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import (
    AssessmentContext,
    AssessmentReport,
    AssessmentRequest,
    CompiledKB,
    EvidenceClaim,
    FactPatch,
    PlanningLimits,
    PlanRequest,
)
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG
from tradegraph.kb.queries import prepare_local_query
from tradegraph.kb.rdf import parse_turtle
from tradegraph.planning.engine import PlanningEngine
from tradegraph.reasoning.client import ReasoningClient


OPT_EX = Namespace("urn:tradegraph:optimize#")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")


def _to_decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _normalize_api_type(raw: str | None) -> str | None:
    if raw is None:
        return None
    clean = re.sub(r"[^a-z]", "", raw.lower())
    if clean == "apip":
        return "apiP"
    if clean == "apiu":
        return "apiU"
    return raw.strip()


@dataclass(frozen=True, slots=True)
class MaterialInput:
    hs_code: str
    non_originating: bool = True
    origin_country: str | None = None
    origin_proof_verified: bool | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> MaterialInput:
        return cls(
            hs_code=str(raw["hs_code"]),
            non_originating=bool(raw.get("non_originating", True)),
            origin_country=str(raw["origin_country"]) if raw.get("origin_country") else None,
            origin_proof_verified=(
                bool(raw["origin_proof_verified"]) if raw.get("origin_proof_verified") is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class ShipmentSpec:
    hs_code: str
    dispatch_country: str
    destination_country: str = "indonesia"
    as_of_date: str = "2025-09-15"
    cif_value: Decimal | None = None
    currency: str = "USD"
    has_api: bool = True
    api_type: str | None = None
    materials: tuple[MaterialInput, ...] = ()
    rvc_percent: Decimal | None = None
    wo_confirmed: bool | None = None
    process_kind: str | None = None
    process_satisfied: bool | None = None
    de_minimis_fob: tuple[Decimal, Decimal] | None = None
    de_minimis_weight: tuple[Decimal, Decimal] | None = None
    performed_operations: tuple[str, ...] | None = None
    dv20_percent: Decimal | None = None
    highest_contributor_country: str | None = None
    provided_origin_docs: tuple[str, ...] = ()
    provided_lartas_docs: tuple[str, ...] = ()
    include_plan: bool = False
    include_explain: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ShipmentSpec:
        dm_fob = raw.get("de_minimis_fob")
        dm_wt = raw.get("de_minimis_weight")
        ops = raw.get("performed_operations")
        docs = raw.get("provided_origin_docs") or ()
        lartas_docs = raw.get("provided_lartas_docs") or ()
        mats = raw.get("materials") or ()
        return cls(
            hs_code=str(raw["hs_code"]),
            dispatch_country=str(raw["dispatch_country"]),
            destination_country=str(raw.get("destination_country", "indonesia")),
            as_of_date=str(raw.get("as_of_date", "2025-09-15")),
            cif_value=_to_decimal(raw.get("cif_value")),
            currency=str(raw.get("currency", "USD")),
            has_api=bool(raw.get("has_api", True)),
            api_type=_normalize_api_type(str(raw["api_type"])) if raw.get("api_type") else None,
            materials=tuple(MaterialInput.from_dict(m) for m in mats),
            rvc_percent=_to_decimal(raw.get("rvc_percent")),
            wo_confirmed=bool(raw["wo_confirmed"]) if raw.get("wo_confirmed") is not None else None,
            process_kind=str(raw["process_kind"]) if raw.get("process_kind") else None,
            process_satisfied=(
                bool(raw["process_satisfied"]) if raw.get("process_satisfied") is not None else None
            ),
            de_minimis_fob=(
                (_to_decimal(dm_fob[0]), _to_decimal(dm_fob[1]))
                if isinstance(dm_fob, (list, tuple)) and len(dm_fob) == 2 else None
            ),
            de_minimis_weight=(
                (_to_decimal(dm_wt[0]), _to_decimal(dm_wt[1]))
                if isinstance(dm_wt, (list, tuple)) and len(dm_wt) == 2 else None
            ),
            performed_operations=tuple(str(op) for op in ops) if ops is not None else None,
            dv20_percent=_to_decimal(raw.get("dv20_percent")),
            highest_contributor_country=(
                str(raw["highest_contributor_country"]) if raw.get("highest_contributor_country") else None
            ),
            provided_origin_docs=tuple(str(d) for d in docs),
            provided_lartas_docs=tuple(str(d) for d in lartas_docs),
            include_plan=bool(raw.get("include_plan", False)),
            include_explain=bool(raw.get("include_explain", False)),
        )


@dataclass(frozen=True, slots=True)
class ProvenanceCitation:
    clause_id: str
    clause_locator: str | None
    document_id: str | None
    regulation_number: str | None
    document_title: str | None
    source_key: str | None
    source_record_id: str | None
    effective_from: str | None
    url: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "clause_id": self.clause_id,
            "clause_locator": self.clause_locator,
            "document_id": self.document_id,
            "regulation_number": self.regulation_number,
            "document_title": self.document_title,
            "source_key": self.source_key,
            "source_record_id": self.source_record_id,
            "effective_from": self.effective_from,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class FindingExplanationSummary:
    policy_id: str
    status: str
    availability: str
    reaches_asserted_facts: bool
    node_kinds: tuple[str, ...]
    source_citations: tuple[ProvenanceCitation, ...]
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "status": self.status,
            "availability": self.availability,
            "reaches_asserted_facts": self.reaches_asserted_facts,
            "node_kinds": list(self.node_kinds),
            "source_citations": [c.to_dict() for c in self.source_citations],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class LartasRuleDetail:
    rule_id: str
    commodity_group: str | None
    supervision_mode: str
    prohibited: bool
    required_docs: tuple[str, ...]
    required_doc_labels: Mapping[str, str]
    allowed_api: tuple[str, ...]
    effective_from: str | None
    source_clause: str | None
    citation: ProvenanceCitation | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "commodity_group": self.commodity_group,
            "supervision_mode": self.supervision_mode,
            "prohibited": self.prohibited,
            "required_docs": list(self.required_docs),
            "required_doc_labels": dict(self.required_doc_labels),
            "allowed_api": list(self.allowed_api),
            "effective_from": self.effective_from,
            "source_clause": self.source_clause,
            "citation": self.citation.to_dict() if self.citation else None,
        }


@dataclass(frozen=True, slots=True)
class LartasSummary:
    status: str  # PROHIBITED | UNRESTRICTED | CLEAR | BORDER_BLOCKED | POST_BORDER_PENDING
    prohibited: bool
    api_type: str | None
    rules: tuple[LartasRuleDetail, ...]
    required_docs: tuple[str, ...]
    missing_border_docs: tuple[str, ...]
    missing_post_border_docs: tuple[str, ...]
    api_mismatch: bool
    source_clauses: tuple[str, ...]
    citations: tuple[ProvenanceCitation, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "prohibited": self.prohibited,
            "api_type": self.api_type,
            "rules": [r.to_dict() for r in self.rules],
            "required_docs": list(self.required_docs),
            "missing_border_docs": list(self.missing_border_docs),
            "missing_post_border_docs": list(self.missing_post_border_docs),
            "api_mismatch": self.api_mismatch,
            "source_clauses": list(self.source_clauses),
            "citations": [c.to_dict() for c in self.citations],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class TaxBreakdown:
    currency: str
    cif_value: Decimal
    duty_percent: Decimal
    bm_amount: Decimal
    nilai_impor: Decimal
    vat_percent: Decimal
    ppn_amount: Decimal
    pph22_percent: Decimal
    pph22_amount: Decimal
    total_import_taxes: Decimal
    total_landed_cost: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "currency": self.currency,
            "cif_value": str(_q2(self.cif_value)),
            "duty_percent": str(_q2(self.duty_percent)),
            "bm_amount": str(self.bm_amount),
            "nilai_impor": str(self.nilai_impor),
            "vat_percent": str(_q2(self.vat_percent)),
            "ppn_amount": str(self.ppn_amount),
            "pph22_percent": str(_q2(self.pph22_percent)),
            "pph22_amount": str(self.pph22_amount),
            "total_import_taxes": str(self.total_import_taxes),
            "total_landed_cost": str(self.total_landed_cost),
        }


@dataclass(frozen=True, slots=True)
class PlanStepSummary:
    action_id: str
    label: str
    added_facts: tuple[str, ...]
    source_clauses: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "label": self.label,
            "added_facts": list(self.added_facts),
            "source_clauses": list(self.source_clauses),
        }


@dataclass(frozen=True, slots=True)
class SchemeEvaluation:
    agreement: str
    agreement_label: str
    facility_code: str | None
    back_to_back_allowed: bool | None
    psr_level: str | None
    concise_rule: str | None
    coverage_status: str
    eligibility: str  # ELIGIBLE | ELIGIBLE_PENDING_DOCS | INELIGIBLE | EXCLUDED_TO_MFN | PROHIBITED
    origin_status: str
    consignment_status: str
    origin_evidence_status: str
    de_minimis_status: str | None
    minimal_ops_status: str | None
    dv20_status: str | None
    requires_rcep_dv20: bool
    rcep_country_of_origin: str | None
    mfn_duty_percent: Decimal | None
    schedule_duty_percent: Decimal | None
    applied_duty_percent: Decimal | None
    duty_savings_percent: Decimal | None
    excluded_to_mfn: bool
    tariff_revoked_on: str | None
    tariff_superseding_doc: str | None
    tax_breakdown: TaxBreakdown | None
    total_tax_savings: Decimal | None
    policy_statuses: Mapping[str, str]
    source_clauses: tuple[str, ...]
    diagnostics: tuple[str, ...]
    plan_steps: tuple[PlanStepSummary, ...] = ()
    citations: tuple[ProvenanceCitation, ...] = ()
    explanations: tuple[FindingExplanationSummary, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "agreement": self.agreement,
            "agreement_label": self.agreement_label,
            "facility_code": self.facility_code,
            "back_to_back_allowed": self.back_to_back_allowed,
            "psr_level": self.psr_level,
            "concise_rule": self.concise_rule,
            "coverage_status": self.coverage_status,
            "eligibility": self.eligibility,
            "origin_status": self.origin_status,
            "consignment_status": self.consignment_status,
            "origin_evidence_status": self.origin_evidence_status,
            "de_minimis_status": self.de_minimis_status,
            "minimal_ops_status": self.minimal_ops_status,
            "dv20_status": self.dv20_status,
            "requires_rcep_dv20": self.requires_rcep_dv20,
            "rcep_country_of_origin": self.rcep_country_of_origin,
            "mfn_duty_percent": str(_q2(self.mfn_duty_percent)) if self.mfn_duty_percent is not None else None,
            "schedule_duty_percent": (
                str(_q2(self.schedule_duty_percent)) if self.schedule_duty_percent is not None else None
            ),
            "applied_duty_percent": (
                str(_q2(self.applied_duty_percent)) if self.applied_duty_percent is not None else None
            ),
            "duty_savings_percent": (
                str(_q2(self.duty_savings_percent)) if self.duty_savings_percent is not None else None
            ),
            "excluded_to_mfn": self.excluded_to_mfn,
            "tariff_revoked_on": self.tariff_revoked_on,
            "tariff_superseding_doc": self.tariff_superseding_doc,
            "tax_breakdown": self.tax_breakdown.to_dict() if self.tax_breakdown else None,
            "total_tax_savings": str(self.total_tax_savings) if self.total_tax_savings is not None else None,
            "policy_statuses": dict(self.policy_statuses),
            "source_clauses": list(self.source_clauses),
            "diagnostics": list(self.diagnostics),
            "plan_steps": [step.to_dict() for step in self.plan_steps],
            "citations": [c.to_dict() for c in self.citations],
            "explanations": [e.to_dict() for e in self.explanations],
        }


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    kb_version: str
    kb_sha256: str
    hs_code: str
    hs_iri: str
    dispatch_country: str
    destination_country: str
    as_of_date: str
    recommended_agreement: str  # agreement slug | "MFN" | "PROHIBITED"
    mfn_duty_percent: Decimal | None
    mfn_tax_breakdown: TaxBreakdown | None
    lartas: LartasSummary
    schemes: tuple[SchemeEvaluation, ...]
    tax_profile_citations: tuple[ProvenanceCitation, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kb_version": self.kb_version,
            "kb_sha256": self.kb_sha256,
            "hs_code": self.hs_code,
            "hs_iri": self.hs_iri,
            "dispatch_country": self.dispatch_country,
            "destination_country": self.destination_country,
            "as_of_date": self.as_of_date,
            "recommended_agreement": self.recommended_agreement,
            "mfn_duty_percent": str(_q2(self.mfn_duty_percent)) if self.mfn_duty_percent is not None else None,
            "mfn_tax_breakdown": self.mfn_tax_breakdown.to_dict() if self.mfn_tax_breakdown else None,
            "lartas": self.lartas.to_dict(),
            "schemes": [s.to_dict() for s in self.schemes],
            "tax_profile_citations": [c.to_dict() for c in self.tax_profile_citations],
        }


class MultiFtaOptimizer:
    """Orchestrates KB tariff optimization, HermiT RoO, Lartas/NTM, RCEP DV20 fallback, landed taxes, provenance, and planning."""

    def __init__(self, kb: CompiledKB, classpath: str, *, java: str = "java"):
        self.kb = kb
        self.classpath = classpath
        self.java = java
        self._owl = kb.dataset.graph(kb.owl_projection_ref)
        self._client = ReasoningClient(kb, classpath, java=java)

    def resolve_hs_iri(self, raw_code: str) -> URIRef:
        text = raw_code.strip()
        if text.startswith("urn:"):
            return URIRef(text)
        if text.startswith("hs-"):
            cand = TG[text]
            if (cand, RDF.type, TG.HSCode) in self._owl:
                return cand
        digits = re.sub(r"\D+", "", text)
        if len(digits) == 8:
            cand = TG[f"hs-{digits}"]
            if (cand, RDF.type, TG.HSCode) in self._owl:
                return cand
        for subj in self._owl.subjects(TG.codeValue, RDFLiteral(text)):
            if isinstance(subj, URIRef):
                return subj
        raise ValueError(f"HS code {raw_code!r} was not found in the canonical KB.")

    def resolve_country_iri(self, raw_country: str) -> URIRef:
        text = raw_country.strip()
        if text.startswith("urn:"):
            return URIRef(text)
        direct = TG[text]
        if (direct, RDF.type, TG.Country) in self._owl:
            return direct
        lower = text.lower()
        for subj in self._owl.subjects(RDF.type, TG.Country):
            if not isinstance(subj, URIRef):
                continue
            local = str(subj).split("#")[-1]
            if local.lower() == lower:
                return subj
            code = self._owl.value(subj, TG.countryCode)
            if code is not None and str(code).lower() == lower:
                return subj
        raise ValueError(f"Country {raw_country!r} was not found in the canonical KB.")

    def resolve_citation(self, clause_ref: URIRef | str) -> ProvenanceCitation:
        clause_iri = clause_ref if isinstance(clause_ref, URIRef) else (
            URIRef(clause_ref) if str(clause_ref).startswith("urn:") else TG[str(clause_ref)]
        )
        clause_id = str(clause_iri).split("#")[-1]
        locator = self._owl.value(clause_iri, TG.clauseLocator)
        doc_iri = self._owl.value(clause_iri, TG.clauseInDocument)
        doc_id = str(doc_iri).split("#")[-1] if isinstance(doc_iri, URIRef) else None
        reg_num = self._owl.value(doc_iri, TG.regulationNumber) if isinstance(doc_iri, URIRef) else None
        doc_label = self._owl.value(doc_iri, RDFS.label) if isinstance(doc_iri, URIRef) else None
        eff_from = self._owl.value(doc_iri, TG.effectiveFromDate) if isinstance(doc_iri, URIRef) else None

        source_keys = sorted(str(v) for v in self._owl.objects(doc_iri, TG.sourceKey)) if isinstance(doc_iri, URIRef) else []
        source_key = next((k for k in source_keys if k.startswith("ID-")), source_keys[0] if source_keys else None)

        rec_ids = sorted(str(v) for v in self._owl.objects(doc_iri, TG.sourceRecordId)) if isinstance(doc_iri, URIRef) else []
        source_record_id = next((r for r in rec_ids if r.startswith("source-")), rec_ids[0] if rec_ids else None)

        urls = sorted(str(v) for v in self._owl.objects(doc_iri, RDFS.seeAlso)) if isinstance(doc_iri, URIRef) else []
        url = next((u for u in urls if "download" in u or u.endswith(".pdf")), urls[0] if urls else None)

        return ProvenanceCitation(
            clause_id=clause_id,
            clause_locator=str(locator) if locator is not None else None,
            document_id=doc_id,
            regulation_number=str(reg_num) if reg_num is not None else None,
            document_title=str(doc_label) if doc_label is not None else None,
            source_key=source_key,
            source_record_id=source_record_id,
            effective_from=str(eff_from) if eff_from is not None else None,
            url=url,
        )

    def resolve_citations(self, clause_refs: Iterable[URIRef | str]) -> tuple[ProvenanceCitation, ...]:
        seen: dict[str, ProvenanceCitation] = {}
        for ref in clause_refs:
            cit = self.resolve_citation(ref)
            seen.setdefault(cit.clause_id, cit)
        return tuple(seen[k] for k in sorted(seen))

    def _query_tax_profile(
        self, jurisdiction_iri: URIRef,
    ) -> tuple[tuple[Decimal, Decimal, Decimal], tuple[ProvenanceCitation, ...]] | None:
        definition = self.kb.query_catalog.get("import-tax-profile")
        if definition is None:
            return None
        query = prepare_local_query(definition.sparql, "SelectQuery")
        rows = list(self._owl.query(query, initBindings={"jurisdiction": jurisdiction_iri}))
        if not rows:
            return None
        d = rows[0].asdict()
        profile_iri = d["profile"]
        clauses = tuple(self._owl.objects(profile_iri, TG.hasSourceClause))
        rates = (
            Decimal(str(d["vatRatePercent"])),
            Decimal(str(d["pph22WithApiPercent"])),
            Decimal(str(d["pph22WithoutApiPercent"])),
        )
        return rates, self.resolve_citations(clauses)

    def _compute_tax(
        self, cif: Decimal, currency: str, duty_pct: Decimal, tax_profile: tuple[Decimal, Decimal, Decimal], has_api: bool,
    ) -> TaxBreakdown:
        vat_pct, pph_api, pph_non_api = tax_profile
        pph_pct = pph_api if has_api else pph_non_api
        bm = _q2(cif * duty_pct / HUNDRED)
        nilai_impor = _q2(cif + bm)
        ppn = _q2(nilai_impor * vat_pct / HUNDRED)
        pph22 = _q2(nilai_impor * pph_pct / HUNDRED)
        total_taxes = _q2(bm + ppn + pph22)
        return TaxBreakdown(
            currency=currency,
            cif_value=_q2(cif),
            duty_percent=_q2(duty_pct),
            bm_amount=bm,
            nilai_impor=nilai_impor,
            vat_percent=_q2(vat_pct),
            ppn_amount=ppn,
            pph22_percent=_q2(pph_pct),
            pph22_amount=pph22,
            total_import_taxes=total_taxes,
            total_landed_cost=_q2(cif + total_taxes),
        )

    def _assess_lartas(self, hs_iri: URIRef, jurisdiction_iri: URIRef, spec: ShipmentSpec) -> LartasSummary:
        definition = self.kb.query_catalog.get("import-lartas")
        if definition is None:
            return LartasSummary("UNRESTRICTED", False, spec.api_type, (), (), (), (), False, ())
        query = prepare_local_query(definition.sparql, "SelectQuery")
        rows = list(self._owl.query(query, initBindings={
            "code": hs_iri,
            "jurisdiction": jurisdiction_iri,
            "asOfDate": RDFLiteral(spec.as_of_date),
        }))
        if not rows:
            return LartasSummary("UNRESTRICTED", False, spec.api_type, (), (), (), (), False, ())

        grouped: dict[str, dict] = {}
        for row in rows:
            d = row.asdict()
            rule_id = str(d["rule"]).split("#")[-1]
            entry = grouped.setdefault(rule_id, {
                "commodity_group": str(d["commodityGroup"]) if d.get("commodityGroup") else None,
                "mode": str(d["supervisionLabel"]) if d.get("supervisionLabel") else str(d["supervisionMode"]).split("#")[-1],
                "prohibited": str(d.get("prohibited")).lower() == "true",
                "docs": {},
                "apis": set(),
                "effective_from": str(d["effectiveFrom"]) if d.get("effectiveFrom") else None,
                "clause": str(d["sourceClause"]).split("#")[-1] if d.get("sourceClause") else None,
            })
            if d.get("requiredDocType"):
                doc_id = str(d["requiredDocType"]).split("#")[-1]
                doc_label = str(d["requiredDocLabel"]) if d.get("requiredDocLabel") else doc_id
                entry["docs"][doc_id] = doc_label
            if d.get("allowedApi"):
                entry["apis"].add(str(d["allowedApi"]).split("#")[-1])

        provided = set(spec.provided_lartas_docs)
        norm_api = _normalize_api_type(spec.api_type)
        rules: list[LartasRuleDetail] = []
        all_req_docs: set[str] = set()
        missing_border: set[str] = set()
        missing_post: set[str] = set()
        clauses: set[str] = set()
        diagnostics: list[str] = []
        prohibited = False
        api_mismatch = False

        for rule_id in sorted(grouped):
            g = grouped[rule_id]
            clause = g["clause"]
            if clause:
                clauses.add(clause)
            citation = self.resolve_citation(clause) if clause and spec.include_explain else None
            req_docs = tuple(sorted(g["docs"]))
            allowed_apis = tuple(sorted(g["apis"]))
            rules.append(LartasRuleDetail(
                rule_id=rule_id,
                commodity_group=g["commodity_group"],
                supervision_mode=g["mode"],
                prohibited=g["prohibited"],
                required_docs=req_docs,
                required_doc_labels=dict(sorted(g["docs"].items())),
                allowed_api=allowed_apis,
                effective_from=g["effective_from"],
                source_clause=clause,
                citation=citation,
            ))
            if g["prohibited"]:
                prohibited = True
                reg = self.resolve_citation(clause).regulation_number if clause else rule_id
                diagnostics.append(
                    f"BARANG DILARANG IMPOR ({g['commodity_group'] or rule_id}) berdasarkan {reg}; klaim FTA tidak dapat digunakan."
                )
                continue

            all_req_docs.update(req_docs)
            if norm_api and allowed_apis and norm_api not in allowed_apis:
                api_mismatch = True
                diagnostics.append(f"Jenis API importir ({norm_api}) tidak memenuhi syarat Lartas {rule_id} (diizinkan: {', '.join(allowed_apis)}).")

            for doc in req_docs:
                if doc not in provided:
                    if g["mode"] == "POST_BORDER":
                        missing_post.add(doc)
                    else:
                        missing_border.add(doc)

        if prohibited:
            status = "PROHIBITED"
        elif missing_border or api_mismatch:
            status = "BORDER_BLOCKED"
            if missing_border:
                diagnostics.append(f"Wajib perizinan Lartas BORDER sebelum pengeluaran barang: {', '.join(sorted(missing_border))}.")
        elif missing_post:
            status = "POST_BORDER_PENDING"
            diagnostics.append(f"Wajib perizinan Lartas POST_BORDER: {', '.join(sorted(missing_post))}.")
        else:
            status = "CLEAR"

        citations = self.resolve_citations(clauses) if spec.include_explain else ()
        return LartasSummary(
            status=status,
            prohibited=prohibited,
            api_type=norm_api,
            rules=tuple(rules),
            required_docs=tuple(sorted(all_req_docs)),
            missing_border_docs=tuple(sorted(missing_border)),
            missing_post_border_docs=tuple(sorted(missing_post)),
            api_mismatch=api_mismatch,
            source_clauses=tuple(sorted(clauses)),
            citations=citations,
            diagnostics=tuple(diagnostics),
        )

    def _query_tariff_candidates(
        self, hs_iri: URIRef, dispatch_iri: URIRef, dest_iri: URIRef, as_of_date: str,
    ) -> list[dict[str, object]]:
        definition = self.kb.query_catalog["optimize-fta-tariff"]
        query = prepare_local_query(definition.sparql, "SelectQuery")
        out: list[dict[str, object]] = []
        seen: set[str] = set()
        for row in self._owl.query(query, initBindings={
            "code": hs_iri,
            "dispatch": dispatch_iri,
            "destination": dest_iri,
            "asOfDate": RDFLiteral(as_of_date),
        }):
            d = row.asdict()
            agr = str(d["agreement"]).split("#")[-1]
            if agr in seen:
                continue
            seen.add(agr)
            tariff_entry = d.get("tariffEntry")
            tariff_clauses = (
                tuple(self._owl.objects(tariff_entry, TG.hasSourceClause))
                if isinstance(tariff_entry, URIRef) else ()
            )
            out.append({
                "agreement": agr,
                "agreement_iri": str(d["agreement"]),
                "agreement_label": str(d["agreementLabel"]) if d.get("agreementLabel") else agr.upper(),
                "facility_code": str(d["facilityCode"]) if d.get("facilityCode") else None,
                "back_to_back": (
                    str(d["backToBackAllowed"]).lower() == "true" if d.get("backToBackAllowed") is not None else None
                ),
                "psr_level": str(d["psrLevel"]) if d.get("psrLevel") else None,
                "concise_rule": str(d["conciseRule"]) if d.get("conciseRule") else None,
                "mfn": _to_decimal(d.get("mfnDutyPercent")),
                "effective": _to_decimal(d.get("effectiveDutyPercent")),
                "savings": _to_decimal(d.get("dutySavingsPercent")),
                "excluded": str(d.get("excludedToMfn")).lower() == "true",
                "requires_dv20": str(d.get("requiresRcepDv20")).lower() == "true",
                "revoked_on": str(d["tariffRevokedOn"]) if d.get("tariffRevokedOn") else None,
                "superseding_doc": (
                    str(d["tariffSupersedingDoc"]).split("#")[-1] if d.get("tariffSupersedingDoc") else None
                ),
                "tariff_clauses": tuple(str(c).split("#")[-1] for c in tariff_clauses),
            })
        return out

    def _build_and_assess(
        self,
        repo: CaseRepository,
        spec: ShipmentSpec,
        agreement_slug: str,
        hs_iri: URIRef,
        dispatch_iri: URIRef,
        dest_iri: URIRef,
        jurisdiction_iri: URIRef,
    ) -> tuple[AssessmentReport, AssessmentService]:
        tx_at = datetime.fromisoformat(f"{spec.as_of_date}T13:00:00+07:00")
        eval_at = datetime.fromisoformat(f"{spec.as_of_date}T14:00:00+07:00")
        context = AssessmentContext(
            self.kb.snapshot.sha256,
            ENGINE_PROFILE_ID,
            jurisdiction_iri=str(jurisdiction_iri),
            procedure_iri=str(TG.importForUse),
            agreement_iri=str(TG[agreement_slug]),
            transaction_at=tx_at,
            evaluation_at=eval_at,
            evidence_scope_id="case-documents",
            product_iri=str(OPT_EX.product),
            batch_iri=str(OPT_EX.batch),
        )
        base = repo.create_case("", context)
        rev = f"<urn:tradegraph:case:{base.case_id}:revision:2>"
        hs_local = str(hs_iri).split("#")[-1]
        disp_local = str(dispatch_iri).split("#")[-1]
        dest_local = str(dest_iri).split("#")[-1]
        jur_local = str(jurisdiction_iri).split("#")[-1]

        prefixes = f"@prefix ex: <{OPT_EX}> .\n@prefix tg: <{TG}> .\n@prefix xsd: <{XSD}> .\n"
        facts = [f"""
ex:usd a tg:Currency .
ex:product tg:hasTariffClassification tg:{hs_local} ; tg:hasBatch ex:batch .
ex:batch tg:producedAt ex:facility ; tg:producedBy ex:producer .
ex:facility tg:withinLocation tg:{disp_local} .
ex:shipment tg:containsGoods ex:product ; tg:shipmentDispatchCountry tg:{disp_local} ;
    tg:shipmentDestinationCountry tg:{dest_local} ; tg:cargoCategory tg:regularCargo ;
    tg:routeKind tg:directRoute ; tg:shipmentRouteClosed true .
{rev} tg:revisionShipment ex:shipment .
ex:importerAssignment tg:roleActor ex:importer ; tg:hasActorRole tg:roleImporter ; tg:assignedInRevision {rev} .
"""]
        claims: list[EvidenceClaim] = []

        valid_from = f"{min(2024, int(spec.as_of_date[:4]) - 1)}-01-01T00:00:00Z"
        valid_until = f"{max(2028, int(spec.as_of_date[:4]) + 2)}-01-01T00:00:00Z"

        def add_doc(name: str, doc_type: str, extra: str = "") -> None:
            facts.append(f"""
ex:{name} a tg:Document ; tg:documentType tg:{doc_type} ;
    tg:documentSubject ex:product ; tg:documentJurisdiction tg:{jur_local} ;
    tg:documentShipment ex:shipment ; tg:documentBatch ex:batch ;
    tg:documentValidFrom "{valid_from}"^^xsd:dateTime ;
    tg:documentValidUntil "{valid_until}"^^xsd:dateTime {extra} .
""")
            claims.append(EvidenceClaim(
                str(OPT_EX[f"{name}Claim"]),
                str(OPT_EX.product),
                str(TG.hasSupportingDocument),
                str(OPT_EX[name]),
                OPT_EX[name],
                verification_status="VERIFIED",
            ))

        add_doc("transport", "transportRecord")

        for idx, doc_type in enumerate(spec.provided_origin_docs, 1):
            add_doc(
                f"originDoc{idx}",
                doc_type,
                f"; tg:documentIssuingCountry tg:{disp_local} ; tg:submittedForPreference true",
            )

        for idx, doc_type in enumerate(spec.provided_lartas_docs, 1):
            add_doc(f"lartasDoc{idx}", doc_type)

        if spec.materials:
            entries_turtle = []
            for idx, mat in enumerate(spec.materials, 1):
                mat_hs_local = str(self.resolve_hs_iri(mat.hs_code)).split("#")[-1]
                cumul = ""
                if mat.origin_country:
                    c_local = str(self.resolve_country_iri(mat.origin_country)).split("#")[-1]
                    cumul += f" ; tg:materialOriginCountry tg:{c_local}"
                if mat.origin_proof_verified is not None:
                    cumul += f" ; tg:materialOriginProofVerified {'true' if mat.origin_proof_verified else 'false'}"
                facts.append(f"""
ex:batch tg:usesMaterial ex:material{idx} .
ex:material{idx} tg:hasTariffClassification tg:{mat_hs_local} .
ex:entry{idx} tg:entryMaterial ex:material{idx} ; tg:nonOriginatingInput {'true' if mat.non_originating else 'false'} {cumul} .
""")
                entries_turtle.append(f"ex:entry{idx}")
            facts.append(
                f"ex:bom a tg:ComponentList ; tg:componentListForBatch ex:batch ; "
                f"tg:componentListClosed true ; tg:componentEntry {', '.join(entries_turtle)} ."
            )
            add_doc("bomDoc", "bomStatement", "; tg:documentComponentList ex:bom")
            if agreement_slug == "atiga":
                add_doc("atigaBomDoc", "atigaBomStatement", "; tg:documentComponentList ex:bom")

        if spec.rvc_percent is not None:
            add_doc("rvcDoc", "rvcWorksheet", f"; tg:recordedRvcPercent {_q2(spec.rvc_percent)}")

        if spec.wo_confirmed is not None:
            add_doc("woDoc", "whollyObtainedStatement", f"; tg:whollyObtainedConfirmed {'true' if spec.wo_confirmed else 'false'}")

        if spec.process_kind is not None and spec.process_satisfied is not None:
            add_doc(
                "procDoc",
                "processRuleStatement",
                f"; tg:satisfiedProcessKind tg:{spec.process_kind} ; tg:processRuleSatisfied {'true' if spec.process_satisfied else 'false'}",
            )

        if spec.de_minimis_fob is not None or spec.de_minimis_weight is not None:
            dm_parts = ["; tg:documentComponentList ex:bom"] if spec.materials else []
            if spec.de_minimis_fob is not None:
                fob, failed = spec.de_minimis_fob
                dm_parts.append(
                    f"; tg:valuationScopeClosed true ; tg:valuationCurrency ex:usd "
                    f"; tg:fobValue {_q2(fob)} ; tg:failedCtcMaterialValue {_q2(failed)}"
                )
            if spec.de_minimis_weight is not None:
                tot_w, fail_w = spec.de_minimis_weight
                dm_parts.append(
                    f"; tg:weightScopeClosed true ; tg:totalProductWeight {_q2(tot_w)} "
                    f"; tg:failedCtcMaterialWeight {_q2(fail_w)}"
                )
            add_doc("dmDoc", "deMinimisValueStatement", " ".join(dm_parts))

        if spec.performed_operations is not None:
            ops_csv = ", ".join(f"tg:{op}" for op in spec.performed_operations)
            add_doc("procOpsDoc", "productionProcessStatement", f"; tg:processScopeClosed true ; tg:performedOperation {ops_csv}")

        if spec.dv20_percent is not None:
            extra = f"; tg:recordedDomesticValueAdditionPercent {_q2(spec.dv20_percent)}"
            if spec.highest_contributor_country:
                hc_local = str(self.resolve_country_iri(spec.highest_contributor_country)).split("#")[-1]
                extra += f" ; tg:highestOriginatingContributorCountry tg:{hc_local}"
            add_doc("dv20Doc", "dv20Statement", extra)

        triples = tuple(parse_turtle((prefixes + "\n".join(facts)).encode("utf-8"), str(OPT_EX)))
        revised = repo.revise_case(
            base.case_id,
            base.revision,
            FactPatch(f"optimize-{agreement_slug}", add_triples=triples, evidence_claims=tuple(claims), evidence_scope_closed=True),
        )
        service = AssessmentService(repo, self._client)
        report = service.assess(AssessmentRequest(self.kb.snapshot.sha256, revised.case_id, revised.revision))
        return report, service

    def _explain_key_findings(
        self, service: AssessmentService, report: AssessmentReport, agr: str,
    ) -> tuple[FindingExplanationSummary, ...]:
        key_ids = {
            f"{agr}-psr-overall",
            f"{agr}-psr-wo",
            f"{agr}-psr-rvc",
            f"{agr}-psr-ctc",
            f"{agr}-psr-process",
            f"{agr}-psr-consignment",
            f"{agr}-psr-origin-evidence",
            f"{agr}-psr-de-minimis",
            f"{agr}-psr-minimal-ops",
            "rcep-tariff-differential-dv20",
            "atiga-rvc",
            "atiga-ctsh",
            "atiga-de-minimis-value",
            "atiga-consignment",
            "atiga-origin-evidence",
        }
        summaries: list[FindingExplanationSummary] = []
        for finding in report.domain_findings:
            if finding.policy_id not in key_ids or finding.finding_id is None:
                continue
            if finding.status == "NOT_APPLICABLE":
                continue
            exp = service.provenance.explain(finding.finding_id)
            clause_iris = [ref.clause_iri for ref in exp.source_refs] or list(finding.source_clauses)
            summaries.append(FindingExplanationSummary(
                policy_id=finding.policy_id,
                status=finding.status,
                availability=exp.availability,
                reaches_asserted_facts=exp.support_graph.reaches_asserted_facts(),
                node_kinds=tuple(sorted({node.kind for node in exp.support_graph.nodes})),
                source_citations=self.resolve_citations(clause_iris),
                diagnostics=exp.diagnostics,
            ))
        return tuple(summaries)

    def _summarize_plan(
        self, service: AssessmentService, run_id: str, *, include_lartas: bool = True,
    ) -> tuple[PlanStepSummary, ...]:
        goal_name = "goal-fta-full-clearance-ready" if include_lartas else "goal-fta-preference-package-ready"
        goal_iri = str(TG[goal_name])
        if goal_iri not in self.kb.goal_catalog:
            goal_iri = str(TG["goal-fta-preference-package-ready"])
        if goal_iri not in self.kb.goal_catalog:
            return ()
        planner = PlanningEngine(service)
        candidates = planner.search_plans(PlanRequest(
            base_run_id=run_id,
            goal_iri=goal_iri,
            allowed_action_ids=(),
            limits=PlanningLimits(max_depth=5, max_states=16, timeout_ms=60000),
            objective_id="fewest-actions",
        ))
        if not candidates:
            return ()
        best = candidates[0]
        steps: list[PlanStepSummary] = []
        for step in best.steps:
            action = self.kb.action_catalog.get(step.action_id)
            label = action.label if action else step.action_id
            added = tuple(
                f"{str(p).split('#')[-1]} -> {str(o).split('#')[-1]}"
                for _, p, o in step.effects.added
            )
            clauses = tuple(str(c).split("#")[-1] for c in step.source_clauses)
            steps.append(PlanStepSummary(step.action_id, label, added, clauses))
        return tuple(steps)

    def assess_scheme(self, spec: ShipmentSpec, agreement: str) -> SchemeEvaluation:
        """Assess a single explicit FTA scheme (with optional compliance plan & provenance explanations)."""
        result = self.optimize(spec, agreements=(agreement.lower(),))
        if not result.schemes:
            raise ValueError(
                f"Agreement {agreement!r} is not applicable for {spec.hs_code} ({spec.dispatch_country} -> {spec.destination_country})."
            )
        return result.schemes[0]

    def optimize(self, spec: ShipmentSpec, *, agreements: tuple[str, ...] | None = None) -> OptimizationResult:
        hs_iri = self.resolve_hs_iri(spec.hs_code)
        dispatch_iri = self.resolve_country_iri(spec.dispatch_country)
        dest_iri = self.resolve_country_iri(spec.destination_country)
        jurisdiction_iri = TG.jurisdictionIndonesiaCustoms
        if str(dest_iri) != str(TG.indonesia):
            raise ValueError("Currently MultiFtaOptimizer supports Indonesian customs destination (indonesia / ID).")

        hs_code_val = str(self._owl.value(hs_iri, TG.codeValue) or spec.hs_code)
        dispatch_slug = str(dispatch_iri).split("#")[-1]
        dest_slug = str(dest_iri).split("#")[-1]

        lartas = self._assess_lartas(hs_iri, jurisdiction_iri, spec)
        candidates = self._query_tariff_candidates(hs_iri, dispatch_iri, dest_iri, spec.as_of_date)
        if agreements is not None:
            allowed = {a.lower() for a in agreements}
            candidates = [c for c in candidates if c["agreement"] in allowed]

        mfn_duty = _to_decimal(self._owl.value(hs_iri, TG.mfnDutyPercent))
        tax_res = self._query_tax_profile(jurisdiction_iri)
        tax_profile = tax_res[0] if tax_res else None
        tax_profile_citations = tax_res[1] if (tax_res and spec.include_explain) else ()
        mfn_tax = (
            self._compute_tax(spec.cif_value, spec.currency, mfn_duty, tax_profile, spec.has_api)
            if spec.cif_value is not None and mfn_duty is not None and tax_profile is not None else None
        )

        evaluated: list[tuple[SchemeEvaluation, AssessmentService, AssessmentReport]] = []
        with TemporaryDirectory(prefix="tradegraph-opt-") as tmp:
            repo = CaseRepository(Path(tmp) / "opt.sqlite3")
            for cand in candidates:
                agr = str(cand["agreement"])
                report, service = self._build_and_assess(
                    repo, spec, agr, hs_iri, dispatch_iri, dest_iri, jurisdiction_iri,
                )
                statuses = {f.policy_id: f.status for f in report.domain_findings}
                clauses = tuple(sorted({
                    *(str(c).split("#")[-1] for f in report.domain_findings for c in f.source_clauses),
                    *cand["tariff_clauses"],
                }))
                diagnostics: list[str] = []

                if f"{agr}-psr-overall" in statuses:
                    origin_status = statuses[f"{agr}-psr-overall"]
                elif "atiga-rvc" in statuses or "atiga-ctsh" in statuses:
                    rvc_s = statuses.get("atiga-rvc")
                    ctsh_s = statuses.get("atiga-ctsh")
                    dm_s = statuses.get("atiga-de-minimis-value")
                    if "SATISFIED" in (rvc_s, ctsh_s, dm_s):
                        origin_status = "SATISFIED"
                    elif rvc_s == "NOT_SATISFIED" and ctsh_s == "NOT_SATISFIED" and dm_s in (None, "NOT_SATISFIED", "NOT_APPLICABLE"):
                        origin_status = "NOT_SATISFIED"
                    else:
                        origin_status = "UNDETERMINED"
                else:
                    origin_status = "UNDETERMINED"

                consignment_status = statuses.get(f"{agr}-psr-consignment") or statuses.get("atiga-consignment") or "UNDETERMINED"
                origin_ev_status = statuses.get(f"{agr}-psr-origin-evidence") or statuses.get("atiga-origin-evidence") or "UNDETERMINED"
                de_minimis_status = statuses.get(f"{agr}-psr-de-minimis") or statuses.get("atiga-de-minimis-value")
                minimal_ops_status = statuses.get(f"{agr}-psr-minimal-ops")
                dv20_status = statuses.get("rcep-tariff-differential-dv20") if agr == "rcep" else None

                schedule_duty = cand["effective"]
                applied_duty = schedule_duty
                excluded = bool(cand["excluded"])
                requires_dv20 = bool(cand["requires_dv20"])
                rcep_coo = dispatch_slug if agr == "rcep" else None

                if cand["revoked_on"]:
                    diagnostics.append(
                        f"PMK tarif sumber dicabut per {cand['revoked_on']} oleh {cand['superseding_doc']}; verifikasi jadwal pengganti."
                    )

                if lartas.prohibited:
                    eligibility = "PROHIBITED"
                    applied_duty = mfn_duty
                    diagnostics.extend(lartas.diagnostics)
                elif report.coverage_status == "OUT_OF_COVERAGE":
                    eligibility = "INELIGIBLE"
                    applied_duty = mfn_duty
                    diagnostics.append("Skema di luar cakupan (OUT_OF_COVERAGE) untuk rute/pos tarif ini.")
                elif excluded:
                    eligibility = "EXCLUDED_TO_MFN"
                    applied_duty = mfn_duty
                    diagnostics.append("Pos tarif dikecualikan ke tarif MFN pada jadwal PMK skema ini.")
                elif origin_status != "SATISFIED":
                    eligibility = "INELIGIBLE"
                    applied_duty = mfn_duty
                    diagnostics.append(f"Kriteria asal ({agr}) belum terpenuhi (status={origin_status}); bea kembali ke MFN.")
                else:
                    if requires_dv20 and dv20_status == "NOT_SATISFIED":
                        if spec.highest_contributor_country:
                            hc_iri = self.resolve_country_iri(spec.highest_contributor_country)
                            hc_slug = str(hc_iri).split("#")[-1]
                            rcep_coo = hc_slug
                            hc_rows = self._query_tariff_candidates(hs_iri, hc_iri, dest_iri, spec.as_of_date)
                            hc_rcep = next((r for r in hc_rows if r["agreement"] == "rcep"), None)
                            applied_duty = hc_rcep["effective"] if (hc_rcep and hc_rcep["effective"] is not None) else mfn_duty
                            diagnostics.append(
                                f"DV20 < 20% FOB (PMK 209/2022 Pasal 14 ayat 5 huruf b): RCEP Country of Origin beralih ke "
                                f"kontributor bahan originating tertinggi ({hc_slug}), tarif berlaku = {applied_duty}%."
                            )
                        else:
                            applied_duty = mfn_duty
                            diagnostics.append(
                                "DV20 < 20% FOB tanpa data kontributor tertinggi (PMK 209/2022 Pasal 15 ayat 1 huruf b): "
                                "diterapkan tarif tertinggi / MFN."
                            )
                    elif requires_dv20 and dv20_status == "UNDETERMINED":
                        diagnostics.append(
                            "Pos tercantum di Lampiran II RCEP (Tariff Differentials): wajib pernyataan DV20 >= 20% FOB untuk tarif Pihak pengekspor."
                        )

                    pending_docs = (origin_ev_status != "SATISFIED") or (requires_dv20 and dv20_status == "UNDETERMINED")
                    eligibility = "ELIGIBLE_PENDING_DOCS" if pending_docs else "ELIGIBLE"

                duty_savings = (
                    _q2(mfn_duty - applied_duty)
                    if mfn_duty is not None and applied_duty is not None else None
                )
                scheme_tax = (
                    self._compute_tax(spec.cif_value, spec.currency, applied_duty, tax_profile, spec.has_api)
                    if spec.cif_value is not None and applied_duty is not None and tax_profile is not None else None
                )
                tax_savings = (
                    _q2(mfn_tax.total_import_taxes - scheme_tax.total_import_taxes)
                    if mfn_tax is not None and scheme_tax is not None else None
                )

                citations = self.resolve_citations(clauses) if spec.include_explain else ()
                explanations = (
                    self._explain_key_findings(service, report, agr)
                    if spec.include_explain and report.execution_status == "CONVERGED" else ()
                )

                ev = SchemeEvaluation(
                    agreement=agr,
                    agreement_label=str(cand["agreement_label"]),
                    facility_code=cand["facility_code"],
                    back_to_back_allowed=cand["back_to_back"],
                    psr_level=cand["psr_level"],
                    concise_rule=cand["concise_rule"],
                    coverage_status=report.coverage_status or "UNKNOWN",
                    eligibility=eligibility,
                    origin_status=origin_status,
                    consignment_status=consignment_status,
                    origin_evidence_status=origin_ev_status,
                    de_minimis_status=de_minimis_status,
                    minimal_ops_status=minimal_ops_status,
                    dv20_status=dv20_status,
                    requires_rcep_dv20=requires_dv20,
                    rcep_country_of_origin=rcep_coo,
                    mfn_duty_percent=mfn_duty,
                    schedule_duty_percent=schedule_duty,
                    applied_duty_percent=applied_duty,
                    duty_savings_percent=duty_savings,
                    excluded_to_mfn=excluded,
                    tariff_revoked_on=cand["revoked_on"],
                    tariff_superseding_doc=cand["superseding_doc"],
                    tax_breakdown=scheme_tax,
                    total_tax_savings=tax_savings,
                    policy_statuses=statuses,
                    source_clauses=clauses,
                    diagnostics=tuple(diagnostics),
                    citations=citations,
                    explanations=explanations,
                )
                evaluated.append((ev, service, report))

            def rank_key(item: tuple[SchemeEvaluation, AssessmentService, AssessmentReport]):
                ev = item[0]
                if ev.eligibility in {"ELIGIBLE", "ELIGIBLE_PENDING_DOCS"}:
                    elig_group = 0
                elif ev.eligibility == "EXCLUDED_TO_MFN":
                    elig_group = 1
                elif ev.eligibility == "INELIGIBLE":
                    elig_group = 2
                else:
                    elig_group = 3
                duty_val = ev.applied_duty_percent if ev.applied_duty_percent is not None else Decimal("999")
                docs_ready = 0 if ev.eligibility == "ELIGIBLE" else 1
                revoked_rank = 1 if ev.tariff_revoked_on else 0
                return (elig_group, duty_val, docs_ready, revoked_rank, ev.agreement)

            evaluated.sort(key=rank_key)

            final_schemes: list[SchemeEvaluation] = []
            recommended = "PROHIBITED" if lartas.prohibited else "MFN"
            for idx, (ev, service, report) in enumerate(evaluated):
                if not lartas.prohibited and idx == 0 and ev.eligibility in {"ELIGIBLE", "ELIGIBLE_PENDING_DOCS"} and (
                    ev.duty_savings_percent is None or ev.duty_savings_percent > Decimal("0")
                ):
                    recommended = ev.agreement
                    if spec.include_plan:
                        ev = replace(ev, plan_steps=self._summarize_plan(service, report.run_id, include_lartas=True))
                elif not lartas.prohibited and spec.include_plan and agreements is not None and len(evaluated) == 1:
                    ev = replace(ev, plan_steps=self._summarize_plan(service, report.run_id, include_lartas=True))
                final_schemes.append(ev)

        return OptimizationResult(
            kb_version=self.kb.snapshot.version,
            kb_sha256=self.kb.snapshot.sha256,
            hs_code=hs_code_val,
            hs_iri=str(hs_iri),
            dispatch_country=dispatch_slug,
            destination_country=dest_slug,
            as_of_date=spec.as_of_date,
            recommended_agreement=recommended,
            mfn_duty_percent=mfn_duty,
            mfn_tax_breakdown=mfn_tax,
            lartas=lartas,
            schemes=tuple(final_schemes),
            tax_profile_citations=tax_profile_citations,
        )
