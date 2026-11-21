"""Regulatory Change Diff & Portfolio Impact Analyzer (Temporal What-If) with Excel/CSV Audit Reporting."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
import io
import re

from rdflib import RDF, RDFS, URIRef

from tradegraph.kb.loader import TG
from tradegraph.optimization.batch_service import (
    BatchMode,
    BatchRowOutcome,
    BatchScreener,
    ScreeningDepth,
    normalize_batch_row,
)
from tradegraph.optimization.service import MultiFtaOptimizer, ProvenanceCitation


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_LEGAL_KIND_LABELS: dict[str, str] = {
    "relRevokes": "REVOKES",
    "relAmends": "AMENDS",
    "relPartiallyRevokes": "PARTIALLY_REVOKES",
    "relRelated": "RELATED",
}


def _validate_date_window(baseline_date: str, target_date: str) -> tuple[str, str]:
    b = baseline_date.strip()
    t = target_date.strip()
    if not _ISO_DATE_RE.match(b) or not _ISO_DATE_RE.match(t):
        raise ValueError("baseline_date and target_date must use YYYY-MM-DD format.")
    if b >= t:
        raise ValueError(f"target_date ({t}) must be strictly later than baseline_date ({b}).")
    return b, t


def _fmt_dec(val: Decimal | None) -> str | None:
    return f"{val:.2f}" if val is not None else None


@dataclass(frozen=True, slots=True)
class RegulatoryChangeEvent:
    change_type: str  # ENACTED | REVOKES | AMENDS | PARTIALLY_REVOKES
    effective_date: str
    acting_document_id: str
    acting_regulation_number: str | None
    acting_title: str
    target_document_id: str | None = None
    target_regulation_number: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "change_type": self.change_type,
            "effective_date": self.effective_date,
            "acting_document_id": self.acting_document_id,
            "acting_regulation_number": self.acting_regulation_number,
            "acting_title": self.acting_title,
            "target_document_id": self.target_document_id,
            "target_regulation_number": self.target_regulation_number,
            "url": self.url,
        }


@dataclass(frozen=True, slots=True)
class RegulatoryDiffReport:
    kb_version: str
    kb_sha256: str
    baseline_date: str
    target_date: str
    enacted_regulations: tuple[RegulatoryChangeEvent, ...]
    legal_events: tuple[RegulatoryChangeEvent, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "kb_version": self.kb_version,
            "kb_sha256": self.kb_sha256,
            "baseline_date": self.baseline_date,
            "target_date": self.target_date,
            "enacted_count": len(self.enacted_regulations),
            "legal_events_count": len(self.legal_events),
            "enacted_regulations": [e.to_dict() for e in self.enacted_regulations],
            "legal_events": [e.to_dict() for e in self.legal_events],
        }


@dataclass(frozen=True, slots=True)
class SkuImpactDiff:
    row_number: int
    reference_id: str
    mode: BatchMode
    hs_code: str
    route: str
    currency: str
    declared_value: Decimal | None
    impact_level: str  # BREAKING | ADVERSE | FAVORABLE | REGULATORY_UPDATE | UNCHANGED | ERROR
    change_tags: tuple[str, ...]
    baseline_status: str
    target_status: str
    baseline_lartas_status: str | None
    target_lartas_status: str | None
    baseline_agreement: str | None
    target_agreement: str | None
    baseline_duty_percent: Decimal | None
    target_duty_percent: Decimal | None
    delta_duty_percent: Decimal | None
    baseline_import_taxes: Decimal | None
    target_import_taxes: Decimal | None
    delta_import_taxes: Decimal | None
    baseline_export_duty_rate: str | None
    target_export_duty_rate: str | None
    baseline_export_duty_usd: Decimal | None
    target_export_duty_usd: Decimal | None
    delta_export_duty_usd: Decimal | None
    net_cost_delta_usd: Decimal | None
    added_permits: tuple[str, ...]
    removed_permits: tuple[str, ...]
    newly_revoked_pmk: str | None
    impact_summary: str
    recommended_mitigation: str | None
    added_source_clauses: tuple[str, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "row_number": self.row_number,
            "reference_id": self.reference_id,
            "mode": self.mode,
            "hs_code": self.hs_code,
            "route": self.route,
            "currency": self.currency,
            "declared_value": _fmt_dec(self.declared_value),
            "impact_level": self.impact_level,
            "change_tags": list(self.change_tags),
            "baseline_status": self.baseline_status,
            "target_status": self.target_status,
            "baseline_lartas_status": self.baseline_lartas_status,
            "target_lartas_status": self.target_lartas_status,
            "baseline_agreement": self.baseline_agreement,
            "target_agreement": self.target_agreement,
            "baseline_duty_percent": _fmt_dec(self.baseline_duty_percent),
            "target_duty_percent": _fmt_dec(self.target_duty_percent),
            "delta_duty_percent": _fmt_dec(self.delta_duty_percent),
            "baseline_import_taxes": _fmt_dec(self.baseline_import_taxes),
            "target_import_taxes": _fmt_dec(self.target_import_taxes),
            "delta_import_taxes": _fmt_dec(self.delta_import_taxes),
            "baseline_export_duty_rate": self.baseline_export_duty_rate,
            "target_export_duty_rate": self.target_export_duty_rate,
            "baseline_export_duty_usd": _fmt_dec(self.baseline_export_duty_usd),
            "target_export_duty_usd": _fmt_dec(self.target_export_duty_usd),
            "delta_export_duty_usd": _fmt_dec(self.delta_export_duty_usd),
            "net_cost_delta_usd": _fmt_dec(self.net_cost_delta_usd),
            "added_permits": list(self.added_permits),
            "removed_permits": list(self.removed_permits),
            "newly_revoked_pmk": self.newly_revoked_pmk,
            "impact_summary": self.impact_summary,
            "recommended_mitigation": self.recommended_mitigation,
            "added_source_clauses": list(self.added_source_clauses),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class PortfolioImpactSummary:
    total_rows: int
    breaking_count: int
    adverse_count: int
    favorable_count: int
    regulatory_update_count: int
    unchanged_count: int
    error_count: int
    baseline_total_import_taxes: Decimal
    target_total_import_taxes: Decimal
    delta_total_import_taxes: Decimal
    baseline_total_export_duty_usd: Decimal
    target_total_export_duty_usd: Decimal
    delta_total_export_duty_usd: Decimal
    net_financial_impact_usd: Decimal
    enacted_regulations_count: int
    legal_events_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "total_rows": self.total_rows,
            "breaking_count": self.breaking_count,
            "adverse_count": self.adverse_count,
            "favorable_count": self.favorable_count,
            "regulatory_update_count": self.regulatory_update_count,
            "unchanged_count": self.unchanged_count,
            "error_count": self.error_count,
            "baseline_total_import_taxes": f"{self.baseline_total_import_taxes:.2f}",
            "target_total_import_taxes": f"{self.target_total_import_taxes:.2f}",
            "delta_total_import_taxes": f"{self.delta_total_import_taxes:.2f}",
            "baseline_total_export_duty_usd": f"{self.baseline_total_export_duty_usd:.2f}",
            "target_total_export_duty_usd": f"{self.target_total_export_duty_usd:.2f}",
            "delta_total_export_duty_usd": f"{self.delta_total_export_duty_usd:.2f}",
            "net_financial_impact_usd": f"{self.net_financial_impact_usd:.2f}",
            "enacted_regulations_count": self.enacted_regulations_count,
            "legal_events_count": self.legal_events_count,
        }


@dataclass(frozen=True, slots=True)
class PortfolioImpactResult:
    kb_version: str
    kb_sha256: str
    mode: BatchMode
    screening_depth: ScreeningDepth
    baseline_date: str
    target_date: str
    generated_at: str
    regulatory_diff: RegulatoryDiffReport
    summary: PortfolioImpactSummary
    rows: tuple[SkuImpactDiff, ...]
    citations: tuple[ProvenanceCitation, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kb_version": self.kb_version,
            "kb_sha256": self.kb_sha256,
            "mode": self.mode,
            "screening_depth": self.screening_depth,
            "baseline_date": self.baseline_date,
            "target_date": self.target_date,
            "generated_at": self.generated_at,
            "regulatory_diff": self.regulatory_diff.to_dict(),
            "summary": self.summary.to_dict(),
            "rows": [r.to_dict() for r in self.rows],
            "citations": [c.to_dict() for c in self.citations],
        }

    def to_csv_bytes(self) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf)
        if self.mode == "import":
            writer.writerow([
                "Row", "Reference", "HS Code", "Route", "Impact Level", "Change Tags",
                f"Lartas ({self.baseline_date})", f"Lartas ({self.target_date})",
                f"Scheme ({self.baseline_date})", f"Scheme ({self.target_date})",
                f"Duty % ({self.baseline_date})", f"Duty % ({self.target_date})", "Delta Duty %",
                f"Import Taxes ({self.baseline_date})", f"Import Taxes ({self.target_date})", "Delta Taxes",
                "Added Permits", "Revoked Tariff PMK", "Impact Summary", "Recommended Mitigation", "Error",
            ])
            for r in self.rows:
                d = r.to_dict()
                writer.writerow([
                    r.row_number, r.reference_id, r.hs_code, r.route, r.impact_level, "; ".join(r.change_tags),
                    r.baseline_lartas_status or "", r.target_lartas_status or "",
                    r.baseline_agreement or "", r.target_agreement or "",
                    d["baseline_duty_percent"] or "", d["target_duty_percent"] or "", d["delta_duty_percent"] or "",
                    d["baseline_import_taxes"] or "", d["target_import_taxes"] or "", d["delta_import_taxes"] or "",
                    "; ".join(r.added_permits), r.newly_revoked_pmk or "",
                    r.impact_summary, r.recommended_mitigation or "", r.error or "",
                ])
        else:
            writer.writerow([
                "Row", "Reference", "HS Code", "Route", "Impact Level", "Change Tags",
                f"Export Lartas ({self.baseline_date})", f"Export Lartas ({self.target_date})",
                f"Bea Keluar Rate ({self.baseline_date})", f"Bea Keluar Rate ({self.target_date})",
                f"Bea Keluar USD ({self.baseline_date})", f"Bea Keluar USD ({self.target_date})", "Delta Bea Keluar USD",
                "Added Permits", "Impact Summary", "Recommended Mitigation", "Error",
            ])
            for r in self.rows:
                d = r.to_dict()
                writer.writerow([
                    r.row_number, r.reference_id, r.hs_code, r.route, r.impact_level, "; ".join(r.change_tags),
                    r.baseline_lartas_status or "", r.target_lartas_status or "",
                    r.baseline_export_duty_rate or "", r.target_export_duty_rate or "",
                    d["baseline_export_duty_usd"] or "", d["target_export_duty_usd"] or "",
                    d["delta_export_duty_usd"] or "",
                    "; ".join(r.added_permits), r.impact_summary, r.recommended_mitigation or "", r.error or "",
                ])
        return buf.getvalue().encode("utf-8-sig")

    def to_xlsx_bytes(self) -> bytes:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()

        navy_fill = PatternFill("solid", fgColor="1E293B")
        sub_fill = PatternFill("solid", fgColor="E2E8F0")
        ok_fill = PatternFill("solid", fgColor="DCFCE7")
        warn_fill = PatternFill("solid", fgColor="FEF3C7")
        danger_fill = PatternFill("solid", fgColor="FEE2E2")
        info_fill = PatternFill("solid", fgColor="DBEAFE")
        muted_fill = PatternFill("solid", fgColor="F1F5F9")
        header_font = Font(bold=True, color="FFFFFF", size=10)
        bold_font = Font(bold=True, size=10)
        title_font = Font(bold=True, size=13, color="0F172A")
        thin = Side(style="thin", color="CBD5E1")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        # --- Sheet 1: Executive Impact Summary ---
        ws_sum = wb.active
        ws_sum.title = "Executive Impact Summary"
        ws_sum["A1"] = (
            f"TradeGraph Regulatory Impact Diff ({self.baseline_date} → {self.target_date}) — "
            f"{'Import (PIB)' if self.mode == 'import' else 'Export & Bea Keluar (PEB)'}"
        )
        ws_sum["A1"].font = title_font

        meta = [
            ("Knowledge Base Version", f"v{self.kb_version} (OWL 2 DL)"),
            ("KB SHA-256 Digest", self.kb_sha256),
            ("Comparison Window", f"{self.baseline_date} -> {self.target_date}"),
            ("Mode / Depth", f"{self.mode.upper()} ({self.screening_depth})"),
            ("Generated At (UTC)", self.generated_at),
            ("", ""),
            ("Newly Enacted Regulations in Window", self.summary.enacted_regulations_count),
            ("Legal Revocation / Amendment Events", self.summary.legal_events_count),
            ("Total SKUs Evaluated", self.summary.total_rows),
            ("BREAKING (Prohibition / PMK Revocation)", self.summary.breaking_count),
            ("ADVERSE (New Permits / Higher Duty)", self.summary.adverse_count),
            ("FAVORABLE (Duty Cut / Relaxed Rules)", self.summary.favorable_count),
            ("REGULATORY_UPDATE (Legal Basis Updated)", self.summary.regulatory_update_count),
            ("UNCHANGED", self.summary.unchanged_count),
            ("Input / Validation Errors", self.summary.error_count),
        ]
        if self.mode == "import":
            meta.extend([
                (f"Baseline Total Import Taxes ({self.baseline_date})", float(self.summary.baseline_total_import_taxes)),
                (f"Target Total Import Taxes ({self.target_date})", float(self.summary.target_total_import_taxes)),
                ("Net Import Tax Impact (+Cost / -Savings)", float(self.summary.delta_total_import_taxes)),
            ])
        else:
            meta.extend([
                (f"Baseline Total Bea Keluar USD ({self.baseline_date})", float(self.summary.baseline_total_export_duty_usd)),
                (f"Target Total Bea Keluar USD ({self.target_date})", float(self.summary.target_total_export_duty_usd)),
                ("Net Bea Keluar Impact USD (+Cost / -Savings)", float(self.summary.delta_total_export_duty_usd)),
            ])

        for idx, (lbl, val) in enumerate(meta, start=3):
            c1 = ws_sum.cell(row=idx, column=1, value=lbl)
            c2 = ws_sum.cell(row=idx, column=2, value=val)
            if lbl:
                c1.font = bold_font
                c1.fill = sub_fill
                c1.border = border
                c2.border = border
                if isinstance(val, float):
                    c2.number_format = "#,##0.00"
        ws_sum.column_dimensions["A"].width = 42
        ws_sum.column_dimensions["B"].width = 66

        # --- Sheet 2: Regulatory Changes ---
        ws_reg = wb.create_sheet("Regulatory Changes")
        reg_headers = ["Change Type", "Effective Date", "Acting Regulation", "Title", "Target Regulation", "Official URL"]
        for c_idx, h in enumerate(reg_headers, start=1):
            cell = ws_reg.cell(row=1, column=c_idx, value=h)
            cell.font = header_font
            cell.fill = navy_fill
            cell.border = border

        all_events = list(self.regulatory_diff.enacted_regulations) + list(self.regulatory_diff.legal_events)
        all_events.sort(key=lambda e: (e.effective_date, e.change_type, e.acting_document_id))
        for r_idx, ev in enumerate(all_events, start=2):
            ws_reg.cell(row=r_idx, column=1, value=ev.change_type).border = border
            ws_reg.cell(row=r_idx, column=2, value=ev.effective_date).border = border
            ws_reg.cell(row=r_idx, column=3, value=ev.acting_regulation_number or ev.acting_document_id).border = border
            ws_reg.cell(row=r_idx, column=4, value=ev.acting_title).border = border
            ws_reg.cell(row=r_idx, column=5, value=ev.target_regulation_number or ev.target_document_id or "").border = border
            url_c = ws_reg.cell(row=r_idx, column=6, value=ev.url or "")
            url_c.border = border
            if ev.url:
                url_c.hyperlink = ev.url
                url_c.font = Font(color="2563EB", underline="single")
            if ev.change_type == "REVOKES":
                ws_reg.cell(row=r_idx, column=1).fill = danger_fill
            elif ev.change_type in {"AMENDS", "PARTIALLY_REVOKES"}:
                ws_reg.cell(row=r_idx, column=1).fill = warn_fill
            else:
                ws_reg.cell(row=r_idx, column=1).fill = info_fill
        ws_reg.freeze_panes = "A2"
        for col_i, w in enumerate((20, 14, 28, 52, 26, 52), start=1):
            ws_reg.column_dimensions[get_column_letter(col_i)].width = w

        # --- Sheet 3: SKU Impact Diff ---
        ws_sku = wb.create_sheet("SKU Impact Diff")
        if self.mode == "import":
            headers = [
                "Row", "Reference", "HS Code", "Route", "Impact Level", "Change Tags",
                f"Lartas ({self.baseline_date})", f"Lartas ({self.target_date})",
                f"Duty % ({self.baseline_date})", f"Duty % ({self.target_date})", "Delta Duty %",
                f"Taxes ({self.baseline_date})", f"Taxes ({self.target_date})", "Delta Taxes",
                "Added Permits", "Revoked PMK", "Impact Summary", "Mitigation",
            ]
            num_cols = {9, 10, 11, 12, 13, 14}
        else:
            headers = [
                "Row", "Reference", "HS Code", "Route", "Impact Level", "Change Tags",
                f"Lartas ({self.baseline_date})", f"Lartas ({self.target_date})",
                f"BK Rate ({self.baseline_date})", f"BK Rate ({self.target_date})",
                f"BK USD ({self.baseline_date})", f"BK USD ({self.target_date})", "Delta BK USD",
                "Added Permits", "Impact Summary", "Mitigation",
            ]
            num_cols = {11, 12, 13}

        for c_idx, h in enumerate(headers, start=1):
            cell = ws_sku.cell(row=1, column=c_idx, value=h)
            cell.font = header_font
            cell.fill = navy_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = border

        level_fills = {
            "BREAKING": danger_fill,
            "ADVERSE": warn_fill,
            "FAVORABLE": ok_fill,
            "REGULATORY_UPDATE": info_fill,
            "UNCHANGED": muted_fill,
            "ERROR": danger_fill,
        }

        for r_idx, r in enumerate(self.rows, start=2):
            if self.mode == "import":
                vals: list[object] = [
                    r.row_number, r.reference_id, r.hs_code, r.route, r.impact_level, "; ".join(r.change_tags),
                    r.baseline_lartas_status or "", r.target_lartas_status or "",
                    float(r.baseline_duty_percent) if r.baseline_duty_percent is not None else None,
                    float(r.target_duty_percent) if r.target_duty_percent is not None else None,
                    float(r.delta_duty_percent) if r.delta_duty_percent is not None else None,
                    float(r.baseline_import_taxes) if r.baseline_import_taxes is not None else None,
                    float(r.target_import_taxes) if r.target_import_taxes is not None else None,
                    float(r.delta_import_taxes) if r.delta_import_taxes is not None else None,
                    "; ".join(r.added_permits), r.newly_revoked_pmk or "",
                    r.error or r.impact_summary, r.recommended_mitigation or "",
                ]
            else:
                vals = [
                    r.row_number, r.reference_id, r.hs_code, r.route, r.impact_level, "; ".join(r.change_tags),
                    r.baseline_lartas_status or "", r.target_lartas_status or "",
                    r.baseline_export_duty_rate or "", r.target_export_duty_rate or "",
                    float(r.baseline_export_duty_usd) if r.baseline_export_duty_usd is not None else None,
                    float(r.target_export_duty_usd) if r.target_export_duty_usd is not None else None,
                    float(r.delta_export_duty_usd) if r.delta_export_duty_usd is not None else None,
                    "; ".join(r.added_permits), r.error or r.impact_summary, r.recommended_mitigation or "",
                ]
            for c_idx, val in enumerate(vals, start=1):
                cell = ws_sku.cell(row=r_idx, column=c_idx, value=val)
                cell.border = border
                if c_idx == 5 and r.impact_level in level_fills:
                    cell.fill = level_fills[r.impact_level]
                    cell.font = bold_font
                if c_idx in num_cols and isinstance(val, (int, float)):
                    cell.number_format = "#,##0.00"

        ws_sku.freeze_panes = "A2"
        ws_sku.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(self.rows) + 1, 2)}"
        for col_idx in range(1, len(headers) + 1):
            col_l = get_column_letter(col_idx)
            max_len = max(
                len(str(ws_sku.cell(row=row_i, column=col_idx).value or ""))
                for row_i in range(1, len(self.rows) + 2)
            )
            ws_sku.column_dimensions[col_l].width = min(max(max_len + 3, 12), 48)

        # --- Sheet 4: Legal Citations ---
        ws_cit = wb.create_sheet("Legal Citations")
        for col_idx, h in enumerate(("Regulation Number", "Document Title", "Clause / Locator", "Effective From", "Official Source URL"), start=1):
            cell = ws_cit.cell(row=1, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = navy_fill
            cell.border = border
        for idx, c in enumerate(self.citations, start=2):
            ws_cit.cell(row=idx, column=1, value=c.regulation_number or c.document_id or c.clause_id).border = border
            ws_cit.cell(row=idx, column=2, value=c.document_title or "").border = border
            ws_cit.cell(row=idx, column=3, value=c.clause_locator or c.clause_id).border = border
            ws_cit.cell(row=idx, column=4, value=c.effective_from or "").border = border
            u_cell = ws_cit.cell(row=idx, column=5, value=c.url or "")
            u_cell.border = border
            if c.url:
                u_cell.hyperlink = c.url
                u_cell.font = Font(color="2563EB", underline="single")
        ws_cit.freeze_panes = "A2"
        for col_idx, w in enumerate((26, 48, 48, 16, 56), start=1):
            ws_cit.column_dimensions[get_column_letter(col_idx)].width = w

        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()


class RegulatoryImpactAnalyzer:
    """KB-driven temporal what-if regulatory diff and portfolio impact analyzer."""

    def __init__(self, optimizer: MultiFtaOptimizer, screener: BatchScreener | None = None):
        self._opt = optimizer
        self._owl = optimizer._owl
        self._screener = screener or BatchScreener(optimizer)

    def _doc_url(self, doc_iri: URIRef) -> str | None:
        urls = sorted(str(u) for u in self._owl.objects(doc_iri, RDFS.seeAlso))
        return next((u for u in urls if "download" in u or u.endswith(".pdf")), urls[0] if urls else None)

    def diff_regulations(self, baseline_date: str, target_date: str) -> RegulatoryDiffReport:
        """Return every primary regulation enacted and every revocation/amendment triggered in (baseline_date, target_date]."""
        b_date, t_date = _validate_date_window(baseline_date, target_date)

        enacted: list[RegulatoryChangeEvent] = []
        seen_docs: set[str] = set()
        for doc_iri in sorted(self._owl.subjects(RDF.type, TG.SourceDocument), key=str):
            if not isinstance(doc_iri, URIRef):
                continue
            eff = self._owl.value(doc_iri, TG.effectiveFromDate)
            if eff is None:
                continue
            eff_str = str(eff)
            if not (b_date < eff_str <= t_date):
                continue
            doc_id = str(doc_iri).split("#")[-1]
            if doc_id in seen_docs:
                continue
            seen_docs.add(doc_id)
            reg_num = self._owl.value(doc_iri, TG.regulationNumber)
            title = self._owl.value(doc_iri, RDFS.label)
            enacted.append(RegulatoryChangeEvent(
                change_type="ENACTED",
                effective_date=eff_str,
                acting_document_id=doc_id,
                acting_regulation_number=str(reg_num) if reg_num is not None else None,
                acting_title=str(title) if title is not None else doc_id,
                url=self._doc_url(doc_iri),
            ))
        enacted.sort(key=lambda e: (e.effective_date, e.acting_regulation_number or e.acting_document_id))

        events: list[RegulatoryChangeEvent] = []
        seen_ev: set[tuple[str, str, str, str]] = set()
        for ev in sorted(self._owl.subjects(RDF.type, TG.LegalRelationEvent), key=str):
            eff = self._owl.value(ev, TG.relationEffectiveDate)
            if eff is None:
                continue
            eff_str = str(eff)
            if not (b_date < eff_str <= t_date):
                continue
            src = self._owl.value(ev, TG.relationActingDocument) or self._owl.value(ev, TG.relationSourceDocument)
            tgt = self._owl.value(ev, TG.relationTargetDocument)
            kind_iri = self._owl.value(ev, TG.hasLegalRelationKind)
            kind_local = str(kind_iri).split("#")[-1] if kind_iri is not None else "relRelated"
            c_type = _LEGAL_KIND_LABELS.get(kind_local, "RELATED")
            src_id = str(src).split("#")[-1] if isinstance(src, URIRef) else ""
            tgt_id = str(tgt).split("#")[-1] if isinstance(tgt, URIRef) else None
            key = (c_type, eff_str, src_id, tgt_id or "")
            if key in seen_ev:
                continue
            seen_ev.add(key)
            events.append(RegulatoryChangeEvent(
                change_type=c_type,
                effective_date=eff_str,
                acting_document_id=src_id,
                acting_regulation_number=(
                    str(self._owl.value(src, TG.regulationNumber))
                    if isinstance(src, URIRef) and self._owl.value(src, TG.regulationNumber) is not None else None
                ),
                acting_title=str(self._owl.value(src, RDFS.label) or src_id) if isinstance(src, URIRef) else src_id,
                target_document_id=tgt_id,
                target_regulation_number=(
                    str(self._owl.value(tgt, TG.regulationNumber))
                    if isinstance(tgt, URIRef) and self._owl.value(tgt, TG.regulationNumber) is not None else None
                ),
                url=self._doc_url(src) if isinstance(src, URIRef) else None,
            ))

        for src, tgt in sorted(self._owl.subject_objects(TG.partiallyRevokesDocument), key=lambda pair: (str(pair[0]), str(pair[1]))):
            if not isinstance(src, URIRef) or not isinstance(tgt, URIRef):
                continue
            eff = self._owl.value(src, TG.effectiveFromDate)
            if eff is None:
                continue
            eff_str = str(eff)
            if not (b_date < eff_str <= t_date):
                continue
            src_id = str(src).split("#")[-1]
            tgt_id = str(tgt).split("#")[-1]
            key = ("PARTIALLY_REVOKES", eff_str, src_id, tgt_id)
            if key in seen_ev:
                continue
            seen_ev.add(key)
            events.append(RegulatoryChangeEvent(
                change_type="PARTIALLY_REVOKES",
                effective_date=eff_str,
                acting_document_id=src_id,
                acting_regulation_number=(
                    str(self._owl.value(src, TG.regulationNumber))
                    if self._owl.value(src, TG.regulationNumber) is not None else None
                ),
                acting_title=str(self._owl.value(src, RDFS.label) or src_id),
                target_document_id=tgt_id,
                target_regulation_number=(
                    str(self._owl.value(tgt, TG.regulationNumber))
                    if self._owl.value(tgt, TG.regulationNumber) is not None else None
                ),
                url=self._doc_url(src),
            ))
        events.sort(key=lambda e: (e.effective_date, e.change_type, e.acting_document_id))

        return RegulatoryDiffReport(
            kb_version=self._opt.kb.snapshot.version,
            kb_sha256=self._opt.kb.snapshot.sha256,
            baseline_date=b_date,
            target_date=t_date,
            enacted_regulations=tuple(enacted),
            legal_events=tuple(events),
        )

    def _compare_pair(
        self,
        b_row: BatchRowOutcome,
        t_row: BatchRowOutcome,
        *,
        baseline_date: str,
        target_date: str,
    ) -> SkuImpactDiff:
        if b_row.overall_status == "ERROR" or t_row.overall_status == "ERROR":
            err_msg = t_row.error or b_row.error or "Validation error"
            return SkuImpactDiff(
                row_number=b_row.row_number,
                reference_id=b_row.reference_id,
                mode=b_row.mode,
                hs_code=b_row.hs_code or t_row.hs_code,
                route=b_row.route or t_row.route,
                currency=b_row.currency,
                declared_value=b_row.declared_value,
                impact_level="ERROR",
                change_tags=("ERROR",),
                baseline_status=b_row.overall_status,
                target_status=t_row.overall_status,
                baseline_lartas_status=b_row.lartas_status,
                target_lartas_status=t_row.lartas_status,
                baseline_agreement=b_row.recommended_agreement,
                target_agreement=t_row.recommended_agreement,
                baseline_duty_percent=b_row.applied_duty_percent,
                target_duty_percent=t_row.applied_duty_percent,
                delta_duty_percent=None,
                baseline_import_taxes=b_row.total_import_taxes,
                target_import_taxes=t_row.total_import_taxes,
                delta_import_taxes=None,
                baseline_export_duty_rate=b_row.export_duty_rate_display,
                target_export_duty_rate=t_row.export_duty_rate_display,
                baseline_export_duty_usd=b_row.export_duty_usd,
                target_export_duty_usd=t_row.export_duty_usd,
                delta_export_duty_usd=None,
                net_cost_delta_usd=None,
                added_permits=(),
                removed_permits=(),
                newly_revoked_pmk=None,
                impact_summary=f"Error: {err_msg}",
                recommended_mitigation="Periksa kode HS, negara, dan format nilai pada baris input.",
                added_source_clauses=(),
                error=err_msg,
            )

        tags: list[str] = []
        notes: list[str] = []
        mitigations: list[str] = []

        b_proh = b_row.overall_status == "PROHIBITED"
        t_proh = t_row.overall_status == "PROHIBITED"

        # 1. Prohibition transitions
        if not b_proh and t_proh:
            tags.append("NEW_PROHIBITION")
            notes.append(f"Menjadi DILARANG ({t_row.lartas_status}) per {target_date}.")
            mitigations.append("Hentikan pengiriman/pengajuan PIB-PEB atau tinjau ulang klasifikasi HS / pengecualian regulasi.")
        elif b_proh and not t_proh:
            tags.append("PROHIBITION_LIFTED")
            notes.append(f"Larangan dicabut ({b_row.lartas_status} -> {t_row.lartas_status}).")

        # 2. Permit & Lartas transitions
        b_permits = set(b_row.missing_permits)
        t_permits = set(t_row.missing_permits)
        added_permits = tuple(sorted(t_permits - b_permits))
        removed_permits = tuple(sorted(b_permits - t_permits))
        if added_permits and not t_proh:
            tags.append("NEW_LARTAS_PERMITS")
            notes.append(f"Tambahan perizinan Lartas wajib: {', '.join(added_permits)}.")
            mitigations.append(f"Ajukan perizinan {', '.join(added_permits)} sebelum tanggal berlaku {target_date}.")
        if removed_permits and not b_proh:
            tags.append("LARTAS_RELAXED")
            notes.append(f"Kewajiban perizinan berkurang: {', '.join(removed_permits)}.")

        # 3. Tariff PMK revocation cliff
        newly_revoked_pmk: str | None = None
        if not b_row.tariff_revoked_on and t_row.tariff_revoked_on:
            tags.append("TARIFF_PMK_REVOKED")
            sup = t_row.tariff_superseding_doc or "PMK baru"
            newly_revoked_pmk = f"{sup} (eff. {t_row.tariff_revoked_on})"
            notes.append(
                f"PMK tarif preferensi {(t_row.recommended_agreement or '').upper()} dicabut per {t_row.tariff_revoked_on} oleh {sup}."
            )
            mitigations.append(
                f"Verifikasi pos tarif pengganti pada {sup} atau evaluasi perpindahan ke skema FTA alternatif (mis. RCEP)."
            )

        # 4. Financial deltas (computed ONLY when both baseline and target are non-prohibited)
        delta_duty: Decimal | None = None
        delta_taxes: Decimal | None = None
        delta_bk: Decimal | None = None
        net_delta: Decimal | None = None

        if b_row.mode == "import":
            if not b_proh and not t_proh:
                if b_row.applied_duty_percent is not None and t_row.applied_duty_percent is not None:
                    delta_duty = (t_row.applied_duty_percent - b_row.applied_duty_percent).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    if delta_duty > Decimal("0"):
                        tags.append("DUTY_INCREASED")
                        notes.append(f"Tarif BM naik {b_row.applied_duty_percent:.2f}% -> {t_row.applied_duty_percent:.2f}%.")
                    elif delta_duty < Decimal("0"):
                        tags.append("DUTY_DECREASED")
                        notes.append(f"Tarif BM turun {b_row.applied_duty_percent:.2f}% -> {t_row.applied_duty_percent:.2f}%.")
                if b_row.total_import_taxes is not None and t_row.total_import_taxes is not None:
                    delta_taxes = (t_row.total_import_taxes - b_row.total_import_taxes).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    net_delta = delta_taxes
                    if delta_taxes < Decimal("0"):
                        notes.append(f"Penghematan pajak impor {b_row.currency} {abs(delta_taxes):,.2f}.")
                    elif delta_taxes > Decimal("0"):
                        notes.append(f"Kenaikan beban pajak impor +{b_row.currency} {delta_taxes:,.2f}.")
                        mitigations.append("Pertimbangkan percepatan pengapalan sebelum kenaikan tarif atau skema FTA alternatif.")
            if b_row.recommended_agreement != t_row.recommended_agreement and not t_proh and not b_proh:
                tags.append("SCHEME_CHANGED")
                notes.append(f"Skema optimal berubah: {b_row.recommended_agreement} -> {t_row.recommended_agreement}.")
        else:
            if not b_proh and not t_proh:
                b_bk = b_row.export_duty_usd
                t_bk = t_row.export_duty_usd
                if b_row.export_duty_status == "NOT_SUBJECT" and t_row.export_duty_status == "APPLICABLE":
                    tags.append("NEW_EXPORT_DUTY")
                    notes.append(f"Objek Bea Keluar baru ({t_row.export_duty_rate_display}).")
                    mitigations.append("Hitung ulang margin ekspor & jadwalkan pelunasan Bea Keluar sebelum PEB.")
                if b_bk is not None and t_bk is not None:
                    delta_bk = (t_bk - b_bk).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    net_delta = delta_bk
                    if delta_bk > Decimal("0"):
                        if "NEW_EXPORT_DUTY" not in tags:
                            tags.append("EXPORT_DUTY_INCREASED")
                        notes.append(
                            f"Beban Bea Keluar naik USD {b_bk:,.2f} -> USD {t_bk:,.2f} (+USD {delta_bk:,.2f})."
                        )
                    elif delta_bk < Decimal("0"):
                        tags.append("EXPORT_DUTY_DECREASED")
                        notes.append(
                            f"Bea Keluar turun USD {b_bk:,.2f} -> USD {t_bk:,.2f} (hemat USD {abs(delta_bk):,.2f})."
                        )
                        mitigations.append("Manfaatkan penurunan tarif Bea Keluar pada jadwal pengapalan mulai tanggal efektif.")

        # 5. Legal basis update check
        added_clauses = tuple(sorted(set(t_row.source_clauses) - set(b_row.source_clauses)))
        if added_clauses and not tags:
            tags.append("LEGAL_BASIS_UPDATED")
            notes.append(f"Tarif/status tetap, namun dasar hukum diperbarui ({len(added_clauses)} klausul baru).")

        # Severity classification (ordered by highest risk)
        if "NEW_PROHIBITION" in tags or "TARIFF_PMK_REVOKED" in tags:
            level = "BREAKING"
        elif any(t in tags for t in ("NEW_LARTAS_PERMITS", "DUTY_INCREASED", "NEW_EXPORT_DUTY", "EXPORT_DUTY_INCREASED")):
            level = "ADVERSE"
        elif any(t in tags for t in ("PROHIBITION_LIFTED", "LARTAS_RELAXED", "DUTY_DECREASED", "EXPORT_DUTY_DECREASED")):
            level = "FAVORABLE"
        elif tags:
            level = "REGULATORY_UPDATE"
        else:
            level = "UNCHANGED"

        summary_text = " ".join(notes) if notes else f"Tidak ada perubahan tarif atau kepatuhan ({baseline_date} -> {target_date})."
        mitigation_text = " ".join(dict.fromkeys(mitigations)) if mitigations else None

        return SkuImpactDiff(
            row_number=b_row.row_number,
            reference_id=b_row.reference_id,
            mode=b_row.mode,
            hs_code=b_row.hs_code,
            route=b_row.route,
            currency=b_row.currency,
            declared_value=b_row.declared_value,
            impact_level=level,
            change_tags=tuple(tags),
            baseline_status=b_row.overall_status,
            target_status=t_row.overall_status,
            baseline_lartas_status=b_row.lartas_status,
            target_lartas_status=t_row.lartas_status,
            baseline_agreement=b_row.recommended_agreement,
            target_agreement=t_row.recommended_agreement,
            baseline_duty_percent=b_row.applied_duty_percent,
            target_duty_percent=t_row.applied_duty_percent,
            delta_duty_percent=delta_duty,
            baseline_import_taxes=b_row.total_import_taxes,
            target_import_taxes=t_row.total_import_taxes,
            delta_import_taxes=delta_taxes,
            baseline_export_duty_rate=b_row.export_duty_rate_display,
            target_export_duty_rate=t_row.export_duty_rate_display,
            baseline_export_duty_usd=b_row.export_duty_usd,
            target_export_duty_usd=t_row.export_duty_usd,
            delta_export_duty_usd=delta_bk,
            net_cost_delta_usd=net_delta,
            added_permits=added_permits,
            removed_permits=removed_permits,
            newly_revoked_pmk=newly_revoked_pmk,
            impact_summary=summary_text,
            recommended_mitigation=mitigation_text,
            added_source_clauses=added_clauses,
        )

    def analyze_portfolio(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        mode: BatchMode,
        baseline_date: str,
        target_date: str,
        screening_depth: ScreeningDepth = "fast_lartas",
        include_explain: bool = True,
    ) -> PortfolioImpactResult:
        """Compare compliance & financial impact of a SKU portfolio between baseline_date and target_date."""
        b_date, t_date = _validate_date_window(baseline_date, target_date)
        reg_diff = self.diff_regulations(b_date, t_date)

        base_rows = [{**normalize_batch_row(r), "as_of_date": b_date} for r in rows]
        targ_rows = [{**normalize_batch_row(r), "as_of_date": t_date} for r in rows]

        base_res = self._screener.screen(base_rows, mode=mode, screening_depth=screening_depth, include_explain=include_explain)
        targ_res = self._screener.screen(targ_rows, mode=mode, screening_depth=screening_depth, include_explain=include_explain)

        diffs: list[SkuImpactDiff] = []
        all_clauses: set[str] = set()
        for b_row, t_row in zip(base_res.rows, targ_res.rows):
            diff = self._compare_pair(b_row, t_row, baseline_date=b_date, target_date=t_date)
            diffs.append(diff)
            all_clauses.update(b_row.source_clauses)
            all_clauses.update(t_row.source_clauses)

        # Aggregate financial impact strictly over comparable (non-prohibited) pairs
        b_imp_tot = sum((d.baseline_import_taxes for d in diffs if d.delta_import_taxes is not None and d.baseline_import_taxes is not None), Decimal("0.00"))
        t_imp_tot = sum((d.target_import_taxes for d in diffs if d.delta_import_taxes is not None and d.target_import_taxes is not None), Decimal("0.00"))
        d_imp_tot = (t_imp_tot - b_imp_tot).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        b_bk_tot = sum((d.baseline_export_duty_usd for d in diffs if d.delta_export_duty_usd is not None and d.baseline_export_duty_usd is not None), Decimal("0.00"))
        t_bk_tot = sum((d.target_export_duty_usd for d in diffs if d.delta_export_duty_usd is not None and d.target_export_duty_usd is not None), Decimal("0.00"))
        d_bk_tot = (t_bk_tot - b_bk_tot).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        net_fin = d_imp_tot if mode == "import" else d_bk_tot

        summary = PortfolioImpactSummary(
            total_rows=len(diffs),
            breaking_count=sum(1 for d in diffs if d.impact_level == "BREAKING"),
            adverse_count=sum(1 for d in diffs if d.impact_level == "ADVERSE"),
            favorable_count=sum(1 for d in diffs if d.impact_level == "FAVORABLE"),
            regulatory_update_count=sum(1 for d in diffs if d.impact_level == "REGULATORY_UPDATE"),
            unchanged_count=sum(1 for d in diffs if d.impact_level == "UNCHANGED"),
            error_count=sum(1 for d in diffs if d.impact_level == "ERROR"),
            baseline_total_import_taxes=b_imp_tot,
            target_total_import_taxes=t_imp_tot,
            delta_total_import_taxes=d_imp_tot,
            baseline_total_export_duty_usd=b_bk_tot,
            target_total_export_duty_usd=t_bk_tot,
            delta_total_export_duty_usd=d_bk_tot,
            net_financial_impact_usd=net_fin,
            enacted_regulations_count=len(reg_diff.enacted_regulations),
            legal_events_count=len(reg_diff.legal_events),
        )
        citations = self._opt.resolve_citations(all_clauses) if include_explain and all_clauses else ()
        return PortfolioImpactResult(
            kb_version=self._opt.kb.snapshot.version,
            kb_sha256=self._opt.kb.snapshot.sha256,
            mode=mode,
            screening_depth=screening_depth,
            baseline_date=b_date,
            target_date=t_date,
            generated_at=datetime.now(UTC).isoformat(),
            regulatory_diff=reg_diff,
            summary=summary,
            rows=tuple(diffs),
            citations=citations,
        )
