"""Outbound Indonesian export compliance, Export Lartas (ET/PE/LS/Ban), PEB, Back-to-Back CO, Third-Party Invoicing, and regional cumulation sourcing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from tempfile import TemporaryDirectory

from rdflib import Literal as RDFLiteral, RDFS, URIRef, XSD

from tradegraph.assessment.policies import assessment_graph
from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentReport, CompiledKB, PlanRequest, PlanningLimits
from tradegraph.kb.loader import TG
from tradegraph.kb.queries import prepare_local_query
from tradegraph.optimization.service import (
    FindingExplanationSummary,
    MaterialInput,
    MultiFtaOptimizer,
    OPT_EX,
    PlanStepSummary,
    ProvenanceCitation,
    _to_decimal,
)
from tradegraph.planning.engine import PlanningEngine


@dataclass(frozen=True, slots=True)
class ExportSpec:
    hs_code: str
    destination_country: str
    origin_country: str = "indonesia"
    as_of_date: str = "2025-09-15"
    fob_value: Decimal | None = None
    currency: str = "USD"
    quantity_mt: Decimal | None = None
    reference_price_usd: Decimal | None = None
    export_price_usd_per_mt: Decimal | None = None
    materials: tuple[MaterialInput, ...] = ()
    rvc_percent: Decimal | None = None
    wo_confirmed: bool | None = None
    process_kind: str | None = None
    process_satisfied: bool | None = None
    de_minimis_fob: tuple[Decimal, Decimal] | None = None
    de_minimis_weight: tuple[Decimal, Decimal] | None = None
    performed_operations: tuple[str, ...] | None = None
    transshipment_hub: str | None = None
    requires_back_to_back: bool = False
    third_party_invoice_country: str | None = None
    provided_export_docs: tuple[str, ...] = ()
    include_explain: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ExportSpec:
        dm_fob = raw.get("de_minimis_fob")
        dm_wt = raw.get("de_minimis_weight")
        ops = raw.get("performed_operations")
        docs = raw.get("provided_export_docs") or ()
        mats = raw.get("materials") or ()
        return cls(
            hs_code=str(raw["hs_code"]),
            destination_country=str(raw["destination_country"]),
            origin_country=str(raw.get("origin_country", "indonesia")),
            as_of_date=str(raw.get("as_of_date", "2025-09-15")),
            fob_value=_to_decimal(raw.get("fob_value")),
            currency=str(raw.get("currency", "USD")),
            quantity_mt=_to_decimal(raw.get("quantity_mt")),
            reference_price_usd=_to_decimal(raw.get("reference_price_usd")),
            export_price_usd_per_mt=_to_decimal(raw.get("export_price_usd_per_mt")),
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
            transshipment_hub=str(raw["transshipment_hub"]) if raw.get("transshipment_hub") else None,
            requires_back_to_back=bool(raw.get("requires_back_to_back", False)),
            third_party_invoice_country=(
                str(raw["third_party_invoice_country"]) if raw.get("third_party_invoice_country") else None
            ),
            provided_export_docs=tuple(str(d) for d in docs),
            include_explain=bool(raw.get("include_explain", False)),
        )


@dataclass(frozen=True, slots=True)
class ExportLartasRuleDetail:
    rule_id: str
    commodity_group: str | None
    supervision_mode: str
    prohibited: bool
    required_docs: tuple[str, ...]
    required_doc_labels: Mapping[str, str]
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
            "effective_from": self.effective_from,
            "source_clause": self.source_clause,
            "citation": self.citation.to_dict() if self.citation else None,
        }


@dataclass(frozen=True, slots=True)
class ExportLartasSummary:
    status: str  # PROHIBITED_EXPORT | EXPORT_PERMITS_MISSING | PEB_PENDING | EXPORT_READY
    prohibited: bool
    peb_filed: bool
    rules: tuple[ExportLartasRuleDetail, ...]
    required_docs: tuple[str, ...]
    missing_docs: tuple[str, ...]
    source_clauses: tuple[str, ...]
    citations: tuple[ProvenanceCitation, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "prohibited": self.prohibited,
            "peb_filed": self.peb_filed,
            "rules": [r.to_dict() for r in self.rules],
            "required_docs": list(self.required_docs),
            "missing_docs": list(self.missing_docs),
            "source_clauses": list(self.source_clauses),
            "citations": [c.to_dict() for c in self.citations],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class CumulationSuggestion:
    agreement: str
    failing_material_hs: str
    current_origin: str | None
    recommended_partner_countries: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "agreement": self.agreement,
            "failing_material_hs": self.failing_material_hs,
            "current_origin": self.current_origin,
            "recommended_partner_countries": list(self.recommended_partner_countries),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ExportSchemeOption:
    agreement: str
    agreement_label: str
    facility_code: str | None
    origin_proof_doc: str | None
    origin_proof_label: str | None
    psr_level: str
    concise_rule: str | None
    eligibility: str  # ELIGIBLE | INELIGIBLE_ROO | BLOCKED_NO_BACK_TO_BACK | BLOCKED_NON_PARTY_HUB | PROHIBITED_EXPORT
    origin_status: str  # SATISFIED | NOT_SATISFIED | UNDETERMINED
    back_to_back_allowed: bool
    back_to_back_status: str  # NOT_REQUESTED | ALLOWED | DISALLOWED_BILATERAL | HUB_NOT_A_PARTY
    third_party_invoicing_allowed: bool
    third_party_invoice_instruction: str | None
    cumulation_suggestions: tuple[CumulationSuggestion, ...]
    source_clauses: tuple[str, ...]
    citations: tuple[ProvenanceCitation, ...] = ()
    explanations: tuple[FindingExplanationSummary, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "agreement": self.agreement,
            "agreement_label": self.agreement_label,
            "facility_code": self.facility_code,
            "origin_proof_doc": self.origin_proof_doc,
            "origin_proof_label": self.origin_proof_label,
            "psr_level": self.psr_level,
            "concise_rule": self.concise_rule,
            "eligibility": self.eligibility,
            "origin_status": self.origin_status,
            "back_to_back_allowed": self.back_to_back_allowed,
            "back_to_back_status": self.back_to_back_status,
            "third_party_invoicing_allowed": self.third_party_invoicing_allowed,
            "third_party_invoice_instruction": self.third_party_invoice_instruction,
            "cumulation_suggestions": [s.to_dict() for s in self.cumulation_suggestions],
            "source_clauses": list(self.source_clauses),
            "citations": [c.to_dict() for c in self.citations],
            "explanations": [e.to_dict() for e in self.explanations],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class ExportChecklistStep:
    step_number: int
    category: str  # LARTAS | CUSTOMS_PEB | ORIGIN_PROOF | TRANSSHIPMENT | INVOICING
    title: str
    required_doc: str | None
    legal_basis: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "step_number": self.step_number,
            "category": self.category,
            "title": self.title,
            "required_doc": self.required_doc,
            "legal_basis": self.legal_basis,
        }


@dataclass(frozen=True, slots=True)
class ExportDutyAssessment:
    status: str  # APPLICABLE | ZERO_RATE | REFERENCE_PRICE_REQUIRED | NOT_SUBJECT | PROHIBITED_EXPORT
    subject_to_export_duty: bool
    commodity_group: str | None = None
    duty_basis: str | None = None  # AD_VALOREM | SPECIFIC_USD_PER_MT
    tier_column: int | None = None
    reference_price_usd: Decimal | None = None
    reference_price_band: str | None = None
    rate_percent: Decimal | None = None
    specific_rate_usd_per_mt: Decimal | None = None
    quantity_mt: Decimal | None = None
    taxable_export_value_usd: Decimal | None = None
    estimated_duty_usd: Decimal | None = None
    rule_id: str | None = None
    effective_from: str | None = None
    source_clauses: tuple[str, ...] = ()
    citations: tuple[ProvenanceCitation, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "subject_to_export_duty": self.subject_to_export_duty,
            "commodity_group": self.commodity_group,
            "duty_basis": self.duty_basis,
            "tier_column": self.tier_column,
            "reference_price_usd": f"{self.reference_price_usd:.2f}" if self.reference_price_usd is not None else None,
            "reference_price_band": self.reference_price_band,
            "rate_percent": f"{self.rate_percent:.2f}" if self.rate_percent is not None else None,
            "specific_rate_usd_per_mt": (
                f"{self.specific_rate_usd_per_mt:.2f}" if self.specific_rate_usd_per_mt is not None else None
            ),
            "quantity_mt": f"{self.quantity_mt:.3f}".rstrip("0").rstrip(".") if self.quantity_mt is not None else None,
            "taxable_export_value_usd": (
                f"{self.taxable_export_value_usd:.2f}" if self.taxable_export_value_usd is not None else None
            ),
            "estimated_duty_usd": f"{self.estimated_duty_usd:.2f}" if self.estimated_duty_usd is not None else None,
            "rule_id": self.rule_id,
            "effective_from": self.effective_from,
            "source_clauses": list(self.source_clauses),
            "citations": [c.to_dict() for c in self.citations],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class ExportEvaluationResult:
    kb_version: str
    kb_sha256: str
    hs_code: str
    hs_iri: str
    origin_country: str
    destination_country: str
    as_of_date: str
    transshipment_hub: str | None
    requires_back_to_back: bool
    third_party_invoice_country: str | None
    recommended_agreement: str  # agreement slug | "NON_PREFERENTIAL" | "PROHIBITED_EXPORT"
    export_lartas: ExportLartasSummary
    export_duty: ExportDutyAssessment
    schemes: tuple[ExportSchemeOption, ...]
    checklist: tuple[ExportChecklistStep, ...]
    cumulation_opportunities: tuple[CumulationSuggestion, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "kb_version": self.kb_version,
            "kb_sha256": self.kb_sha256,
            "hs_code": self.hs_code,
            "hs_iri": self.hs_iri,
            "origin_country": self.origin_country,
            "destination_country": self.destination_country,
            "as_of_date": self.as_of_date,
            "transshipment_hub": self.transshipment_hub,
            "requires_back_to_back": self.requires_back_to_back,
            "third_party_invoice_country": self.third_party_invoice_country,
            "recommended_agreement": self.recommended_agreement,
            "export_lartas": self.export_lartas.to_dict(),
            "export_duty": self.export_duty.to_dict(),
            "schemes": [s.to_dict() for s in self.schemes],
            "checklist": [c.to_dict() for c in self.checklist],
            "cumulation_opportunities": [c.to_dict() for c in self.cumulation_opportunities],
        }


class ExportComplianceAdvisor:
    """KB-driven outbound Indonesian export compliance, transshipment/Back-to-Back CO, and cumulation advisor."""

    def __init__(self, optimizer: MultiFtaOptimizer):
        self._opt = optimizer
        self.kb: CompiledKB = optimizer.kb
        self._owl = optimizer._owl

    def assess_export_lartas(self, hs_iri: URIRef, spec: ExportSpec) -> ExportLartasSummary:
        jur = TG.jurisdictionIndonesiaCustoms
        definition = self.kb.query_catalog.get("export-lartas")
        provided = set(spec.provided_export_docs)
        peb_filed = "pebDeclaration" in provided

        base_clauses = ["pmk155-pasal-2-peb", "pmk155-pasal-4-ekspor"]
        if definition is None:
            status = "EXPORT_READY" if peb_filed else "PEB_PENDING"
            return ExportLartasSummary(
                status=status, prohibited=False, peb_filed=peb_filed, rules=(),
                required_docs=("pebDeclaration",),
                missing_docs=() if peb_filed else ("pebDeclaration",),
                source_clauses=tuple(base_clauses),
                citations=self._opt.resolve_citations(base_clauses) if spec.include_explain else (),
            )

        query = prepare_local_query(definition.sparql, "SelectQuery")
        rows = list(self._owl.query(query, initBindings={
            "code": hs_iri,
            "jurisdiction": jur,
            "asOfDate": RDFLiteral(spec.as_of_date),
        }))

        grouped: dict[str, dict] = {}
        for row in rows:
            d = row.asdict()
            rule_id = str(d["rule"]).split("#")[-1]
            entry = grouped.setdefault(rule_id, {
                "commodity_group": str(d["commodityGroup"]) if d.get("commodityGroup") else None,
                "mode": str(d["supervisionLabel"]) if d.get("supervisionLabel") else str(d["supervisionMode"]).split("#")[-1],
                "prohibited": str(d.get("prohibited")).lower() == "true",
                "docs": {},
                "effective_from": str(d["effectiveFrom"]) if d.get("effectiveFrom") else None,
                "clause": str(d["sourceClause"]).split("#")[-1] if d.get("sourceClause") else None,
            })
            if d.get("requiredDocType"):
                doc_id = str(d["requiredDocType"]).split("#")[-1]
                doc_label = str(d["requiredDocLabel"]) if d.get("requiredDocLabel") else doc_id
                entry["docs"][doc_id] = doc_label

        rules: list[ExportLartasRuleDetail] = []
        req_lartas: set[str] = set()
        clauses: set[str] = set(base_clauses)
        diagnostics: list[str] = []
        prohibited = False

        for rule_id in sorted(grouped):
            g = grouped[rule_id]
            clause = g["clause"]
            if clause:
                clauses.add(clause)
            cit = self._opt.resolve_citation(clause) if clause and spec.include_explain else None
            req_docs = tuple(sorted(g["docs"]))
            rules.append(ExportLartasRuleDetail(
                rule_id=rule_id,
                commodity_group=g["commodity_group"],
                supervision_mode=g["mode"],
                prohibited=g["prohibited"],
                required_docs=req_docs,
                required_doc_labels=dict(sorted(g["docs"].items())),
                effective_from=g["effective_from"],
                source_clause=clause,
                citation=cit,
            ))
            if g["prohibited"]:
                prohibited = True
                reg = self._opt.resolve_citation(clause).regulation_number if clause else rule_id
                diagnostics.append(
                    f"BARANG DILARANG EKSPOR ({g['commodity_group'] or rule_id}) berdasarkan {reg}; PEB dan SKA tidak dapat diterbitkan."
                )
                continue
            req_lartas.update(req_docs)

        all_required = sorted(req_lartas) + ([] if prohibited else ["pebDeclaration"])
        missing_lartas = sorted(d for d in req_lartas if d not in provided)
        missing_all = sorted(d for d in all_required if d not in provided)

        if prohibited:
            status = "PROHIBITED_EXPORT"
        elif missing_lartas:
            status = "EXPORT_PERMITS_MISSING"
            diagnostics.append(
                f"Wajib melengkapi perizinan Lartas Ekspor Kemendag sebelum pengajuan PEB: {', '.join(missing_lartas)}."
            )
        elif not peb_filed:
            status = "PEB_PENDING"
            diagnostics.append("Lartas Ekspor terpenuhi/bebas; wajib menyampaikan PEB (BC 3.0) untuk memperoleh NPE (PMK 155/PMK.04/2022).")
        else:
            status = "EXPORT_READY"

        citations = self._opt.resolve_citations(clauses) if spec.include_explain else ()
        return ExportLartasSummary(
            status=status,
            prohibited=prohibited,
            peb_filed=peb_filed,
            rules=tuple(rules),
            required_docs=tuple(all_required),
            missing_docs=tuple(missing_all),
            source_clauses=tuple(sorted(clauses)),
            citations=citations,
            diagnostics=tuple(diagnostics),
        )

    def assess_export_duty(
        self,
        hs_iri: URIRef,
        spec: ExportSpec,
        *,
        prohibited: bool = False,
    ) -> ExportDutyAssessment:
        """Evaluate Indonesian Export Duty (Bea Keluar — PMK 38/2024 jo. PMK 68/2025) strictly from KB rules."""
        if prohibited:
            return ExportDutyAssessment(
                status="PROHIBITED_EXPORT",
                subject_to_export_duty=False,
                quantity_mt=spec.quantity_mt,
                reference_price_usd=spec.reference_price_usd,
                diagnostics=("Barang termasuk kategori Dilarang Ekspor; perhitungan Bea Keluar tidak berlaku.",),
            )

        definition = self.kb.query_catalog.get("export-duty")
        if definition is None:
            return ExportDutyAssessment(
                status="NOT_SUBJECT",
                subject_to_export_duty=False,
                estimated_duty_usd=Decimal("0.00"),
            )

        query = prepare_local_query(definition.sparql, "SelectQuery")
        rows = [
            row.asdict()
            for row in self._owl.query(query, initBindings={
                "code": hs_iri,
                "jurisdiction": TG.jurisdictionIndonesiaCustoms,
                "asOfDate": RDFLiteral(spec.as_of_date),
            })
        ]
        if not rows:
            return ExportDutyAssessment(
                status="NOT_SUBJECT",
                subject_to_export_duty=False,
                quantity_mt=spec.quantity_mt,
                reference_price_usd=spec.reference_price_usd,
                estimated_duty_usd=Decimal("0.00"),
                diagnostics=("Kode HS ini tidak termasuk objek pengenaan Bea Keluar aktif pada tanggal penilaian.",),
            )

        # Deduplicate rows by rule IRI
        rules_by_id: dict[str, dict] = {}
        for d in rows:
            rule_id = str(d["rule"]).split("#")[-1]
            if rule_id not in rules_by_id:
                rules_by_id[rule_id] = {
                    "rule_id": rule_id,
                    "commodity_group": str(d["commodityGroup"]) if d.get("commodityGroup") else None,
                    "basis": str(d["basisLabel"]) if d.get("basisLabel") else (
                        "SPECIFIC_USD_PER_MT" if d.get("basis") == TG.exportDutySpecificUsdPerMt else "AD_VALOREM"
                    ),
                    "rate_percent": _to_decimal(d.get("ratePercent")),
                    "specific_usd": _to_decimal(d.get("specificUsdPerMt")),
                    "hr_min": _to_decimal(d.get("refPriceMinExcl")),
                    "hr_max": _to_decimal(d.get("refPriceMaxIncl")),
                    "tier": int(str(d["tierColumn"])) if d.get("tierColumn") is not None else None,
                    "effective_from": str(d["effectiveFrom"]) if d.get("effectiveFrom") else None,
                    "clause": str(d["sourceClause"]).split("#")[-1] if d.get("sourceClause") else None,
                }

        candidates = sorted(
            rules_by_id.values(),
            key=lambda item: (item["tier"] if item["tier"] is not None else 0, item["rule_id"]),
        )
        is_tiered = any(c["hr_min"] is not None or c["hr_max"] is not None for c in candidates)
        clauses: set[str] = {"pmk38-pasal-2-objek", "pmk38-pasal-13-formula"}

        def _fmt_band(hr_min: Decimal | None, hr_max: Decimal | None) -> str | None:
            if hr_min is None and hr_max is None:
                return None
            if hr_min is None and hr_max is not None:
                return f"<= USD {hr_max:,.2f} / MT"
            if hr_min is not None and hr_max is not None:
                return f"> USD {hr_min:,.2f} s.d. USD {hr_max:,.2f} / MT"
            return f"> USD {hr_min:,.2f} / MT"

        if is_tiered:
            if spec.reference_price_usd is None:
                first = candidates[0]
                if first["clause"]:
                    clauses.add(first["clause"])
                if first["effective_from"] and first["effective_from"] >= "2025-10-22":
                    clauses.add("pmk68-pasal-i-perubahan")
                return ExportDutyAssessment(
                    status="REFERENCE_PRICE_REQUIRED",
                    subject_to_export_duty=True,
                    commodity_group=first["commodity_group"],
                    duty_basis=first["basis"],
                    quantity_mt=spec.quantity_mt,
                    effective_from=first["effective_from"],
                    source_clauses=tuple(sorted(clauses)),
                    citations=self._opt.resolve_citations(clauses) if spec.include_explain else (),
                    diagnostics=(
                        f"Komoditas {first['commodity_group'] or 'ekspor'} dikenakan Bea Keluar progresif ({len(candidates)} kolom); "
                        "masukkan Harga Referensi (HR) Kemendag dalam USD/MT untuk menentukan kolom tarif aktif.",
                    ),
                )
            hr = spec.reference_price_usd
            matched = None
            for cand in candidates:
                lo_ok = cand["hr_min"] is None or hr > cand["hr_min"]
                hi_ok = cand["hr_max"] is None or hr <= cand["hr_max"]
                if lo_ok and hi_ok:
                    matched = cand
                    break
            if matched is None:
                matched = candidates[-1]
        else:
            # Prefer latest effective_from if multiple non-tiered rules overlap
            candidates.sort(key=lambda item: (item["effective_from"] or "", item["rule_id"]), reverse=True)
            matched = candidates[0]

        if matched["clause"]:
            clauses.add(matched["clause"])
        if matched["effective_from"] and matched["effective_from"] >= "2025-10-22":
            clauses.add("pmk68-pasal-i-perubahan")

        diagnostics: list[str] = []
        basis = matched["basis"]
        band = _fmt_band(matched["hr_min"], matched["hr_max"])
        taxable_val: Decimal | None = None
        est_duty: Decimal | None = None
        is_zero = False

        if basis == "SPECIFIC_USD_PER_MT":
            spec_rate = matched["specific_usd"] or Decimal("0.00")
            is_zero = spec_rate == Decimal("0")
            if spec.quantity_mt is not None:
                est_duty = (spec_rate * spec.quantity_mt).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            elif is_zero:
                est_duty = Decimal("0.00")
            else:
                diagnostics.append(
                    f"Tarif Bea Keluar spesifik aktif USD {spec_rate:.2f}/MT (Kolom {matched['tier']}); masukkan quantity_mt untuk estimasi total Bea Keluar."
                )
        else:
            pct = matched["rate_percent"] or Decimal("0.00")
            is_zero = pct == Decimal("0")
            if spec.export_price_usd_per_mt is not None and spec.quantity_mt is not None:
                taxable_val = (spec.export_price_usd_per_mt * spec.quantity_mt).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            elif spec.fob_value is not None:
                taxable_val = spec.fob_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if not is_zero:
                    diagnostics.append(
                        "Estimasi Bea Keluar ad valorem menggunakan nilai FOB sebagai proksi (HPE x Volume sesuai PMK 38/2024 Pasal 13 ayat 1 huruf a)."
                    )
            if taxable_val is not None:
                est_duty = (taxable_val * pct / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            elif is_zero:
                est_duty = Decimal("0.00")
            else:
                diagnostics.append(
                    f"Tarif Bea Keluar ad valorem aktif {pct:.2f}%; masukkan export_price_usd_per_mt + quantity_mt (atau fob_value) untuk menghitung estimasi Bea Keluar."
                )

        status = "ZERO_RATE" if is_zero else "APPLICABLE"
        return ExportDutyAssessment(
            status=status,
            subject_to_export_duty=True,
            commodity_group=matched["commodity_group"],
            duty_basis=basis,
            tier_column=matched["tier"],
            reference_price_usd=spec.reference_price_usd,
            reference_price_band=band,
            rate_percent=matched["rate_percent"],
            specific_rate_usd_per_mt=matched["specific_usd"],
            quantity_mt=spec.quantity_mt,
            taxable_export_value_usd=taxable_val,
            estimated_duty_usd=est_duty,
            rule_id=matched["rule_id"],
            effective_from=matched["effective_from"],
            source_clauses=tuple(sorted(clauses)),
            citations=self._opt.resolve_citations(clauses) if spec.include_explain else (),
            diagnostics=tuple(diagnostics),
        )

    @staticmethod
    def _has_roo_inputs(spec: ExportSpec) -> bool:
        return bool(
            spec.materials
            or spec.rvc_percent is not None
            or spec.wo_confirmed is not None
            or (spec.process_kind is not None and spec.process_satisfied is not None)
            or spec.de_minimis_fob is not None
            or spec.de_minimis_weight is not None
            or spec.performed_operations is not None
        )

    def _check_back_to_back(
        self,
        agr_iri: URIRef,
        *,
        requested: bool,
        hub_iri: URIRef | None,
    ) -> tuple[bool, str]:
        """Evaluate Back-to-Back CO eligibility & hub party membership declaratively via KB SPARQL query."""
        qdef = self.kb.query_catalog["export-back-to-back-check"]
        query = prepare_local_query(qdef.sparql, "SelectQuery")
        rows = list(self._owl.query(query, initBindings={
            "agreement": agr_iri,
            "requested": RDFLiteral(requested, datatype=XSD.boolean),
            "hasHub": RDFLiteral(hub_iri is not None, datatype=XSD.boolean),
            "hub": hub_iri if hub_iri is not None else agr_iri,
        }))
        if not rows:
            return False, "NOT_REQUESTED" if not requested else "DISALLOWED_BILATERAL"
        d = rows[0].asdict()
        return str(d.get("allowed")).lower() == "true", str(d["status"])

    def _suggest_cumulation(
        self,
        agr_iri: URIRef,
        agr_slug: str,
        origin_iri: URIRef,
        service: AssessmentService,
        report: AssessmentReport,
    ) -> tuple[CumulationSuggestion, ...]:
        """Diagnose CTC-failing materials and partner cumulation countries declaratively on the HermiT-materialized graph."""
        if report.execution_status != "CONVERGED":
            return ()
        qdef = self.kb.query_catalog.get("cumulation-candidates")
        if qdef is None:
            return ()
        _, case = service.provenance.load_run(report.run_id)
        data, _ = assessment_graph(
            self.kb,
            case,
            ((f.triple, f.support) for f in report.findings),
            report.validation_report,
        )
        query = prepare_local_query(qdef.sparql, "SelectQuery")
        rows = list(data.query(query, initBindings={
            "agreement": agr_iri,
            "product": OPT_EX.product,
            "origin": origin_iri,
        }))
        grouped: dict[str, dict[str, object]] = {}
        for row in rows:
            d = row.asdict()
            mat_hs = str(d["materialHs"])
            entry = grouped.setdefault(mat_hs, {
                "origin": str(d["currentOrigin"]) if d.get("currentOrigin") is not None else None,
                "kind": str(d["criterionKind"]).split("#")[-1],
                "partners": set(),
            })
            entry["partners"].add(str(d["partner"]).split("#")[-1])

        suggestions: list[CumulationSuggestion] = []
        for mat_hs in sorted(grouped):
            info = grouped[mat_hs]
            partner_slugs = tuple(sorted(info["partners"]))
            kind_name = str(info["kind"])
            suggestions.append(CumulationSuggestion(
                agreement=agr_slug,
                failing_material_hs=mat_hs,
                current_origin=info["origin"] if isinstance(info["origin"], str) else None,
                recommended_partner_countries=partner_slugs,
                reason=(
                    f"Bahan {mat_hs} gagal kriteria {kind_name} untuk {agr_slug.upper()}; "
                    f"pengadaan dari negara mitra ({', '.join(partner_slugs[:5])}{'...' if len(partner_slugs) > 5 else ''}) "
                    f"dengan bukti asal terverifikasi mengaktifkan akumulasi regional."
                ),
            ))
        return tuple(suggestions)

    def _load_checklist_templates(self) -> dict[str, dict[str, object]]:
        """Load ordered export checklist step templates & legal bases declaratively from the KB."""
        qdef = self.kb.query_catalog["export-checklist-templates"]
        query = prepare_local_query(qdef.sparql, "SelectQuery")
        templates: dict[str, dict[str, object]] = {}
        for row in self._owl.query(query):
            d = row.asdict()
            templates[str(d["code"])] = {
                "category": str(d["category"]),
                "order": int(str(d["order"])),
                "title": str(d["titleTemplate"]),
                "legal_basis": str(d["legalBasisTemplate"]),
            }
        return templates

    def plan_export_clearance(
        self,
        service: AssessmentService,
        run_id: str,
    ) -> tuple[PlanStepSummary, ...]:
        """Run BFS PlanningEngine over KB export actions toward goal-export-clearance-ready."""
        goal_iri = str(TG["goal-export-clearance-ready"])
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
        steps: list[PlanStepSummary] = []
        for step in candidates[0].steps:
            action = self.kb.action_catalog.get(step.action_id)
            label = action.label if action else step.action_id
            added = tuple(
                f"{str(p).split('#')[-1]} -> {str(o).split('#')[-1]}"
                for _, p, o in step.effects.added
            )
            clauses = tuple(str(c).split("#")[-1] for c in step.source_clauses)
            steps.append(PlanStepSummary(step.action_id, label, added, clauses))
        return tuple(steps)

    def evaluate(self, spec: ExportSpec) -> ExportEvaluationResult:
        hs_iri = self._opt.resolve_hs_iri(spec.hs_code)
        origin_iri = self._opt.resolve_country_iri(spec.origin_country)
        dest_iri = self._opt.resolve_country_iri(spec.destination_country)
        if str(origin_iri) != str(TG.indonesia):
            raise ValueError("Currently ExportComplianceAdvisor supports Indonesian outbound exports (indonesia / ID).")
        if origin_iri == dest_iri:
            raise ValueError("Export origin and destination countries must be different.")

        hub_iri = self._opt.resolve_country_iri(spec.transshipment_hub) if spec.transshipment_hub else None
        hub_slug = str(hub_iri).split("#")[-1] if hub_iri is not None else None
        tpi_country = spec.third_party_invoice_country.strip() if spec.third_party_invoice_country else None

        hs_code_val = str(self._owl.value(hs_iri, TG.codeValue) or spec.hs_code)
        origin_slug = str(origin_iri).split("#")[-1]
        dest_slug = str(dest_iri).split("#")[-1]

        export_lartas = self.assess_export_lartas(hs_iri, spec)
        export_duty = self.assess_export_duty(hs_iri, spec, prohibited=export_lartas.prohibited)

        opt_def = self.kb.query_catalog["export-fta-options"]
        query = prepare_local_query(opt_def.sparql, "SelectQuery")
        rows = list(self._owl.query(query, initBindings={
            "code": hs_iri,
            "origin": origin_iri,
            "destination": dest_iri,
        }))

        schemes: list[ExportSchemeOption] = []
        scheme_priorities: dict[str, int] = {}
        all_cumul: list[CumulationSuggestion] = []
        seen_agr: set[str] = set()
        run_semantic_roo = (not export_lartas.prohibited) and self._has_roo_inputs(spec)

        with TemporaryDirectory(prefix="tradegraph-export-") as tmp:
            repo = CaseRepository(Path(tmp) / "export.sqlite3") if run_semantic_roo else None
            for row in rows:
                d = row.asdict()
                agr_iri = d["agreement"]
                agr_slug = str(agr_iri).split("#")[-1]
                if agr_slug in seen_agr:
                    continue
                seen_agr.add(agr_slug)
                scheme_priorities[agr_slug] = (
                    int(str(d["exportPreferencePriority"]))
                    if d.get("exportPreferencePriority") is not None else 99
                )

                psr_iri = d.get("psr") if isinstance(d.get("psr"), URIRef) else None
                b2b_allowed, b2b_status = self._check_back_to_back(
                    agr_iri, requested=spec.requires_back_to_back, hub_iri=hub_iri,
                )
                tpi_allowed = str(d.get("thirdPartyAllowed")).lower() == "true"
                tpi_req = str(d["thirdPartyReq"]) if d.get("thirdPartyReq") else None

                clause_ids: set[str] = set()
                for c in self._owl.objects(agr_iri, TG.hasSourceClause):
                    clause_ids.add(str(c).split("#")[-1])
                if psr_iri is not None:
                    for c in self._owl.objects(psr_iri, TG.hasSourceClause):
                        clause_ids.add(str(c).split("#")[-1])
                if d.get("backToBackClause"):
                    clause_ids.add(str(d["backToBackClause"]).split("#")[-1])
                if d.get("thirdPartyClause"):
                    clause_ids.add(str(d["thirdPartyClause"]).split("#")[-1])

                diags: list[str] = []
                explanations: tuple[FindingExplanationSummary, ...] = ()
                origin_status = "UNDETERMINED"
                suggs: tuple[CumulationSuggestion, ...] = ()

                if psr_iri is None:
                    diags.append("PSR skema ini belum terpetakan di KB.")
                elif run_semantic_roo and repo is not None:
                    report, service = self._opt._build_and_assess(
                        repo, spec, agr_slug, hs_iri, origin_iri, dest_iri,
                        TG.jurisdictionIndonesiaCustoms,
                        procedure_iri=TG.commercialExport,
                    )
                    statuses = {f.policy_id: f.status for f in report.domain_findings}
                    for f in report.domain_findings:
                        for c in f.source_clauses:
                            clause_ids.add(str(c).split("#")[-1])
                    origin_status = statuses.get(f"{agr_slug}-psr-overall", "UNDETERMINED")
                    if statuses.get(f"{agr_slug}-psr-minimal-ops") == "NOT_SATISFIED":
                        ops_txt = ", ".join(spec.performed_operations or ())
                        diags.append(
                            f"Seluruh proses produksi ({ops_txt}) termasuk proses minimal yang tidak memenuhi kualifikasi asal ({agr_slug.upper()})."
                        )
                    elif origin_status == "NOT_SATISFIED":
                        diags.append(f"Belum memenuhi kriteria asal PSR {agr_slug.upper()} berdasarkan BOM/RVC saat ini.")
                    if spec.include_explain and report.execution_status == "CONVERGED":
                        explanations = self._opt._explain_key_findings(service, report, agr_slug)
                    if origin_status != "SATISFIED" and statuses.get(f"{agr_slug}-psr-minimal-ops") != "NOT_SATISFIED":
                        suggs = self._suggest_cumulation(agr_iri, agr_slug, origin_iri, service, report)
                        all_cumul.extend(suggs)

                if b2b_status == "DISALLOWED_BILATERAL":
                    diags.append(
                        f"SKA Back-to-Back tidak berlaku pada skema bilateral {agr_slug.upper()}; gunakan pengiriman langsung/transit atau skema multilateral."
                    )
                elif b2b_status == "HUB_NOT_A_PARTY":
                    diags.append(
                        f"Hub transshipment ({hub_slug}) bukan Negara Anggota {agr_slug.upper()}, sehingga tidak dapat menerbitkan Back-to-Back CO."
                    )
                elif b2b_status == "ALLOWED" and hub_slug:
                    diags.append(
                        f"Hub transshipment ({hub_slug}) adalah Negara Anggota {agr_slug.upper()} dan berwenang menerbitkan Back-to-Back CO."
                    )

                if tpi_country and tpi_allowed and tpi_req:
                    diags.append(f"Third-Party Invoicing ({tpi_country}): {tpi_req}")

                if export_lartas.prohibited:
                    elig = "PROHIBITED_EXPORT"
                elif b2b_status == "DISALLOWED_BILATERAL":
                    elig = "BLOCKED_NO_BACK_TO_BACK"
                elif b2b_status == "HUB_NOT_A_PARTY":
                    elig = "BLOCKED_NON_PARTY_HUB"
                elif origin_status == "NOT_SATISFIED":
                    elig = "INELIGIBLE_ROO"
                else:
                    elig = "ELIGIBLE"

                sorted_clauses = tuple(sorted(clause_ids))
                schemes.append(ExportSchemeOption(
                    agreement=agr_slug,
                    agreement_label=str(d["agreementLabel"]) if d.get("agreementLabel") else agr_slug.upper(),
                    facility_code=str(d["facilityCode"]) if d.get("facilityCode") else None,
                    origin_proof_doc=str(d["primaryOriginDoc"]).split("#")[-1] if d.get("primaryOriginDoc") else None,
                    origin_proof_label=str(d["primaryOriginDocLabel"]) if d.get("primaryOriginDocLabel") else None,
                    psr_level=str(d["psrLevel"]) if d.get("psrLevel") else "NONE",
                    concise_rule=str(d["conciseRule"]) if d.get("conciseRule") else None,
                    eligibility=elig,
                    origin_status=origin_status,
                    back_to_back_allowed=b2b_allowed,
                    back_to_back_status=b2b_status,
                    third_party_invoicing_allowed=tpi_allowed,
                    third_party_invoice_instruction=tpi_req if tpi_country else None,
                    cumulation_suggestions=suggs,
                    source_clauses=sorted_clauses,
                    citations=self._opt.resolve_citations(sorted_clauses) if spec.include_explain else (),
                    explanations=explanations,
                    diagnostics=tuple(diags),
                ))

        # Rank using eligibility, origin status, and KB-declared exportPreferencePriority
        def rank_key(opt: ExportSchemeOption):
            elig_rank = {"ELIGIBLE": 0, "INELIGIBLE_ROO": 1, "BLOCKED_NON_PARTY_HUB": 2, "BLOCKED_NO_BACK_TO_BACK": 3, "PROHIBITED_EXPORT": 4}.get(opt.eligibility, 5)
            orig_rank = {"SATISFIED": 0, "UNDETERMINED": 1, "NOT_SATISFIED": 2}.get(opt.origin_status, 3)
            agr_pref = scheme_priorities.get(opt.agreement, 99)
            return (elig_rank, orig_rank, agr_pref, opt.agreement)

        schemes.sort(key=rank_key)

        if export_lartas.prohibited:
            recommended = "PROHIBITED_EXPORT"
        elif schemes and schemes[0].eligibility == "ELIGIBLE":
            recommended = schemes[0].agreement
        else:
            recommended = "NON_PREFERENTIAL"

        # Render ordered outbound compliance checklist from KB ExportChecklistTemplate definitions
        checklist: list[ExportChecklistStep] = []
        if not export_lartas.prohibited:
            templates = self._load_checklist_templates()
            rendered_entries: list[tuple[int, str, str, str, str | None, str]] = []

            def add_from_template(code: str, *, required_doc: str | None, params: Mapping[str, str], sort_tie: str = "") -> None:
                tpl = templates[code]
                rendered_entries.append((
                    int(tpl["order"]),
                    sort_tie,
                    str(tpl["category"]),
                    str(tpl["title"]).format(**params),
                    required_doc,
                    str(tpl["legal_basis"]).format(**params),
                ))

            for idx, doc in enumerate(export_lartas.missing_docs):
                if doc == "pebDeclaration":
                    continue
                doc_label = str(self._owl.value(TG[doc], RDFS.label) or doc)
                add_from_template(
                    "LARTAS_MISSING",
                    required_doc=doc,
                    params={"doc_label": doc_label},
                    sort_tie=f"{idx:03d}-{doc}",
                )
            if export_duty.status == "APPLICABLE":
                if export_duty.duty_basis == "SPECIFIC_USD_PER_MT":
                    rate_desc = f"USD {export_duty.specific_rate_usd_per_mt:.2f}/MT"
                else:
                    rate_desc = f"{export_duty.rate_percent:.2f}% ad valorem"
                amt_note = (
                    f" (estimasi USD {export_duty.estimated_duty_usd:,.2f})"
                    if export_duty.estimated_duty_usd is not None else ""
                )
                add_from_template(
                    "EXPORT_DUTY_APPLICABLE",
                    required_doc="pebDeclaration",
                    params={"rate_desc": rate_desc, "amt_note": amt_note},
                )
            elif export_duty.status == "REFERENCE_PRICE_REQUIRED":
                add_from_template(
                    "EXPORT_DUTY_HR_REQUIRED",
                    required_doc="pebDeclaration",
                    params={},
                )
            if not export_lartas.peb_filed:
                add_from_template(
                    "PEB_PENDING",
                    required_doc="pebDeclaration",
                    params={},
                )
            win = next((s for s in schemes if s.agreement == recommended), None)
            if win is not None:
                win_params = {
                    "origin_proof_label": win.origin_proof_label or win.origin_proof_doc or "SKA",
                    "agreement_upper": win.agreement.upper(),
                    "agreement_label": win.agreement_label,
                    "facility_code": win.facility_code or "-",
                    "hub_label": hub_slug or "Negara Anggota",
                    "tpi_instruction": win.third_party_invoice_instruction or "",
                    "tpi_country": tpi_country or "",
                }
                add_from_template("ORIGIN_PROOF", required_doc=win.origin_proof_doc, params=win_params)
                if spec.requires_back_to_back and win.back_to_back_status == "ALLOWED":
                    add_from_template("BACK_TO_BACK", required_doc=win.origin_proof_doc, params=win_params)
                if tpi_country and win.third_party_invoice_instruction:
                    add_from_template("THIRD_PARTY_INVOICE", required_doc=win.origin_proof_doc, params=win_params)

            rendered_entries.sort(key=lambda item: (item[0], item[1]))
            checklist = [
                ExportChecklistStep(
                    step_number=step_no,
                    category=cat,
                    title=title,
                    required_doc=req_doc,
                    legal_basis=basis,
                )
                for step_no, (_, _, cat, title, req_doc, basis) in enumerate(rendered_entries, 1)
            ]

        return ExportEvaluationResult(
            kb_version=self.kb.snapshot.version,
            kb_sha256=self.kb.snapshot.sha256,
            hs_code=hs_code_val,
            hs_iri=str(hs_iri),
            origin_country=origin_slug,
            destination_country=dest_slug,
            as_of_date=spec.as_of_date,
            transshipment_hub=hub_slug,
            requires_back_to_back=spec.requires_back_to_back,
            third_party_invoice_country=tpi_country,
            recommended_agreement=recommended,
            export_lartas=export_lartas,
            export_duty=export_duty,
            schemes=tuple(schemes),
            checklist=tuple(checklist),
            cumulation_opportunities=tuple(all_cumul),
        )
