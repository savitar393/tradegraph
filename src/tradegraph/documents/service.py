"""Declarative KB-driven Document Template Resolver & Draft Generator (.docx, printable HTML, ZIP bundle)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import html
import io
import re
import zipfile

from lxml import etree
from rdflib import RDFS

from tradegraph.kb.loader import TG
from tradegraph.kb.queries import prepare_local_query
from tradegraph.optimization.service import MultiFtaOptimizer, ProvenanceCitation

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
W = f"{{{W_NS}}}"
WIB = timezone(timedelta(hours=7))


@dataclass(frozen=True, slots=True)
class DocumentFieldSpec:
    key: str
    label: str
    section: str
    input_kind: str
    order: int
    required: bool
    auto_fill_key: str | None = None
    placeholder: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "section": self.section,
            "input_kind": self.input_kind,
            "order": self.order,
            "required": self.required,
            "auto_fill_key": self.auto_fill_key,
            "placeholder": self.placeholder,
        }


@dataclass(frozen=True, slots=True)
class DocumentTemplateSpec:
    template_code: str
    title: str
    subtitle: str
    form_number: str
    issuing_portal: str
    direction: str
    document_types: tuple[str, ...]
    agreement: str | None
    source_clauses: tuple[str, ...]
    fields: tuple[DocumentFieldSpec, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "template_code": self.template_code,
            "title": self.title,
            "subtitle": self.subtitle,
            "form_number": self.form_number,
            "issuing_portal": self.issuing_portal,
            "direction": self.direction,
            "document_types": list(self.document_types),
            "agreement": self.agreement,
            "source_clauses": list(self.source_clauses),
            "fields": [f.to_dict() for f in self.fields],
        }


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    template_code: str
    title: str
    subtitle: str
    form_number: str
    issuing_portal: str
    direction: str
    requirement_reason: str
    target_doc_type: str | None
    fields: tuple[DocumentFieldSpec, ...]
    prefilled_values: Mapping[str, str]
    missing_required_fields: tuple[str, ...]
    citations: tuple[ProvenanceCitation, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "template_code": self.template_code,
            "title": self.title,
            "subtitle": self.subtitle,
            "form_number": self.form_number,
            "issuing_portal": self.issuing_portal,
            "direction": self.direction,
            "requirement_reason": self.requirement_reason,
            "target_doc_type": self.target_doc_type,
            "fields": [f.to_dict() for f in self.fields],
            "prefilled_values": dict(self.prefilled_values),
            "missing_required_fields": list(self.missing_required_fields),
            "citations": [c.to_dict() for c in self.citations],
        }


@dataclass(frozen=True, slots=True)
class PreparedDocumentPackage:
    mode: str
    kb_version: str
    hs_code: str
    recommended_agreement: str
    blocked: bool
    block_reason: str | None
    documents: tuple[PreparedDocument, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "kb_version": self.kb_version,
            "hs_code": self.hs_code,
            "recommended_agreement": self.recommended_agreement,
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "documents": [d.to_dict() for d in self.documents],
        }


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").lower() or "draft"


class DocumentGeneratorService:
    """Resolves required compliance documents from the canonical KB and generates filled .docx / HTML / ZIP drafts."""

    def __init__(self, optimizer: MultiFtaOptimizer):
        self._opt = optimizer
        self.kb = optimizer.kb
        self._owl = optimizer._owl
        self._templates_cache: dict[str, DocumentTemplateSpec] | None = None

    def list_templates(self) -> tuple[DocumentTemplateSpec, ...]:
        """Load all DocumentDraftTemplate specifications declaratively from the canonical KB."""
        if self._templates_cache is not None:
            return tuple(self._templates_cache.values())

        qdef = self.kb.query_catalog.get("document-draft-templates")
        if qdef is None:
            self._templates_cache = {}
            return ()

        query = prepare_local_query(qdef.sparql, "SelectQuery")
        grouped: dict[str, dict[str, object]] = {}
        for row in self._owl.query(query):
            d = row.asdict()
            code = str(d["code"])
            entry = grouped.setdefault(code, {
                "title": str(d["title"]),
                "subtitle": str(d["subtitle"]),
                "form_number": str(d["formNumber"]),
                "portal": str(d["portal"]),
                "direction": str(d["direction"]),
                "doc_types": set(),
                "agreement": str(d["agreement"]).split("#")[-1] if d.get("agreement") else None,
                "clauses": set(),
                "fields": {},
            })
            if d.get("docType"):
                entry["doc_types"].add(str(d["docType"]).split("#")[-1])
            if d.get("clause"):
                entry["clauses"].add(str(d["clause"]).split("#")[-1])
            fkey = str(d["fieldKey"])
            if fkey not in entry["fields"]:
                entry["fields"][fkey] = DocumentFieldSpec(
                    key=fkey,
                    label=str(d["fieldLabel"]),
                    section=str(d["fieldSection"]),
                    input_kind=str(d["fieldKind"]),
                    order=int(str(d["fieldOrder"])),
                    required=str(d["fieldRequired"]).lower() == "true",
                    auto_fill_key=str(d["autoFillKey"]) if d.get("autoFillKey") else None,
                    placeholder=str(d["placeholder"]) if d.get("placeholder") else None,
                )

        specs: dict[str, DocumentTemplateSpec] = {}
        for code in sorted(grouped):
            g = grouped[code]
            sorted_fields = tuple(sorted(g["fields"].values(), key=lambda f: (f.order, f.key)))
            specs[code] = DocumentTemplateSpec(
                template_code=code,
                title=str(g["title"]),
                subtitle=str(g["subtitle"]),
                form_number=str(g["form_number"]),
                issuing_portal=str(g["portal"]),
                direction=str(g["direction"]),
                document_types=tuple(sorted(g["doc_types"])),
                agreement=g["agreement"] if isinstance(g["agreement"], str) else None,
                source_clauses=tuple(sorted(g["clauses"])),
                fields=sorted_fields,
            )
        self._templates_cache = specs
        return tuple(specs.values())

    def get_template(self, template_code: str) -> DocumentTemplateSpec:
        self.list_templates()
        assert self._templates_cache is not None
        tpl = self._templates_cache.get(template_code)
        if tpl is None:
            raise ValueError(f"Unknown document template {template_code!r}.")
        return tpl

    def _find_template_for_doc_type(
        self,
        doc_type: str,
        *,
        agreement: str | None = None,
        direction: str | None = None,
    ) -> DocumentTemplateSpec | None:
        templates = self.list_templates()
        if agreement:
            for tpl in templates:
                if doc_type in tpl.document_types and tpl.agreement == agreement:
                    return tpl
        for tpl in templates:
            if doc_type in tpl.document_types:
                if direction is None or tpl.direction in ("BOTH", direction):
                    return tpl
        return None

    def _hs_description(self, hs_code: str) -> str:
        try:
            hs_iri = self._opt.resolve_hs_iri(hs_code)
            lbl = self._owl.value(hs_iri, RDFS.label)
            if lbl:
                return str(lbl)
        except ValueError:
            pass
        return f"Komoditas HS {hs_code}"

    def _build_autofill_context(
        self,
        mode: str,
        rec: Mapping[str, object],
        user_info: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        hs_code = str(rec.get("hs_code") or "")
        as_of_date = str(rec.get("as_of_date") or datetime.now(WIB).strftime("%Y-%m-%d"))
        rec_agr = str(rec.get("recommended_agreement") or "")
        schemes = rec.get("schemes") if isinstance(rec.get("schemes"), list) else []
        winner = next((s for s in schemes if isinstance(s, Mapping) and s.get("agreement") == rec_agr), None)

        origin = str(rec.get("dispatch_country") or rec.get("origin_country") or "").upper()
        dest = str(rec.get("destination_country") or "").upper()

        agr_label = str(winner.get("agreement_label") or rec_agr.upper()) if winner else rec_agr.upper()
        fac_code = str(winner.get("facility_code") or "-") if winner else "-"
        agr_with_code = (
            f"{agr_label} (Kode Fasilitas PIB {fac_code})"
            if winner and fac_code != "-" else agr_label
        )

        concise_rule = str(winner.get("concise_rule") or "") if winner else ""
        tpi_instr = str(winner.get("third_party_invoice_instruction") or "") if winner else ""
        tpi_country = str(rec.get("third_party_invoice_country") or "")
        tpi_combined = (
            f"Negara Penerbit Invoice Pihak Ketiga: {tpi_country.upper()} — {tpi_instr}"
            if tpi_country and tpi_instr else (tpi_instr or tpi_country)
        )

        # Customs value & tax / export duty summary
        customs_value = ""
        tax_summary = ""
        qty_mt = ""
        if mode == "import":
            tax = (winner.get("tax_breakdown") if winner else None) or rec.get("mfn_tax_breakdown")
            if isinstance(tax, Mapping):
                curr = str(tax.get("currency") or "USD")
                if tax.get("cif_value"):
                    customs_value = f"{curr} {tax['cif_value']} (CIF)"
                tax_summary = (
                    f"Tarif BM: {tax.get('duty_percent', '-')}% (BM {curr} {tax.get('bm_amount', '-')}) | "
                    f"PPN {tax.get('vat_percent', '11')}%: {curr} {tax.get('ppn_amount', '-')} | "
                    f"PPh 22 ({tax.get('pph22_percent', '-')}%): {curr} {tax.get('pph22_amount', '-')} | "
                    f"Total Pajak Impor: {curr} {tax.get('total_import_taxes', '-')}"
                )
        else:
            duty = rec.get("export_duty") if isinstance(rec.get("export_duty"), Mapping) else {}
            if duty.get("quantity_mt"):
                qty_mt = f"{duty['quantity_mt']} MT"
            if duty.get("taxable_export_value_usd"):
                customs_value = f"USD {duty['taxable_export_value_usd']} (Nilai Ekspor / FOB)"
            elif rec.get("fob_value"):
                customs_value = f"{rec.get('currency', 'USD')} {rec['fob_value']} (FOB)"
            d_status = str(duty.get("status") or "NOT_SUBJECT")
            if d_status == "APPLICABLE":
                rate = (
                    f"USD {duty['specific_rate_usd_per_mt']}/MT"
                    if duty.get("duty_basis") == "SPECIFIC_USD_PER_MT"
                    else f"{duty.get('rate_percent', '0')}%"
                )
                tax_summary = (
                    f"Objek Bea Keluar ({duty.get('commodity_group', '-')}) | Tarif: {rate} "
                    f"| Estimasi Bea Keluar: USD {duty.get('estimated_duty_usd', '-')}"
                )
            elif d_status == "REFERENCE_PRICE_REQUIRED":
                tax_summary = "Objek Bea Keluar Progresif — wajib melampirkan Harga Referensi (HR) Kemendag periode berjalan."
            elif d_status == "ZERO_RATE":
                tax_summary = "Tarif Bea Keluar aktif 0% (USD 0.00)."
            else:
                tax_summary = "Komoditas bukan objek pengenaan Bea Keluar aktif."

        lartas = rec.get("lartas") if isinstance(rec.get("lartas"), Mapping) else {}
        api_type = str(lartas.get("api_type") or "")

        ctx: dict[str, str] = {
            "hs_code": hs_code,
            "hs_description": self._hs_description(hs_code) if hs_code else "",
            "origin_country": origin,
            "destination_country": dest,
            "as_of_date": as_of_date,
            "agreement_with_code": agr_with_code,
            "origin_criterion": concise_rule,
            "customs_value": customs_value,
            "quantity_mt": qty_mt,
            "tax_or_duty_summary": tax_summary,
            "api_type": api_type,
            "transshipment_hub": str(rec.get("transshipment_hub") or "").upper(),
            "third_party_invoice_info": tpi_combined,
            "rvc_percent": f"{rec['rvc_percent']}%" if rec.get("rvc_percent") not in (None, "") else "",
            "dv20_percent": f"{rec['dv20_percent']}%" if rec.get("dv20_percent") not in (None, "") else "",
            "highest_contributor_country": str(rec.get("highest_contributor_country") or "").upper(),
            "sign_place_date": f"Jakarta, {as_of_date}",
        }
        if user_info:
            if user_info.get("name"):
                ctx["user_name"] = str(user_info["name"])
            if user_info.get("company"):
                ctx["user_company"] = str(user_info["company"])
        return {k: v for k, v in ctx.items() if v}

    def prepare_from_recommendation(
        self,
        *,
        mode: str,
        recommendation: Mapping[str, object],
        user_info: Mapping[str, str] | None = None,
        extra_inputs: Mapping[str, str] | None = None,
    ) -> PreparedDocumentPackage:
        """Derive all required KB document templates for an import or export recommendation with prefilled fields."""
        norm_mode = mode.strip().lower()
        if norm_mode not in {"import", "export"}:
            raise ValueError("mode must be 'import' or 'export'.")

        hs_code = str(recommendation.get("hs_code") or "")
        rec_agr = str(recommendation.get("recommended_agreement") or "")

        # Hard-block prohibited goods
        if rec_agr in {"PROHIBITED", "PROHIBITED_EXPORT"}:
            lartas_key = "lartas" if norm_mode == "import" else "export_lartas"
            lartas_obj = recommendation.get(lartas_key) if isinstance(recommendation.get(lartas_key), Mapping) else {}
            diags = lartas_obj.get("diagnostics") if isinstance(lartas_obj.get("diagnostics"), list) else []
            reason = str(diags[0]) if diags else "Barang termasuk kategori DILARANG sehingga dokumen kepabeanan tidak dapat diterbitkan."
            return PreparedDocumentPackage(
                mode=norm_mode,
                kb_version=self.kb.snapshot.version,
                hs_code=hs_code,
                recommended_agreement=rec_agr,
                blocked=True,
                block_reason=reason,
                documents=(),
            )

        autofill = self._build_autofill_context(norm_mode, recommendation, user_info=user_info)
        if extra_inputs:
            for k, v in extra_inputs.items():
                if v is not None and str(v).strip():
                    autofill[str(k)] = str(v).strip()

        schemes = recommendation.get("schemes") if isinstance(recommendation.get("schemes"), list) else []
        winner = next((s for s in schemes if isinstance(s, Mapping) and s.get("agreement") == rec_agr), None)

        selected: list[tuple[DocumentTemplateSpec, str, str | None]] = []
        seen_keys: set[tuple[str, str | None]] = set()

        def add_selected(tpl: DocumentTemplateSpec | None, reason: str, doc_type: str | None) -> None:
            if tpl is None:
                return
            key = (tpl.template_code, doc_type if tpl.template_code == "export-lartas-permit" else None)
            if key in seen_keys:
                return
            seen_keys.add(key)
            selected.append((tpl, reason, doc_type))

        if norm_mode == "import":
            # 1. FTA Origin Proof (if an FTA scheme is recommended)
            if winner is not None:
                agr = str(winner.get("agreement") or "")
                agr_iri = TG[agr]
                prim_doc = self._owl.value(agr_iri, TG.primaryOriginDocumentType)
                prim_slug = str(prim_doc).split("#")[-1] if prim_doc else ""
                tpl = self._find_template_for_doc_type(prim_slug, agreement=agr, direction="IMPORT")
                add_selected(
                    tpl,
                    f"Wajib untuk klaim tarif preferensi {winner.get('agreement_label', agr.upper())} (Kode Fasilitas PIB {winner.get('facility_code', '-')}).",
                    prim_slug or None,
                )
                if winner.get("requires_rcep_dv20") and winner.get("dv20_status") != "SATISFIED":
                    dv_tpl = self._find_template_for_doc_type("dv20Statement", agreement="rcep", direction="IMPORT")
                    add_selected(
                        dv_tpl,
                        "Wajib untuk pos Lampiran II RCEP (Tariff Differentials, DV20 >= 20% FOB sesuai PMK 209/2022 Pasal 14).",
                        "dv20Statement",
                    )

            # 2. Missing Import Lartas Permits (Border + Post-Border)
            lartas = recommendation.get("lartas") if isinstance(recommendation.get("lartas"), Mapping) else {}
            missing_border = list(lartas.get("missing_border_docs") or [])
            missing_post = list(lartas.get("missing_post_border_docs") or [])
            for doc in missing_border:
                tpl = self._find_template_for_doc_type(str(doc), direction="IMPORT")
                add_selected(tpl, f"Perizinan Lartas BORDER wajib sebelum barang keluar pelabuhan ({doc}).", str(doc))
            for doc in missing_post:
                tpl = self._find_template_for_doc_type(str(doc), direction="IMPORT")
                add_selected(tpl, f"Perizinan Lartas POST-BORDER wajib dipenuhi untuk kepatuhan impor ({doc}).", str(doc))

            # 3. PIB / BC 2.0 Draft Worksheet
            pib_tpl = self._find_template_for_doc_type("pibDeclarationDoc", direction="IMPORT")
            add_selected(pib_tpl, "Lembar kerja pengajuan Pemberitahuan Impor Barang (PIB / BC 2.0) ke CEISA 4.0.", "pibDeclarationDoc")

        else:
            # 1. Missing Export Lartas Permits
            exp_lartas = recommendation.get("export_lartas") if isinstance(recommendation.get("export_lartas"), Mapping) else {}
            for doc in list(exp_lartas.get("missing_docs") or []):
                if str(doc) == "pebDeclaration":
                    continue
                tpl = self._find_template_for_doc_type(str(doc), direction="EXPORT")
                doc_lbl = str(self._owl.value(TG[str(doc)], RDFS.label) or doc)
                add_selected(tpl, f"Perizinan Lartas Ekspor wajib sebelum pengajuan PEB: {doc_lbl}.", str(doc))

            # 2. PEB / BC 3.0 Draft (if not filed)
            if not exp_lartas.get("peb_filed"):
                peb_tpl = self._find_template_for_doc_type("pebDeclaration", direction="EXPORT")
                add_selected(peb_tpl, "Pemberitahuan Ekspor Barang (PEB / BC 3.0) wajib ke SKP DJBC untuk memperoleh NPE.", "pebDeclaration")

            # 3. Recommended FTA Certificate of Origin (e-SKA)
            if winner is not None:
                agr = str(winner.get("agreement") or "")
                origin_doc = str(winner.get("origin_proof_doc") or "")
                tpl = self._find_template_for_doc_type(origin_doc, agreement=agr, direction="EXPORT")
                add_selected(
                    tpl,
                    f"Diterbitkan eksportir melalui e-SKA Kemendag agar pembeli memperoleh tarif preferensi {winner.get('agreement_label', agr.upper())}.",
                    origin_doc or None,
                )
                # 4. Back-to-Back CO if requested & allowed
                if recommendation.get("requires_back_to_back") and winner.get("back_to_back_status") == "ALLOWED":
                    b2b_tpl = self._find_template_for_doc_type("backToBackCooDocument", direction="EXPORT")
                    add_selected(
                        b2b_tpl,
                        f"Permohonan penerbitan ulang Back-to-Back CO di hub transshipment {str(recommendation.get('transshipment_hub') or '').upper()}.",
                        "backToBackCooDocument",
                    )

        prepared_docs: list[PreparedDocument] = []
        for tpl, reason, doc_type in selected:
            prefilled: dict[str, str] = {}
            missing_req: list[str] = []
            for f in tpl.fields:
                val = ""
                if extra_inputs and f.key in extra_inputs and str(extra_inputs[f.key]).strip():
                    val = str(extra_inputs[f.key]).strip()
                elif f.auto_fill_key and f.auto_fill_key in autofill:
                    val = autofill[f.auto_fill_key]
                elif f.key in autofill:
                    val = autofill[f.key]
                if val:
                    prefilled[f.key] = val
                elif f.required:
                    missing_req.append(f.key)

            # Customize title slightly when multiple export Lartas docs share the export-lartas-permit template
            title = tpl.title
            if tpl.template_code == "export-lartas-permit" and doc_type:
                doc_lbl = str(self._owl.value(TG[doc_type], RDFS.label) or doc_type)
                title = f"Draf Permohonan {doc_lbl}"

            citations = self._opt.resolve_citations(tpl.source_clauses)
            prepared_docs.append(PreparedDocument(
                template_code=tpl.template_code,
                title=title,
                subtitle=tpl.subtitle,
                form_number=tpl.form_number,
                issuing_portal=tpl.issuing_portal,
                direction=tpl.direction,
                requirement_reason=reason,
                target_doc_type=doc_type,
                fields=tpl.fields,
                prefilled_values=prefilled,
                missing_required_fields=tuple(missing_req),
                citations=citations,
            ))

        return PreparedDocumentPackage(
            mode=norm_mode,
            kb_version=self.kb.snapshot.version,
            hs_code=hs_code,
            recommended_agreement=rec_agr,
            blocked=False,
            block_reason=None,
            documents=tuple(prepared_docs),
        )

    def generate_html_preview(
        self,
        template_code: str,
        field_values: Mapping[str, object],
        *,
        custom_title: str | None = None,
        requirement_reason: str | None = None,
    ) -> str:
        """Render a clean, print-ready HTML document preview."""
        tpl = self.get_template(template_code)
        title = custom_title or tpl.title
        citations = self._opt.resolve_citations(tpl.source_clauses)
        generated_at = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")

        sections: list[tuple[str, list[DocumentFieldSpec]]] = []
        sec_map: dict[str, list[DocumentFieldSpec]] = {}
        for f in tpl.fields:
            if f.section not in sec_map:
                sec_map[f.section] = []
                sections.append((f.section, sec_map[f.section]))
            sec_map[f.section].append(f)

        sec_html = []
        for sec_name, fields in sections:
            rows = []
            for f in fields:
                raw_val = str(field_values.get(f.key) or "").strip()
                val_html = (
                    html.escape(raw_val).replace("\n", "<br/>")
                    if raw_val
                    else '<span style="color:#94a3b8;font-style:italic;">(Belum diisi)</span>'
                )
                req_mark = ' <span style="color:#dc2626;">*</span>' if f.required else ""
                rows.append(
                    f"<tr><td style='width:38%;font-weight:600;background:#f8fafc;'>{html.escape(f.label)}{req_mark}</td>"
                    f"<td>{val_html}</td></tr>"
                )
            sec_html.append(
                f"<h3>{html.escape(sec_name)}</h3>"
                f"<table><tbody>{''.join(rows)}</tbody></table>"
            )

        cit_rows = "".join(
            f"<tr><td>{html.escape(c.regulation_number or c.document_label or c.clause_id)}</td>"
            f"<td>{html.escape(c.clause_locator or c.clause_id)}</td></tr>"
            for c in citations
        )
        sign_name = html.escape(str(field_values.get("signatory_name") or "........................................"))
        sign_title = html.escape(str(field_values.get("signatory_title") or "Pejabat Berwenang"))
        sign_date = html.escape(str(field_values.get("sign_place_date") or datetime.now(WIB).strftime("Jakarta, %Y-%m-%d")))
        company = html.escape(str(field_values.get("company_name") or ""))

        return f"""<!DOCTYPE html>
<html lang="id"><head><meta charset="utf-8"/>
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: "Times New Roman", Georgia, serif; color: #111827; max-width: 820px; margin: 24px auto; padding: 24px; line-height: 1.45; }}
  .doc-header {{ border-bottom: 2px solid #111827; padding-bottom: 12px; margin-bottom: 16px; }}
  .doc-kicker {{ font-family: Arial, sans-serif; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: #475569; }}
  h1 {{ font-size: 18px; margin: 4px 0 4px; text-transform: uppercase; }}
  .doc-sub {{ font-size: 12.5px; color: #334155; margin: 0; }}
  .meta-bar {{ display: flex; flex-wrap: wrap; gap: 14px; font-family: Arial, sans-serif; font-size: 11px; color: #334155; margin-top: 8px; background: #f1f5f9; padding: 8px 10px; border: 1px solid #cbd5e1; }}
  .reason-box {{ font-size: 12px; background: #f8fafc; border-left: 3px solid #0284c7; padding: 8px 12px; margin: 12px 0; }}
  h3 {{ font-family: Arial, sans-serif; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; margin: 16px 0 6px; padding-bottom: 3px; border-bottom: 1px solid #94a3b8; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 10px; font-size: 12.5px; }}
  td, th {{ border: 1px solid #94a3b8; padding: 6px 8px; vertical-align: top; text-align: left; }}
  th {{ background: #e2e8f0; font-family: Arial, sans-serif; font-size: 11.5px; }}
  .sig-box {{ margin-top: 28px; display: flex; justify-content: flex-end; }}
  .sig-inner {{ width: 280px; text-align: center; font-size: 12.5px; }}
  .sig-space {{ height: 64px; }}
  .footer-note {{ margin-top: 24px; font-family: Arial, sans-serif; font-size: 10.5px; color: #64748b; border-top: 1px dashed #cbd5e1; padding-top: 8px; }}
  @media print {{ body {{ margin: 0; padding: 12px; }} }}
</style></head><body>
  <div class="doc-header">
    <div class="doc-kicker">Draft Persiapan Dokumen Kepabeanan &middot; TradeGraph KB v{html.escape(self.kb.snapshot.version)}</div>
    <h1>{html.escape(title)}</h1>
    <p class="doc-sub">{html.escape(tpl.subtitle)}</p>
    <div class="meta-bar">
      <span><strong>Formulir:</strong> {html.escape(tpl.form_number)}</span>
      <span><strong>Portal / Instansi:</strong> {html.escape(tpl.issuing_portal)}</span>
      <span><strong>Dibuat:</strong> {html.escape(generated_at)}</span>
    </div>
  </div>
  {f'<div class="reason-box"><strong>Dasar Kebutuhan Rekomendasi:</strong> {html.escape(requirement_reason)}</div>' if requirement_reason else ''}
  {''.join(sec_html)}
  <h3>DASAR HUKUM &amp; RUJUKAN PASAL (KB TRADEGRAPH)</h3>
  <table><thead><tr><th>Peraturan Resmi</th><th>Pasal / Lampiran</th></tr></thead><tbody>{cit_rows}</tbody></table>
  <div class="sig-box"><div class="sig-inner">
    <div>{sign_date}</div>
    <div>{company}</div>
    <div class="sig-space"></div>
    <div style="font-weight:700;text-decoration:underline;">{sign_name}</div>
    <div>{sign_title}</div>
  </div></div>
  <div class="footer-note">Dokumen ini adalah draf persiapan yang dihasilkan otomatis dari basis pengetahuan TradeGraph ({html.escape(self.kb.snapshot.kb_id)} v{html.escape(self.kb.snapshot.version)}). Verifikasi kembali kesesuaian data sebelum diajukan pada portal resmi ({html.escape(tpl.issuing_portal)}).</div>
</body></html>"""

    def generate_docx(
        self,
        template_code: str,
        field_values: Mapping[str, object],
        *,
        custom_title: str | None = None,
        requirement_reason: str | None = None,
    ) -> tuple[bytes, str]:
        """Generate a valid Microsoft Word (.docx) draft document strictly using stdlib zipfile + lxml."""
        tpl = self.get_template(template_code)
        title = custom_title or tpl.title
        citations = self._opt.resolve_citations(tpl.source_clauses)
        generated_at = datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB")
        hs_slug = _slug(str(field_values.get("hs_code") or "shipment"))
        filename = f"draft-{_slug(tpl.template_code)}-{hs_slug}.docx"

        doc_el = etree.Element(f"{W}document", nsmap={"w": W_NS})
        body = etree.SubElement(doc_el, f"{W}body")

        def el(tag, attrib=None, parent=None):
            node = etree.Element(f"{W}{tag}") if parent is None else etree.SubElement(parent, f"{W}{tag}")
            if attrib:
                for k, v in attrib.items():
                    node.set(f"{W}{k}", str(v))
            return node

        def add_p(
            text: str,
            *,
            size_pt: float = 10.5,
            bold: bool = False,
            italic: bool = False,
            align: str = "left",
            space_before: int = 0,
            space_after: int = 60,
            bottom_border: bool = False,
            parent=body,
        ):
            p = el("p", parent=parent)
            pPr = el("pPr", parent=p)
            el("widowControl", parent=pPr)
            if bottom_border:
                pBdr = el("pBdr", parent=pPr)
                el("bottom", {"val": "single", "sz": "8", "space": "4", "color": "000000"}, parent=pBdr)
            el("spacing", {"before": str(space_before), "after": str(space_after), "line": "260", "lineRule": "auto"}, parent=pPr)
            el("jc", {"val": align}, parent=pPr)
            if text:
                r = el("r", parent=p)
                rPr = el("rPr", parent=r)
                el("rFonts", {"ascii": "Times New Roman", "hAnsi": "Times New Roman", "cs": "Times New Roman"}, parent=rPr)
                if bold:
                    el("b", {"val": "1"}, parent=rPr)
                if italic:
                    el("i", {"val": "1"}, parent=rPr)
                el("color", {"val": "000000"}, parent=rPr)
                hp = str(int(round(size_pt * 2)))
                el("sz", {"val": hp}, parent=rPr)
                el("szCs", {"val": hp}, parent=rPr)
                t = el("t", parent=r)
                t.set(f"{{{XML_NS}}}space", "preserve")
                t.text = text
            return p

        def add_table(rows: list[list[str]], col_widths: list[int], *, header_row: bool = False):
            tbl = el("tbl", parent=body)
            tblPr = el("tblPr", parent=tbl)
            el("tblW", {"w": str(sum(col_widths)), "type": "dxa"}, parent=tblPr)
            el("jc", {"val": "center"}, parent=tblPr)
            borders = el("tblBorders", parent=tblPr)
            for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                el(side, {"val": "single", "sz": "4", "space": "0", "color": "000000"}, parent=borders)
            mar = el("tblCellMar", parent=tblPr)
            for side, val in (("top", "60"), ("bottom", "60"), ("left", "100"), ("right", "100")):
                el(side, {"w": val, "type": "dxa"}, parent=mar)
            grid = el("tblGrid", parent=tbl)
            for w in col_widths:
                el("gridCol", {"w": str(w)}, parent=grid)

            for r_idx, row in enumerate(rows):
                tr = el("tr", parent=tbl)
                trPr = el("trPr", parent=tr)
                el("cantSplit", parent=trPr)
                is_hdr = header_row and r_idx == 0
                if is_hdr:
                    el("tblHeader", parent=trPr)
                for c_idx, cell_text in enumerate(row):
                    tc = el("tc", parent=tr)
                    tcPr = el("tcPr", parent=tc)
                    el("tcW", {"w": str(col_widths[c_idx]), "type": "dxa"}, parent=tcPr)
                    if is_hdr or (not header_row and c_idx == 0):
                        el("shd", {"val": "clear", "color": "auto", "fill": "F2F4F7"}, parent=tcPr)
                    el("vAlign", {"val": "top"}, parent=tcPr)
                    lines = str(cell_text).splitlines() or [""]
                    for line in lines:
                        add_p(
                            line,
                            size_pt=10,
                            bold=(is_hdr or (not header_row and c_idx == 0)),
                            align="left",
                            space_before=0,
                            space_after=20,
                            parent=tc,
                        )

        # Header
        add_p(
            f"DRAFT PERSIAPAN DOKUMEN KEPABEANAN — TRADEGRAPH KB v{self.kb.snapshot.version}",
            size_pt=9,
            bold=True,
            align="center",
            space_after=40,
        )
        add_p(title.upper(), size_pt=13.5, bold=True, align="center", space_after=40)
        add_p(tpl.subtitle, size_pt=10, italic=True, align="center", space_after=100, bottom_border=True)

        add_table(
            [
                ["Kode Formulir", tpl.form_number],
                ["Portal / Instansi Tujuan", tpl.issuing_portal],
                ["Tanggal Pembuatan Draf", generated_at],
            ] + ([["Dasar Kebutuhan", requirement_reason]] if requirement_reason else []),
            [2900, 6460],
            header_row=False,
        )

        # Sections & user-filled / prefilled fields
        sections: list[tuple[str, list[DocumentFieldSpec]]] = []
        sec_map: dict[str, list[DocumentFieldSpec]] = {}
        for f in tpl.fields:
            if f.section not in sec_map:
                sec_map[f.section] = []
                sections.append((f.section, sec_map[f.section]))
            sec_map[f.section].append(f)

        for sec_name, fields in sections:
            add_p(sec_name, size_pt=11, bold=True, space_before=160, space_after=60)
            sec_rows: list[list[str]] = []
            for f in fields:
                raw_val = str(field_values.get(f.key) or "").strip()
                lbl = f"{f.label} *" if f.required else f.label
                sec_rows.append([lbl, raw_val if raw_val else "(Belum diisi)"])
            add_table(sec_rows, [3400, 5960], header_row=False)

        # Legal basis table
        if citations:
            add_p("DASAR HUKUM & RUJUKAN PASAL (KB TRADEGRAPH)", size_pt=11, bold=True, space_before=160, space_after=60)
            cit_rows = [["Peraturan Resmi", "Pasal / Lampiran"]]
            for c in citations:
                cit_rows.append([c.regulation_number or c.document_label or c.clause_id, c.clause_locator or c.clause_id])
            add_table(cit_rows, [4800, 4560], header_row=True)

        # Signature block
        sign_date = str(field_values.get("sign_place_date") or datetime.now(WIB).strftime("Jakarta, %Y-%m-%d"))
        company = str(field_values.get("company_name") or "")
        sign_name = str(field_values.get("signatory_name") or "........................................")
        sign_title = str(field_values.get("signatory_title") or "Pejabat Berwenang")
        add_p(sign_date, size_pt=10.5, align="right", space_before=240, space_after=20)
        if company:
            add_p(company, size_pt=10.5, bold=True, align="right", space_after=20)
        add_p("", size_pt=10.5, align="right", space_after=420)
        add_p(sign_name, size_pt=10.5, bold=True, align="right", space_after=20)
        add_p(sign_title, size_pt=10, align="right", space_after=140)

        add_p(
            f"Catatan: Draf ini dihasilkan otomatis dari TradeGraph ({self.kb.snapshot.kb_id} v{self.kb.snapshot.version}). "
            f"Periksa kembali seluruh isian sebelum diajukan ke {tpl.issuing_portal}.",
            size_pt=8.5,
            italic=True,
            align="left",
            space_before=80,
            space_after=0,
        )

        # Page setup (A4 / Letter portrait, 1 inch margins -> content width 9360 twips)
        sectPr = el("sectPr", parent=body)
        el("pgSz", {"w": "12240", "h": "15840", "orient": "portrait"}, parent=sectPr)
        el("pgMar", {"top": "1440", "right": "1440", "bottom": "1440", "left": "1440", "header": "720", "footer": "720"}, parent=sectPr)

        doc_xml = etree.tostring(doc_el, xml_declaration=True, encoding="UTF-8", standalone="yes")
        content_types = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        ).encode("utf-8")
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>"
        ).encode("utf-8")
        doc_rels_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
        ).encode("utf-8")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            zout.writestr("[Content_Types].xml", content_types)
            zout.writestr("_rels/.rels", rels_xml)
            zout.writestr("word/_rels/document.xml.rels", doc_rels_xml)
            zout.writestr("word/document.xml", doc_xml)
        return buf.getvalue(), filename

    def generate_bundle_zip(
        self,
        items: list[Mapping[str, object]],
    ) -> bytes:
        """Generate a ZIP archive containing filled .docx drafts for multiple required documents."""
        buf = io.BytesIO()
        seen_names: dict[str, int] = {}
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in items:
                code = str(item["template_code"])
                fields = item.get("fields") if isinstance(item.get("fields"), Mapping) else {}
                custom_title = str(item["custom_title"]) if item.get("custom_title") else None
                reason = str(item["requirement_reason"]) if item.get("requirement_reason") else None
                docx_bytes, filename = self.generate_docx(
                    code,
                    fields,
                    custom_title=custom_title,
                    requirement_reason=reason,
                )
                count = seen_names.get(filename, 0) + 1
                seen_names[filename] = count
                if count > 1:
                    stem = filename.removesuffix(".docx")
                    filename = f"{stem}-{count}.docx"
                zout.writestr(filename, docx_bytes)
        return buf.getvalue()
