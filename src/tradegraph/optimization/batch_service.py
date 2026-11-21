"""Multi-SKU Import (PIB) & Export (PEB) Batch Screening and Audit-Grade Excel/CSV Report Generator."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import io
import json
from pathlib import Path
from typing import Literal

from tradegraph.kb.loader import TG
from tradegraph.optimization.export_service import ExportComplianceAdvisor, ExportSpec
from tradegraph.optimization.service import (
    MultiFtaOptimizer,
    ProvenanceCitation,
    ShipmentSpec,
    _to_decimal,
)


BatchMode = Literal["import", "export"]
ScreeningDepth = Literal["full", "fast_lartas"]


def _parse_bool(val: object) -> bool | None:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return val
    text = str(val).strip().lower()
    if text in {"true", "yes", "y", "1", "ya"}:
        return True
    if text in {"false", "no", "n", "0", "tidak"}:
        return False
    return None


def _parse_str_list(val: object) -> list[str]:
    if val is None or val == "":
        return []
    if isinstance(val, (list, tuple)):
        return [str(x).strip() for x in val if str(x).strip()]
    text = str(val).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:
            pass
    sep = ";" if ";" in text else ","
    return [part.strip() for part in text.split(sep) if part.strip()]


def _parse_materials_cell(val: object) -> list[dict[str, object]]:
    if val is None or val == "":
        return []
    if isinstance(val, list):
        return [dict(item) for item in val if isinstance(item, Mapping)]
    text = str(val).strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [dict(item) for item in parsed if isinstance(item, Mapping)]
    # Compact shorthand: "8507.90.92:CN:false;7208.54.10:JP:true"
    out: list[dict[str, object]] = []
    for chunk in text.split(";"):
        part = chunk.strip()
        if not part:
            continue
        bits = [b.strip() for b in part.split(":")]
        mat: dict[str, object] = {"hs_code": bits[0], "non_originating": True}
        if len(bits) >= 2 and bits[1]:
            mat["origin_country"] = bits[1]
        if len(bits) >= 3 and bits[2]:
            mat["origin_proof_verified"] = _parse_bool(bits[2])
        out.append(mat)
    return out


def normalize_batch_row(raw: Mapping[str, object]) -> dict[str, object]:
    """Normalize spreadsheet/CSV/JSON keys & cell values into canonical TradeGraph spec dictionaries."""
    clean: dict[str, object] = {}
    for k, v in raw.items():
        if k is None:
            continue
        key = str(k).strip().lower().replace(" ", "_").replace("-", "_")
        if not key:
            continue
        if isinstance(v, str):
            v = v.strip()
        clean[key] = None if v == "" else v
    return clean


def load_batch_rows_from_csv(data: bytes | str) -> list[dict[str, object]]:
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, object]] = []
    for raw in reader:
        norm = normalize_batch_row(raw)
        if any(v is not None for v in norm.values()):
            rows.append(norm)
    return rows


def load_batch_rows_from_xlsx(data: bytes) -> list[dict[str, object]]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.active
    raw_rows = list(ws.iter_rows(values_only=True))
    if not raw_rows:
        return []
    headers = [str(h).strip() if h is not None else "" for h in raw_rows[0]]
    rows: list[dict[str, object]] = []
    for values in raw_rows[1:]:
        if all(v is None or str(v).strip() == "" for v in values):
            continue
        mapped = {headers[i]: values[i] for i in range(min(len(headers), len(values))) if headers[i]}
        rows.append(normalize_batch_row(mapped))
    return rows


def load_batch_rows_from_file(path: Path) -> list[dict[str, object]]:
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    if suffix == ".csv":
        return load_batch_rows_from_csv(raw)
    if suffix in {".xlsx", ".xlsm"}:
        return load_batch_rows_from_xlsx(raw)
    if suffix == ".json":
        parsed = json.loads(raw.decode("utf-8-sig"))
        items = parsed.get("items") if isinstance(parsed, Mapping) else parsed
        if not isinstance(items, list):
            raise ValueError("JSON batch file must contain a list of shipment objects or an {'items': [...]} object.")
        return [normalize_batch_row(item) for item in items if isinstance(item, Mapping)]
    raise ValueError(f"Unsupported batch file extension: {suffix!r} (expected .json, .csv, or .xlsx)")


@dataclass(frozen=True, slots=True)
class BatchRowOutcome:
    row_number: int
    reference_id: str
    mode: BatchMode
    hs_code: str
    route: str
    as_of_date: str
    overall_status: str  # READY | ACTION_REQUIRED | BLOCKED | PROHIBITED | ERROR
    lartas_status: str | None
    recommended_agreement: str | None
    currency: str
    declared_value: Decimal | None
    base_duty_percent: Decimal | None
    applied_duty_percent: Decimal | None
    total_import_taxes: Decimal | None
    net_tax_savings: Decimal | None
    export_duty_status: str | None
    export_duty_rate_display: str | None
    export_duty_usd: Decimal | None
    missing_permits: tuple[str, ...]
    back_to_back_status: str | None
    cumulation_tip: str | None
    next_action: str | None
    source_clauses: tuple[str, ...]
    tariff_revoked_on: str | None = None
    tariff_superseding_doc: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        def _fmt(val: Decimal | None) -> str | None:
            return f"{val:.2f}" if val is not None else None

        return {
            "row_number": self.row_number,
            "reference_id": self.reference_id,
            "mode": self.mode,
            "hs_code": self.hs_code,
            "route": self.route,
            "as_of_date": self.as_of_date,
            "overall_status": self.overall_status,
            "lartas_status": self.lartas_status,
            "recommended_agreement": self.recommended_agreement,
            "currency": self.currency,
            "declared_value": _fmt(self.declared_value),
            "base_duty_percent": _fmt(self.base_duty_percent),
            "applied_duty_percent": _fmt(self.applied_duty_percent),
            "total_import_taxes": _fmt(self.total_import_taxes),
            "net_tax_savings": _fmt(self.net_tax_savings),
            "export_duty_status": self.export_duty_status,
            "export_duty_rate_display": self.export_duty_rate_display,
            "export_duty_usd": _fmt(self.export_duty_usd),
            "missing_permits": list(self.missing_permits),
            "back_to_back_status": self.back_to_back_status,
            "cumulation_tip": self.cumulation_tip,
            "next_action": self.next_action,
            "source_clauses": list(self.source_clauses),
            "tariff_revoked_on": self.tariff_revoked_on,
            "tariff_superseding_doc": self.tariff_superseding_doc,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class BatchScreeningSummary:
    total_rows: int
    ready_count: int
    action_required_count: int
    blocked_count: int
    prohibited_count: int
    error_count: int
    total_declared_value: Decimal
    total_import_taxes: Decimal
    total_net_tax_savings: Decimal
    total_export_duty_usd: Decimal

    def to_dict(self) -> dict[str, object]:
        return {
            "total_rows": self.total_rows,
            "ready_count": self.ready_count,
            "action_required_count": self.action_required_count,
            "blocked_count": self.blocked_count,
            "prohibited_count": self.prohibited_count,
            "error_count": self.error_count,
            "total_declared_value": f"{self.total_declared_value:.2f}",
            "total_import_taxes": f"{self.total_import_taxes:.2f}",
            "total_net_tax_savings": f"{self.total_net_tax_savings:.2f}",
            "total_export_duty_usd": f"{self.total_export_duty_usd:.2f}",
        }


@dataclass(frozen=True, slots=True)
class BatchScreeningResult:
    kb_version: str
    kb_sha256: str
    mode: BatchMode
    screening_depth: ScreeningDepth
    generated_at: str
    summary: BatchScreeningSummary
    rows: tuple[BatchRowOutcome, ...]
    citations: tuple[ProvenanceCitation, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "kb_version": self.kb_version,
            "kb_sha256": self.kb_sha256,
            "mode": self.mode,
            "screening_depth": self.screening_depth,
            "generated_at": self.generated_at,
            "summary": self.summary.to_dict(),
            "rows": [r.to_dict() for r in self.rows],
            "citations": [c.to_dict() for c in self.citations],
        }

    def to_csv_bytes(self) -> bytes:
        buf = io.StringIO()
        if self.mode == "import":
            headers = [
                "Row", "Reference", "HS Code", "Route", "Assessment Date", "Overall Status",
                "Lartas Status", "Recommended Scheme", "Currency", "CIF Value",
                "MFN Duty (%)", "Applied Duty (%)", "Total Import Taxes", "Net Tax Savings",
                "Missing Permits", "Next Action", "Error",
            ]
            writer = csv.writer(buf)
            writer.writerow(headers)
            for r in self.rows:
                d = r.to_dict()
                writer.writerow([
                    r.row_number, r.reference_id, r.hs_code, r.route, r.as_of_date, r.overall_status,
                    r.lartas_status or "", r.recommended_agreement or "", r.currency, d["declared_value"] or "",
                    d["base_duty_percent"] or "", d["applied_duty_percent"] or "",
                    d["total_import_taxes"] or "", d["net_tax_savings"] or "",
                    "; ".join(r.missing_permits), r.next_action or "", r.error or "",
                ])
        else:
            headers = [
                "Row", "Reference", "HS Code", "Route", "Export Date", "Overall Status",
                "Export Lartas / PEB", "Recommended FTA", "Currency", "FOB Value",
                "Bea Keluar Status", "Bea Keluar Rate", "Estimated Bea Keluar (USD)",
                "Back-to-Back Status", "Missing Permits / Docs", "Cumulation Tip", "Next Action", "Error",
            ]
            writer = csv.writer(buf)
            writer.writerow(headers)
            for r in self.rows:
                d = r.to_dict()
                writer.writerow([
                    r.row_number, r.reference_id, r.hs_code, r.route, r.as_of_date, r.overall_status,
                    r.lartas_status or "", r.recommended_agreement or "", r.currency, d["declared_value"] or "",
                    r.export_duty_status or "", r.export_duty_rate_display or "", d["export_duty_usd"] or "",
                    r.back_to_back_status or "", "; ".join(r.missing_permits),
                    r.cumulation_tip or "", r.next_action or "", r.error or "",
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
        header_font = Font(bold=True, color="FFFFFF", size=10)
        bold_font = Font(bold=True, size=10)
        title_font = Font(bold=True, size=13, color="0F172A")
        thin_side = Side(style="thin", color="CBD5E1")
        cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

        # --- Sheet 1: Executive Summary ---
        ws_sum = wb.active
        ws_sum.title = "Executive Summary"
        ws_sum["A1"] = f"TradeGraph Audit Report — {'Import (PIB)' if self.mode == 'import' else 'Export & Bea Keluar (PEB)'} Batch Screening"
        ws_sum["A1"].font = title_font

        meta_rows = [
            ("Knowledge Base Version", f"v{self.kb_version} (OWL 2 DL)"),
            ("KB SHA-256 Digest", self.kb_sha256),
            ("Screening Mode", f"{self.mode.upper()} ({self.screening_depth})"),
            ("Generated At (UTC)", self.generated_at),
            ("", ""),
            ("Total Shipments Screened", self.summary.total_rows),
            ("Ready / Clear", self.summary.ready_count),
            ("Action Required (Permits / PEB / HR)", self.summary.action_required_count),
            ("Blocked (Border Lartas / Hub)", self.summary.blocked_count),
            ("Prohibited (Import/Export Ban)", self.summary.prohibited_count),
            ("Validation / Input Errors", self.summary.error_count),
            ("Total Declared Value (CIF/FOB)", float(self.summary.total_declared_value)),
        ]
        if self.mode == "import":
            meta_rows.extend([
                ("Total Estimated Import Taxes", float(self.summary.total_import_taxes)),
                ("Total Net FTA Tax Savings", float(self.summary.total_net_tax_savings)),
            ])
        else:
            meta_rows.append(("Total Estimated Bea Keluar (USD)", float(self.summary.total_export_duty_usd)))

        for idx, (label, val) in enumerate(meta_rows, start=3):
            c_lbl = ws_sum.cell(row=idx, column=1, value=label)
            c_val = ws_sum.cell(row=idx, column=2, value=val)
            if label:
                c_lbl.font = bold_font
                c_lbl.fill = sub_fill
                c_lbl.border = cell_border
                c_val.border = cell_border
                if isinstance(val, float):
                    c_val.number_format = "#,##0.00"
        ws_sum.column_dimensions["A"].width = 36
        ws_sum.column_dimensions["B"].width = 68

        # --- Sheet 2: Screening Results ---
        ws_rows = wb.create_sheet("Screening Results")
        if self.mode == "import":
            headers = [
                "Row", "Reference", "HS Code", "Route", "Date", "Overall Status",
                "Lartas Status", "Recommended Scheme", "Currency", "CIF Value",
                "MFN (%)", "Applied (%)", "Import Taxes", "Net Tax Savings",
                "Missing Permits", "Next Action", "Error",
            ]
        else:
            headers = [
                "Row", "Reference", "HS Code", "Route", "Date", "Overall Status",
                "Export Lartas / PEB", "Recommended FTA", "Currency", "FOB Value",
                "Bea Keluar Status", "Bea Keluar Rate", "Est. Bea Keluar (USD)",
                "Back-to-Back", "Missing Permits / Docs", "Cumulation Tip", "Next Action", "Error",
            ]

        for col_idx, h in enumerate(headers, start=1):
            cell = ws_rows.cell(row=1, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = navy_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = cell_border

        status_fills = {
            "READY": ok_fill,
            "ACTION_REQUIRED": warn_fill,
            "BLOCKED": warn_fill,
            "PROHIBITED": danger_fill,
            "ERROR": danger_fill,
        }

        for r_idx, r in enumerate(self.rows, start=2):
            if self.mode == "import":
                vals: list[object] = [
                    r.row_number, r.reference_id, r.hs_code, r.route, r.as_of_date, r.overall_status,
                    r.lartas_status or "", r.recommended_agreement or "", r.currency,
                    float(r.declared_value) if r.declared_value is not None else None,
                    float(r.base_duty_percent) if r.base_duty_percent is not None else None,
                    float(r.applied_duty_percent) if r.applied_duty_percent is not None else None,
                    float(r.total_import_taxes) if r.total_import_taxes is not None else None,
                    float(r.net_tax_savings) if r.net_tax_savings is not None else None,
                    "; ".join(r.missing_permits), r.next_action or "", r.error or "",
                ]
                num_cols = {10, 11, 12, 13, 14}
            else:
                vals = [
                    r.row_number, r.reference_id, r.hs_code, r.route, r.as_of_date, r.overall_status,
                    r.lartas_status or "", r.recommended_agreement or "", r.currency,
                    float(r.declared_value) if r.declared_value is not None else None,
                    r.export_duty_status or "", r.export_duty_rate_display or "",
                    float(r.export_duty_usd) if r.export_duty_usd is not None else None,
                    r.back_to_back_status or "", "; ".join(r.missing_permits),
                    r.cumulation_tip or "", r.next_action or "", r.error or "",
                ]
                num_cols = {10, 13}

            for c_idx, val in enumerate(vals, start=1):
                cell = ws_rows.cell(row=r_idx, column=c_idx, value=val)
                cell.border = cell_border
                if c_idx == 6 and r.overall_status in status_fills:
                    cell.fill = status_fills[r.overall_status]
                    cell.font = bold_font
                if c_idx in num_cols and isinstance(val, (int, float)):
                    cell.number_format = "#,##0.00"

        ws_rows.freeze_panes = "A2"
        ws_rows.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(self.rows) + 1, 2)}"
        for col_idx in range(1, len(headers) + 1):
            col_letter = get_column_letter(col_idx)
            max_len = max(
                len(str(ws_rows.cell(row=row_i, column=col_idx).value or ""))
                for row_i in range(1, len(self.rows) + 2)
            )
            ws_rows.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 46)

        # --- Sheet 3: Legal Citations ---
        ws_cit = wb.create_sheet("Legal Citations")
        cit_headers = ["Regulation Number", "Document Title", "Clause / Locator", "Effective From", "Official Source URL"]
        for col_idx, h in enumerate(cit_headers, start=1):
            cell = ws_cit.cell(row=1, column=col_idx, value=h)
            cell.font = header_font
            cell.fill = navy_fill
            cell.border = cell_border

        for idx, c in enumerate(self.citations, start=2):
            ws_cit.cell(row=idx, column=1, value=c.regulation_number or c.document_id or c.clause_id).border = cell_border
            ws_cit.cell(row=idx, column=2, value=c.document_title or "").border = cell_border
            ws_cit.cell(row=idx, column=3, value=c.clause_locator or c.clause_id).border = cell_border
            ws_cit.cell(row=idx, column=4, value=c.effective_from or "").border = cell_border
            url_cell = ws_cit.cell(row=idx, column=5, value=c.url or "")
            url_cell.border = cell_border
            if c.url:
                url_cell.hyperlink = c.url
                url_cell.font = Font(color="2563EB", underline="single")

        ws_cit.freeze_panes = "A2"
        for col_idx, width in enumerate((26, 48, 48, 16, 56), start=1):
            ws_cit.column_dimensions[get_column_letter(col_idx)].width = width

        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()


class BatchScreener:
    """Multi-SKU Import (PIB) and Export (PEB + Bea Keluar) batch compliance screener."""

    def __init__(
        self,
        optimizer: MultiFtaOptimizer,
        export_advisor: ExportComplianceAdvisor | None = None,
    ):
        self._opt = optimizer
        self._exp = export_advisor or ExportComplianceAdvisor(optimizer)

    def _screen_import_fast(
        self,
        row_no: int,
        ref_id: str,
        raw: Mapping[str, object],
        *,
        include_explain: bool,
    ) -> tuple[BatchRowOutcome, set[str]]:
        hs_code = str(raw.get("hs_code") or "").strip()
        dispatch = str(raw.get("dispatch_country") or "").strip()
        dest = str(raw.get("destination_country") or "ID").strip()
        as_of = str(raw.get("as_of_date") or "2025-09-15").strip()
        currency = str(raw.get("currency") or "USD").strip().upper()
        cif = _to_decimal(raw.get("cif_value"))
        api_type = str(raw.get("api_type") or "API-P").strip() if raw.get("api_type") else "API-P"
        has_api = _parse_bool(raw.get("has_api"))
        if has_api is None:
            has_api = api_type.upper() != "NONE"
        if not has_api:
            api_type = None

        spec = ShipmentSpec(
            hs_code=hs_code,
            dispatch_country=dispatch,
            destination_country=dest,
            as_of_date=as_of,
            cif_value=cif,
            currency=currency,
            has_api=has_api,
            api_type=api_type,
            provided_lartas_docs=tuple(_parse_str_list(raw.get("provided_lartas_docs"))),
            include_explain=include_explain,
        )

        hs_iri = self._opt.resolve_hs_iri(spec.hs_code)
        disp_iri = self._opt.resolve_country_iri(spec.dispatch_country)
        dest_iri = self._opt.resolve_country_iri(spec.destination_country)
        hs_val = str(self._opt._owl.value(hs_iri, TG.codeValue) or spec.hs_code)
        route = f"{str(disp_iri).split('#')[-1].upper()} -> {str(dest_iri).split('#')[-1].upper()}"

        lartas = self._opt._assess_lartas(hs_iri, TG.jurisdictionIndonesiaCustoms, spec)
        mfn_lit = self._opt._owl.value(hs_iri, TG.mfnDutyPercent)
        mfn_duty = _to_decimal(mfn_lit)
        tax_result = self._opt._query_tax_profile(TG.jurisdictionIndonesiaCustoms)
        tax_rates = tax_result[0] if tax_result is not None else None
        tax_cits = tax_result[1] if tax_result is not None else ()
        clauses: set[str] = set(lartas.source_clauses) | {c.clause_id for c in tax_cits}

        # Query active preferential schedule rates directly from KB (fast SPARQL, no HermiT)
        agr_filter = {a.lower() for a in _parse_str_list(raw.get("agreements"))}
        best_agr = "MFN"
        best_duty = mfn_duty
        best_revoked_on: str | None = None
        best_superseding: str | None = None
        if not lartas.prohibited:
            candidates = self._opt._query_tariff_candidates(hs_iri, disp_iri, dest_iri, spec.as_of_date)
            for cand in candidates:
                agr_slug = str(cand["agreement"]).lower()
                if agr_filter and agr_slug not in agr_filter:
                    continue
                if cand["excluded"]:
                    continue
                pref_duty = cand["effective"]
                if isinstance(pref_duty, Decimal) and (best_duty is None or pref_duty < best_duty):
                    best_duty = pref_duty
                    best_agr = agr_slug
                    best_revoked_on = str(cand["revoked_on"]) if cand.get("revoked_on") else None
                    sup_id = str(cand["superseding_doc"]) if cand.get("superseding_doc") else None
                    if sup_id:
                        reg_num = self._opt._owl.value(TG[sup_id], TG.regulationNumber)
                        best_superseding = str(reg_num) if reg_num is not None else sup_id
                    else:
                        best_superseding = None
                    clauses.update(cand["tariff_clauses"])
        else:
            best_agr = "PROHIBITED"

        mfn_tax = (
            self._opt._compute_tax(cif, currency, mfn_duty, tax_rates, spec.has_api)
            if cif is not None and mfn_duty is not None and tax_rates is not None else None
        )
        app_tax = (
            self._opt._compute_tax(cif, currency, best_duty, tax_rates, spec.has_api)
            if cif is not None and best_duty is not None and tax_rates is not None else None
        )
        savings = (
            (mfn_tax.total_import_taxes - app_tax.total_import_taxes).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if mfn_tax and app_tax and not lartas.prohibited else Decimal("0.00")
        )

        missing = tuple(lartas.missing_border_docs) + tuple(lartas.missing_post_border_docs)
        if lartas.prohibited:
            overall = "PROHIBITED"
            next_act = "STOP: Barang dilarang impor (Permendag Larangan Impor)."
        elif lartas.status == "BORDER_BLOCKED":
            overall = "BLOCKED"
            next_act = f"Lengkapi Lartas Border wajib sebelum PIB: {', '.join(lartas.missing_border_docs)}"
        elif lartas.status == "POST_BORDER_PENDING":
            overall = "ACTION_REQUIRED"
            next_act = f"Siapkan Lartas Post-Border: {', '.join(lartas.missing_post_border_docs)}"
        else:
            overall = "READY"
            next_act = (
                f"Verifikasi RoO & ajukan SKA {best_agr.upper()} (potensi hemat {currency} {savings:,.2f})."
                if best_agr not in {"MFN", "PROHIBITED"} else "Ajukan PIB jalur MFN."
            )

        return BatchRowOutcome(
            row_number=row_no,
            reference_id=ref_id,
            mode="import",
            hs_code=hs_val,
            route=route,
            as_of_date=spec.as_of_date,
            overall_status=overall,
            lartas_status=lartas.status,
            recommended_agreement=best_agr,
            currency=currency,
            declared_value=cif,
            base_duty_percent=mfn_duty,
            applied_duty_percent=None if lartas.prohibited else best_duty,
            total_import_taxes=app_tax.total_import_taxes if app_tax and not lartas.prohibited else None,
            net_tax_savings=savings if not lartas.prohibited else None,
            export_duty_status=None,
            export_duty_rate_display=None,
            export_duty_usd=None,
            missing_permits=missing,
            back_to_back_status=None,
            cumulation_tip=None,
            next_action=next_act,
            source_clauses=tuple(sorted(clauses)),
            tariff_revoked_on=best_revoked_on,
            tariff_superseding_doc=best_superseding,
        ), clauses

    def _screen_import_full(
        self,
        row_no: int,
        ref_id: str,
        raw: Mapping[str, object],
        *,
        include_explain: bool,
    ) -> tuple[BatchRowOutcome, set[str]]:
        payload = dict(raw)
        payload.setdefault("destination_country", "ID")
        payload.setdefault("as_of_date", "2025-09-15")
        payload.setdefault("currency", "USD")
        if "materials" in payload:
            payload["materials"] = _parse_materials_cell(payload["materials"])
        if "provided_origin_docs" in payload:
            payload["provided_origin_docs"] = _parse_str_list(payload["provided_origin_docs"])
        if "provided_lartas_docs" in payload:
            payload["provided_lartas_docs"] = _parse_str_list(payload["provided_lartas_docs"])
        if "has_api" in payload:
            parsed_api = _parse_bool(payload["has_api"])
            payload["has_api"] = True if parsed_api is None else parsed_api
        if "wo_confirmed" in payload:
            payload["wo_confirmed"] = _parse_bool(payload["wo_confirmed"])
        payload["include_plan"] = False
        payload["include_explain"] = include_explain

        agr_list = tuple(a.lower() for a in _parse_str_list(payload.get("agreements")))
        spec = ShipmentSpec.from_dict(payload)
        res = self._opt.optimize(spec, agreements=agr_list or None)
        winner = next((s for s in res.schemes if s.agreement == res.recommended_agreement), None)
        win_revoked_on = winner.tariff_revoked_on if winner else None
        win_superseding = None
        if winner and winner.tariff_superseding_doc:
            reg_num = self._opt._owl.value(TG[winner.tariff_superseding_doc], TG.regulationNumber)
            win_superseding = str(reg_num) if reg_num is not None else winner.tariff_superseding_doc
        clauses: set[str] = set(res.lartas.source_clauses)
        for c in res.tax_profile_citations:
            clauses.add(c.clause_id)
        for s in res.schemes:
            clauses.update(s.source_clauses)

        missing = tuple(res.lartas.missing_border_docs) + tuple(res.lartas.missing_post_border_docs)
        if res.recommended_agreement == "PROHIBITED":
            overall = "PROHIBITED"
            next_act = "STOP: Barang dilarang impor."
        elif res.lartas.status == "BORDER_BLOCKED":
            overall = "BLOCKED"
            next_act = f"Lengkapi Lartas Border wajib: {', '.join(res.lartas.missing_border_docs)}"
        elif res.lartas.status == "POST_BORDER_PENDING" or (winner and winner.eligibility == "ELIGIBLE_PENDING_DOCS"):
            overall = "ACTION_REQUIRED"
            parts = []
            if winner and winner.eligibility == "ELIGIBLE_PENDING_DOCS":
                parts.append(f"Lengkapi bukti asal {winner.agreement.upper()}")
            if res.lartas.missing_post_border_docs:
                parts.append(f"Lartas Post-Border: {', '.join(res.lartas.missing_post_border_docs)}")
            next_act = "; ".join(parts) or "Lengkapi dokumen pendukung PIB."
        else:
            overall = "READY"
            next_act = f"Siap ajukan PIB dengan skema {res.recommended_agreement.upper()}."

        applied_duty = winner.applied_duty_percent if winner else res.mfn_duty_percent
        total_tax = (
            winner.tax_breakdown.total_import_taxes if winner and winner.tax_breakdown
            else (res.mfn_tax_breakdown.total_import_taxes if res.mfn_tax_breakdown else None)
        )
        savings = winner.total_tax_savings if winner else Decimal("0.00")

        return BatchRowOutcome(
            row_number=row_no,
            reference_id=ref_id,
            mode="import",
            hs_code=res.hs_code,
            route=f"{res.dispatch_country.upper()} -> {res.destination_country.upper()}",
            as_of_date=res.as_of_date,
            overall_status=overall,
            lartas_status=res.lartas.status,
            recommended_agreement=res.recommended_agreement,
            currency=spec.currency,
            declared_value=spec.cif_value,
            base_duty_percent=res.mfn_duty_percent,
            applied_duty_percent=None if res.recommended_agreement == "PROHIBITED" else applied_duty,
            total_import_taxes=None if res.recommended_agreement == "PROHIBITED" else total_tax,
            net_tax_savings=None if res.recommended_agreement == "PROHIBITED" else savings,
            export_duty_status=None,
            export_duty_rate_display=None,
            export_duty_usd=None,
            missing_permits=missing,
            back_to_back_status=None,
            cumulation_tip=None,
            next_action=next_act,
            source_clauses=tuple(sorted(clauses)),
            tariff_revoked_on=win_revoked_on,
            tariff_superseding_doc=win_superseding,
        ), clauses

    def _screen_export(
        self,
        row_no: int,
        ref_id: str,
        raw: Mapping[str, object],
        *,
        include_explain: bool,
    ) -> tuple[BatchRowOutcome, set[str]]:
        payload = dict(raw)
        payload.setdefault("origin_country", "ID")
        payload.setdefault("as_of_date", "2025-09-15")
        payload.setdefault("currency", "USD")
        if "materials" in payload:
            payload["materials"] = _parse_materials_cell(payload["materials"])
        if "provided_export_docs" in payload:
            payload["provided_export_docs"] = _parse_str_list(payload["provided_export_docs"])
        if "requires_back_to_back" in payload:
            payload["requires_back_to_back"] = bool(_parse_bool(payload["requires_back_to_back"]))
        if "wo_confirmed" in payload:
            payload["wo_confirmed"] = _parse_bool(payload["wo_confirmed"])
        payload["include_explain"] = include_explain

        spec = ExportSpec.from_dict(payload)
        res = self._exp.evaluate(spec)
        winner = next((s for s in res.schemes if s.agreement == res.recommended_agreement), None)
        bk = res.export_duty

        clauses: set[str] = set(res.export_lartas.source_clauses) | set(bk.source_clauses)
        for s in res.schemes:
            clauses.update(s.source_clauses)

        route = (
            f"{res.origin_country.upper()} -> {res.transshipment_hub.upper()} -> {res.destination_country.upper()}"
            if res.transshipment_hub else f"{res.origin_country.upper()} -> {res.destination_country.upper()}"
        )
        if bk.duty_basis == "SPECIFIC_USD_PER_MT" and bk.specific_rate_usd_per_mt is not None:
            bk_rate_disp = f"USD {bk.specific_rate_usd_per_mt:.2f}/MT (Kolom {bk.tier_column})"
        elif bk.duty_basis == "AD_VALOREM" and bk.rate_percent is not None:
            tier_sfx = f" (Kolom {bk.tier_column})" if bk.tier_column is not None else ""
            bk_rate_disp = f"{bk.rate_percent:.2f}%{tier_sfx}"
        elif bk.status == "REFERENCE_PRICE_REQUIRED":
            bk_rate_disp = "Perlu HR (USD/MT)"
        else:
            bk_rate_disp = "0%"

        if res.recommended_agreement == "PROHIBITED_EXPORT":
            overall = "PROHIBITED"
            next_act = "STOP: Barang dilarang ekspor (Permendag 8/2025); PEB & SKA diblokir."
        elif res.export_lartas.status == "EXPORT_PERMITS_MISSING":
            overall = "BLOCKED"
            next_act = res.checklist[0].title if res.checklist else "Lengkapi perizinan Lartas Ekspor."
        elif res.export_lartas.status == "PEB_PENDING" or bk.status in {"APPLICABLE", "REFERENCE_PRICE_REQUIRED"}:
            overall = "ACTION_REQUIRED"
            next_act = res.checklist[0].title if res.checklist else "Ajukan PEB (BC 3.0) dan lunasi Bea Keluar."
        else:
            overall = "READY"
            next_act = (
                f"Siap ekspor dengan {winner.origin_proof_doc or winner.agreement.upper()}."
                if winner else "Siap ekspor non-preferensi."
            )

        cum_tip = (
            f"Ganti bahan {res.cumulation_opportunities[0].failing_material_hs} ke mitra {', '.join(res.cumulation_opportunities[0].recommended_partner_countries[:3])} ({res.cumulation_opportunities[0].agreement.upper()})"
            if res.cumulation_opportunities else None
        )
        b2b_status = (
            winner.back_to_back_status if winner
            else (res.schemes[0].back_to_back_status if res.schemes else "NOT_REQUESTED")
        )

        return BatchRowOutcome(
            row_number=row_no,
            reference_id=ref_id,
            mode="export",
            hs_code=res.hs_code,
            route=route,
            as_of_date=res.as_of_date,
            overall_status=overall,
            lartas_status=res.export_lartas.status,
            recommended_agreement=res.recommended_agreement,
            currency=spec.currency,
            declared_value=spec.fob_value,
            base_duty_percent=None,
            applied_duty_percent=None,
            total_import_taxes=None,
            net_tax_savings=None,
            export_duty_status=bk.status,
            export_duty_rate_display=bk_rate_disp,
            export_duty_usd=bk.estimated_duty_usd,
            missing_permits=res.export_lartas.missing_docs,
            back_to_back_status=b2b_status,
            cumulation_tip=cum_tip,
            next_action=next_act,
            source_clauses=tuple(sorted(clauses)),
        ), clauses

    def screen(
        self,
        rows: Sequence[Mapping[str, object]],
        *,
        mode: BatchMode,
        screening_depth: ScreeningDepth = "full",
        include_explain: bool = True,
    ) -> BatchScreeningResult:
        outcomes: list[BatchRowOutcome] = []
        all_clauses: set[str] = set()

        for idx, raw_item in enumerate(rows, start=1):
            norm = normalize_batch_row(raw_item)
            ref_id = str(norm.get("reference_id") or norm.get("sku") or norm.get("line_id") or f"ROW-{idx:03d}")
            try:
                if mode == "import":
                    if screening_depth == "fast_lartas":
                        outcome, row_clauses = self._screen_import_fast(idx, ref_id, norm, include_explain=include_explain)
                    else:
                        outcome, row_clauses = self._screen_import_full(idx, ref_id, norm, include_explain=include_explain)
                else:
                    outcome, row_clauses = self._screen_export(idx, ref_id, norm, include_explain=include_explain)
                outcomes.append(outcome)
                all_clauses.update(row_clauses)
            except (ValueError, KeyError, InvalidOperation, json.JSONDecodeError) as err:
                outcomes.append(BatchRowOutcome(
                    row_number=idx,
                    reference_id=ref_id,
                    mode=mode,
                    hs_code=str(norm.get("hs_code") or ""),
                    route="",
                    as_of_date=str(norm.get("as_of_date") or ""),
                    overall_status="ERROR",
                    lartas_status=None,
                    recommended_agreement=None,
                    currency=str(norm.get("currency") or "USD").upper(),
                    declared_value=None,
                    base_duty_percent=None,
                    applied_duty_percent=None,
                    total_import_taxes=None,
                    net_tax_savings=None,
                    export_duty_status=None,
                    export_duty_rate_display=None,
                    export_duty_usd=None,
                    missing_permits=(),
                    back_to_back_status=None,
                    cumulation_tip=None,
                    next_action=None,
                    source_clauses=(),
                    error=str(err),
                ))

        summary = BatchScreeningSummary(
            total_rows=len(outcomes),
            ready_count=sum(1 for r in outcomes if r.overall_status == "READY"),
            action_required_count=sum(1 for r in outcomes if r.overall_status == "ACTION_REQUIRED"),
            blocked_count=sum(1 for r in outcomes if r.overall_status == "BLOCKED"),
            prohibited_count=sum(1 for r in outcomes if r.overall_status == "PROHIBITED"),
            error_count=sum(1 for r in outcomes if r.overall_status == "ERROR"),
            total_declared_value=sum((r.declared_value or Decimal("0.00") for r in outcomes), Decimal("0.00")),
            total_import_taxes=sum((r.total_import_taxes or Decimal("0.00") for r in outcomes), Decimal("0.00")),
            total_net_tax_savings=sum((r.net_tax_savings or Decimal("0.00") for r in outcomes), Decimal("0.00")),
            total_export_duty_usd=sum((r.export_duty_usd or Decimal("0.00") for r in outcomes), Decimal("0.00")),
        )
        citations = self._opt.resolve_citations(all_clauses) if include_explain and all_clauses else ()
        return BatchScreeningResult(
            kb_version=self._opt.kb.snapshot.version,
            kb_sha256=self._opt.kb.snapshot.sha256,
            mode=mode,
            screening_depth=screening_depth,
            generated_at=datetime.now(UTC).isoformat(),
            summary=summary,
            rows=tuple(outcomes),
            citations=citations,
        )
