"""Bounded multi-chapter batch ingestion across primary Kemenkeu tariffs, ATIGA/RCEP PSRs, and Kemendag Lartas PDFs."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
import re

from pypdf import PdfReader

from tradegraph.sources import parsers
from tradegraph.sources.rdf_staging import generate_staging_turtle


ASEAN_EXPORTERS: tuple[str, ...] = (
    "brunei", "cambodia", "laos", "malaysia", "myanmar", "philippines", "singapore", "thailand", "vietnam",
)

# Kemendag 2025 sectoral Lartas PDFs and the HS chapters they govern (bounded search).
_LARTAS_SOURCES: tuple[tuple[str, str, str, tuple[str, ...], bool], ...] = (
    ("ID-LARTAS-TEKSTIL", "Tekstil dan Produk Tekstil", "2025-08-29", ("50", "51", "52", "53", "54", "55", "56", "57", "58", "59", "60", "61", "62", "63"), False),
    ("ID-LARTAS-PERTANIAN", "Barang Pertanian dan Peternakan", "2025-08-29", ("01", "02", "03", "04", "07", "08", "09", "10", "11", "12", "15", "17", "23", "24"), False),
    ("ID-LARTAS-ELEKTRONIK", "Barang Elektronik dan Telematika", "2025-08-29", ("84", "85", "90"), False),
    ("ID-LARTAS-INDUSTRI", "Barang Industri Tertentu (Besi/Baja dan Turunannya)", "2025-08-29", ("28", "29", "39", "40", "48", "68", "69", "70", "72", "73", "74", "76"), False),
    ("ID-LARTAS-LARANGAN", "Barang Dilarang Impor", "2026-01-01", ("29", "38", "40", "63", "84", "85"), True),
)


@dataclass(frozen=True, slots=True)
class BatchIngestReport:
    hs8_codes: tuple[str, ...]
    tariffs_verified: int
    tariffs_flagged: int
    psrs_verified: int
    psrs_flagged: int
    rcep_appendix_hits: int
    lartas_verified: int
    lartas_flagged: int
    per_source_counts: dict[str, int]
    jsonl_outputs: dict[str, str]
    staging_ttl: str | None = None
    triples_generated: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _locate_lartas_pages(pdf_path: Path, target_hs8_set: set[str], *, start_page: int = 8) -> dict[str, int]:
    """Scan only text for target HS8 strings inside a sectoral Kemendag Lartas PDF (100-200p)."""
    reader = PdfReader(str(pdf_path))
    found: dict[str, int] = {}
    remaining = set(target_hs8_set)
    for page_no in range(start_page, len(reader.pages) + 1):
        if not remaining:
            break
        txt = reader.pages[page_no - 1].extract_text() or ""
        for hs8 in list(remaining):
            if hs8 in txt:
                found[hs8] = page_no
                remaining.discard(hs8)
    return found


def discover_hs8_from_mfn(
    manifest_path: Path,
    prefixes: Iterable[str],
    *,
    max_hs: int = 20,
) -> tuple[str, ...]:
    """Discover up to max_hs real BTKI 2022 HS8 codes matching the requested HS2/HS4/HS6 prefixes."""
    if max_hs < 1:
        raise ValueError("max_hs must be >= 1.")
    mfn_pdf, _ = parsers.resolve_manifest_source(manifest_path, "ID-TARIF-MFN")
    reader = PdfReader(str(mfn_pdf))
    hs_re = re.compile(r"\b(\d{4}\.\d{2}\.\d{2})\b")
    discovered: list[str] = []
    seen: set[str] = set()

    for raw_pref in prefixes:
        if len(discovered) >= max_hs:
            break
        clean = re.sub(r"\D+", "", raw_pref)
        if not clean:
            continue
        target8 = clean[:8].ljust(8, "0")
        lo, hi = 8, len(reader.pages)
        start_hit: int | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            txt = reader.pages[mid - 1].extract_text() or ""
            codes = [m.replace(".", "") for m in hs_re.findall(txt)]
            if not codes:
                hi = mid - 1
                continue
            if any(c.startswith(clean) for c in codes):
                start_hit = mid
                break
            if target8 < min(codes):
                hi = mid - 1
            else:
                lo = mid + 1
        if start_hit is None:
            continue
        # Walk backwards up to 3 pages to catch start of prefix, then collect up to max_hs
        scan_pg = max(8, start_hit - 2)
        while scan_pg <= len(reader.pages) and len(discovered) < max_hs:
            txt = reader.pages[scan_pg - 1].extract_text() or ""
            matches = [m for m in hs_re.findall(txt) if m.replace(".", "").startswith(clean)]
            for code in matches:
                if code not in seen:
                    seen.add(code)
                    discovered.append(code)
                    if len(discovered) >= max_hs:
                        break
            if matches and scan_pg >= start_hit:
                # Continue while prefix still appears
                scan_pg += 1
            elif scan_pg < start_hit:
                scan_pg += 1
            else:
                break
    return tuple(discovered[:max_hs])


def ingest_hs_batch(
    manifest_path: Path,
    *,
    hs8_codes: Iterable[str] = (),
    hs_prefixes: Iterable[str] = (),
    max_hs: int = 20,
    output_dir: Path,
    include_agreements: tuple[str, ...] = ("MFN", "ATIGA", "ACFTA", "IJEPA", "IKCEPA", "IACEPA", "RCEP"),
    include_psrs: bool = True,
    include_lartas: bool = True,
    emit_ttl: Path | None = None,
    canonical_kb: Path | None = None,
) -> BatchIngestReport:
    """Extract, cross-check, and optionally stage a bounded set of HS8 codes across primary trade PDFs."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in hs8_codes:
        norm = parsers._normalize_ocr_hs8(raw)
        if re.match(r"^\d{4}\.\d{2}\.\d{2}$", norm) and norm not in seen:
            seen.add(norm)
            normalized.append(norm)
    if hs_prefixes and len(normalized) < max_hs:
        for code in discover_hs8_from_mfn(manifest_path, hs_prefixes, max_hs=max_hs - len(normalized)):
            if code not in seen:
                seen.add(code)
                normalized.append(code)
    if not normalized:
        raise ValueError("Provide at least one valid HS8 code or HS prefix.")
    if len(normalized) > max_hs:
        normalized = normalized[:max_hs]

    target_set = set(normalized)
    target_hs6 = {c[:7] for c in target_set}
    target_hs4 = {f"{c[:2]}.{c[2:4]}" for c in target_set}
    target_chaps = {c[:2] for c in target_set}
    agr_set = {a.upper() for a in include_agreements}

    tariff_records: list[parsers.TariffRecord] = []
    psr_records: list[parsers.PsrRecord] = []
    appendix_records: list[dict] = []
    lartas_records: list[parsers.LartasRecord] = []
    per_source: dict[str, int] = {}

    # 1. Single-rate / 2-column PMK tariff schedules (MFN, ATIGA, ACFTA, IJEPA)
    flat_specs: list[tuple[str, str, tuple[str, ...], str]] = []
    if "MFN" in agr_set:
        flat_specs.append(("ID-TARIF-MFN", "MFN", (), "2022-04-01"))
    if "ATIGA" in agr_set:
        flat_specs.append(("ID-TARIF-ATIGA", "ATIGA", (), "2022-04-01"))
    if "ACFTA" in agr_set:
        flat_specs.append(("ID-TARIF-ACFTA", "ACFTA", (), "2022-04-01"))
    if "IJEPA" in agr_set:
        flat_specs.append(("ID-TARIF-IJEPA", "IJEPA", ("japan",), "2022-04-01"))

    for key, agr, partners, eff in flat_specs:
        pdf, meta = parsers.resolve_manifest_source(manifest_path, key)
        located = parsers.locate_hs8_pages(pdf, target_set)
        pages = sorted(set(located.values()))
        hits: list[parsers.TariffRecord] = []
        for pg in pages:
            rows = parsers.parse_pmk_tariff_pdf(
                pdf, source_meta=meta, start_page=pg, end_page=pg,
                agreement=agr, partner_countries=partners, effective_from=eff,
            )
            hits.extend(r for r in rows if r.hs8 in target_set)
        # Deduplicate by hs8 keeping first verified match
        dedup: dict[str, parsers.TariffRecord] = {}
        for r in hits:
            dedup.setdefault(r.hs8, r)
        tariff_records.extend(dedup.values())
        per_source[key] = len(dedup)

    # 2. Multi-year staged PMK schedules (IK-CEPA, IA-CEPA, RCEP ASEAN/CN/JP/AU/KR)
    if "IKCEPA" in agr_set:
        pdf, meta = parsers.resolve_manifest_source(manifest_path, "ID-TARIF-IKCEPA")
        located = parsers.locate_hs8_pages(pdf, target_set, start_page=7)
        hits_ik: list[parsers.TariffRecord] = []
        for pg in sorted(set(located.values())):
            hits_ik.extend(parsers.parse_ikcepa_staged_tariff_pdf(
                pdf, source_meta=meta, start_page=pg, end_page=pg, hs8_filter=target_set,
            ))
        tariff_records.extend(hits_ik)
        per_source["ID-TARIF-IKCEPA"] = len(hits_ik)

    if "IACEPA" in agr_set:
        pdf, meta = parsers.resolve_manifest_source(manifest_path, "ID-TARIF-IACEPA")
        located = parsers.locate_hs8_pages(pdf, target_set, start_page=10)
        hits_ia: list[parsers.TariffRecord] = []
        for pg in sorted(set(located.values())):
            hits_ia.extend(parsers.parse_iacepa_staged_tariff_pdf(
                pdf, source_meta=meta, start_page=pg, end_page=pg, hs8_filter=target_set,
            ))
        tariff_records.extend(hits_ia)
        per_source["ID-TARIF-IACEPA"] = len(hits_ia)

    if "RCEP" in agr_set:
        rcep_specs = (
            ("ID-TARIF-RCEP-ASEAN", ASEAN_EXPORTERS),
            ("ID-TARIF-RCEP-CN", ("china",)),
            ("ID-TARIF-RCEP-JP", ("japan",)),
            ("ID-TARIF-RCEP-AU", ("australia",)),
            ("ID-TARIF-RCEP-KR", ("southKorea",)),
        )
        for key, partners in rcep_specs:
            pdf, meta = parsers.resolve_manifest_source(manifest_path, key)
            located = parsers.locate_hs8_pages(pdf, target_set, start_page=7)
            hits_r: list[parsers.TariffRecord] = []
            for pg in sorted(set(located.values())):
                hits_r.extend(parsers.parse_rcep_staged_tariff_pdf(
                    pdf, source_meta=meta, partner_countries=partners,
                    start_page=pg, end_page=pg, hs8_filter=target_set,
                ))
            tariff_records.extend(hits_r)
            per_source[key] = len(hits_r)

        # Check RCEP Lampiran II (DV20 Appendix) on ID-TARIF-RCEP-ASEAN pages 598..620
        asean_pdf, asean_meta = parsers.resolve_manifest_source(manifest_path, "ID-TARIF-RCEP-ASEAN")
        app_rows = parsers.parse_rcep_appendix_pdf(asean_pdf, source_meta=asean_meta, start_page=596, end_page=620)
        matched_app = [r for r in app_rows if r["hs8"] in target_set]
        appendix_records.extend(matched_app)
        per_source["ID-RCEP-DV20-APPENDIX"] = len(matched_app)

    # 3. ATIGA & RCEP PSRs for target HS6 / HS4 / HS2 codes
    if include_psrs:
        atiga_pdf, atiga_meta = parsers.resolve_manifest_source(manifest_path, "ATIGA-PSR22")
        atiga_total = len(PdfReader(str(atiga_pdf)).pages)
        atiga_pages = sorted({
            cand
            for pg in parsers.locate_psr_pages(atiga_pdf, target_hs6).values()
            for cand in (pg - 1, pg, pg + 1)
            if 1 <= cand <= atiga_total
        })
        hits_ap: dict[str, parsers.PsrRecord] = {}
        for pg in atiga_pages:
            rows = parsers.parse_atiga_psr_pdf(atiga_pdf, source_meta=atiga_meta, start_page=pg, end_page=pg)
            for r in rows:
                if r.hs6 in target_hs6:
                    hits_ap.setdefault(r.hs6, r)
        psr_records.extend(hits_ap.values())
        per_source["ATIGA-PSR22"] = len(hits_ap)

        rcep_pdf, rcep_meta = parsers.resolve_manifest_source(manifest_path, "RCEP-PSR22")
        rcep_total = len(PdfReader(str(rcep_pdf)).pages)
        rcep_pages = sorted({
            cand
            for pg in parsers.locate_psr_pages(rcep_pdf, target_hs6 | target_hs4).values()
            for cand in (pg - 1, pg, pg + 1)
            if 1 <= cand <= rcep_total
        })
        dedup_rp: dict[str, parsers.PsrRecord] = {}
        for pg in rcep_pages:
            rows = parsers.parse_rcep_psr_pdf(rcep_pdf, source_meta=rcep_meta, start_page=pg, end_page=pg)
            for r in rows:
                if r.hs_code in target_hs6 or r.hs_code in target_hs4 or r.hs_code in target_chaps:
                    dedup_rp.setdefault(r.hs_code, r)
        psr_records.extend(dedup_rp.values())
        per_source["RCEP-PSR22"] = len(dedup_rp)

    # 4. Kemendag 2025 Sectoral Lartas & Prohibited PDFs
    if include_lartas:
        for key, group, eff, chaps, is_proh in _LARTAS_SOURCES:
            relevant = {c for c in target_set if c[:2] in chaps}
            if not relevant:
                continue
            pdf, meta = parsers.resolve_manifest_source(manifest_path, key)
            located = _locate_lartas_pages(pdf, relevant, start_page=6 if is_proh else 10)
            hits_l: list[parsers.LartasRecord] = []
            for pg in sorted(set(located.values())):
                if is_proh:
                    hits_l.extend(parsers.parse_permendag_prohibited_pdf(
                        pdf, source_meta=meta, commodity_group=group,
                        start_page=pg, end_page=pg, effective_from=eff, hs8_filter=relevant,
                    ))
                else:
                    hits_l.extend(parsers.parse_permendag_lartas_pdf(
                        pdf, source_meta=meta, commodity_group=group,
                        start_page=pg, end_page=pg, effective_from=eff, hs8_filter=relevant,
                    ))
            dedup_l: dict[str, parsers.LartasRecord] = {}
            for r in hits_l:
                dedup_l.setdefault(r.hs8, r)
            lartas_records.extend(dedup_l.values())
            per_source[key] = len(dedup_l)

    # Cross-check EVERY record before writing staging JSONL
    chk_tariffs = parsers.verify_records(tariff_records)
    chk_psrs = parsers.verify_records(psr_records)
    chk_lartas = parsers.verify_records(lartas_records)

    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_outputs: dict[str, str] = {}
    tariffs_path = output_dir / "batch_tariffs.jsonl"
    parsers.write_jsonl(tariffs_path, chk_tariffs.verified)
    jsonl_outputs["tariffs"] = str(tariffs_path)

    if chk_psrs.verified:
        psrs_path = output_dir / "batch_psrs.jsonl"
        parsers.write_jsonl(psrs_path, chk_psrs.verified)
        jsonl_outputs["psrs"] = str(psrs_path)
    if appendix_records:
        app_path = output_dir / "batch_rcep_appendix.jsonl"
        parsers.write_jsonl(app_path, appendix_records)
        jsonl_outputs["rcep_appendix"] = str(app_path)
    if chk_lartas.verified:
        lartas_path = output_dir / "batch_lartas.jsonl"
        parsers.write_jsonl(lartas_path, chk_lartas.verified)
        jsonl_outputs["lartas"] = str(lartas_path)

    triples_gen = 0
    ttl_out_str: str | None = None
    if emit_ttl is not None:
        all_verified = list(chk_psrs.verified) + list(chk_tariffs.verified) + list(appendix_records) + list(chk_lartas.verified)
        turtle_text, summary = generate_staging_turtle(
            all_verified,
            canonical_kb_path=canonical_kb,
        )
        emit_ttl.parent.mkdir(parents=True, exist_ok=True)
        emit_ttl.write_text(turtle_text, encoding="utf-8")
        triples_gen = summary.triples_generated
        ttl_out_str = str(emit_ttl)

    return BatchIngestReport(
        hs8_codes=tuple(normalized),
        tariffs_verified=len(chk_tariffs.verified),
        tariffs_flagged=len(chk_tariffs.flagged),
        psrs_verified=len(chk_psrs.verified),
        psrs_flagged=len(chk_psrs.flagged),
        rcep_appendix_hits=len(appendix_records),
        lartas_verified=len(chk_lartas.verified),
        lartas_flagged=len(chk_lartas.flagged),
        per_source_counts=per_source,
        jsonl_outputs=jsonl_outputs,
        staging_ttl=ttl_out_str,
        triples_generated=triples_gen,
    )
