"""Structured parsers for archived trade regulations, tariff/PSR tables, and web guidance."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
from typing import Iterable, Literal

from bs4 import BeautifulSoup
from pypdf import PdfReader


@dataclass(frozen=True, slots=True)
class SourceMeta:
    source_key: str
    source_id: str
    content_hash: str
    local_ref: str
    title: str | None = None
    url: str | None = None
    authority: str | None = None


@dataclass(frozen=True, slots=True)
class PsrRecord:
    source_key: str
    source_id: str
    content_hash: str
    row_no: int
    hs6: str
    hs6_digits: str
    description: str
    concise_rule: str
    full_rule: str
    criteria_alternatives: tuple[tuple[dict[str, str], ...], ...]
    page: int
    locator: str
    record_kind: Literal["psr"] = "psr"
    agreement: str = "ATIGA"
    hs_level: Literal["SUBHEADING", "HEADING", "CHAPTER"] = "SUBHEADING"
    hs_code: str = ""
    hs_digits: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TariffRecord:
    source_key: str
    source_id: str
    content_hash: str
    row_no: int
    hs8: str
    hs8_digits: str
    hs6: str
    hs6_digits: str
    heading_4: str
    description_id: str
    description_en: str
    tariff_rate_raw: str | None
    tariff_rate_percent: str | None
    page: int
    locator: str
    record_kind: Literal["tariff"] = "tariff"
    agreement: str = "ATIGA"
    partner_countries: tuple[str, ...] = ()
    effective_from: str | None = None
    effective_until_exclusive: str | None = None
    mfn_excluded: bool = False
    subject_to_rcep_dv20: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LegalClauseRecord:
    source_key: str
    source_id: str
    content_hash: str
    bab: str | None
    bab_title: str | None
    bagian: str | None
    pasal: str
    text: str
    ayat: tuple[dict[str, str], ...]
    cross_references: tuple[str, ...]
    page_start: int
    page_end: int
    locator: str
    record_kind: Literal["statute_clause"] = "statute_clause"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GuidanceSectionRecord:
    source_key: str
    source_id: str
    content_hash: str
    heading: str
    heading_level: int
    text: str
    links: tuple[dict[str, str], ...]
    locator: str
    record_kind: Literal["guidance_section"] = "guidance_section"

    def to_dict(self) -> dict:
        return asdict(self)


def load_manifest(manifest_path: Path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def resolve_manifest_source(manifest_path: Path, source_key: str) -> tuple[Path, SourceMeta]:
    manifest = load_manifest(manifest_path)
    repo_root = manifest_path.resolve().parent.parent
    for entry in manifest.get("sources", []):
        if entry.get("key") == source_key:
            path = (repo_root / entry["local_ref"]).resolve()
            meta = SourceMeta(
                source_key=entry["key"],
                source_id=entry["id"],
                content_hash=entry["content_hash"],
                local_ref=entry["local_ref"],
                title=entry.get("title"),
                url=entry.get("url"),
                authority=entry.get("authority"),
            )
            return path, meta
    raise KeyError(f"Unknown source key in {manifest_path}: {source_key}")


def build_adhoc_source_meta(path: Path, *, source_key: str, url: str | None = None, title: str | None = None) -> SourceMeta:
    with path.open("rb") as stream:
        digest = file_digest(stream, "sha256").hexdigest()
    version = sha256(f"{url or path.name}\0{digest}".encode()).hexdigest()
    return SourceMeta(
        source_key=source_key,
        source_id=f"source-{version}",
        content_hash=digest,
        local_ref=path.as_posix(),
        title=title,
        url=url,
    )


def _page_bounds(total_pages: int, start_page: int, end_page: int | None) -> tuple[int, int]:
    if start_page < 1:
        raise ValueError("start_page must be >= 1")
    upper = total_pages if end_page is None else min(end_page, total_pages)
    if upper < start_page:
        return start_page, start_page - 1
    return start_page, upper


# --- 1. ATIGA Annex 3 Product Specific Rules (PSR) parser ---

_PSR_ANCHOR_RE = re.compile(r"^(\d{1,5})\s+(\d{4}\.\d{2})\b(.*)$")
_PSR_FULL_START_RE = re.compile(
    r"\b("
    r"Wholly obtained|A regional value content|Regional value content|"
    r"A change to|Change to|Produced from|Manufactured from|"
    r"Chemical reaction|Process Rules|No required change"
    r")\b"
)
_PSR_HS_RANGE = r"\d{2,4}(?:\.\d{2})?(?:\s*-\s*\d{2,4}(?:\.\d{2})?)?"
_PSR_EXCLUSION = rf"(?:except|exc\.?)\s+(?:from\s+)?{_PSR_HS_RANGE}(?:(?:\s*,\s*|\s+and\s+){_PSR_HS_RANGE})*"
_PSR_CONCISE_TOKEN = (
    rf"(?:WO|RVC\d+(?:\([^)]*\))?(?:\s*\+\s*(?:CC|CTH|CTSH)(?:\s+{_PSR_EXCLUSION})?)?|"
    rf"(?:CC|CTH|CTSH)(?:\s+{_PSR_EXCLUSION})?|SP|"
    r"cut\s+and\s+sewn\s+in\s+any\s+AMS|"
    r"textile\s+process\s+rule|chemical\s+reaction\s+rule|process\s+rule)"
)
_PSR_CONCISE_TAIL_RE = re.compile(
    rf"(?P<rule>{_PSR_CONCISE_TOKEN}(?:\s+(?:or|and|\+|with)\s+{_PSR_CONCISE_TOKEN})*)\s*$",
    re.I,
)


def expand_hs_exclusions(raw_exclusion: str) -> str:
    """Expand numeric HS chapter/heading ranges (e.g. '7208-7211' or '72.08 to 72.11') into explicit codes."""
    # Normalize dotted 4-digit headings (e.g. '72.08' -> '7208') while preserving 6-digit subheadings ('1211.20').
    normalized = re.sub(r"\b(\d{2})\.(\d{2})\b(?!\.\d{2})", r"\1\2", raw_exclusion)
    codes: list[str] = []
    for match in re.finditer(
        r"(\d{2,4}(?:\.\d{2})?)(?:(?:\s*-\s*|\s+to\s+)(\d{2,4}(?:\.\d{2})?))?",
        normalized,
        re.I,
    ):
        start_tok, end_tok = match.group(1), match.group(2)
        if end_tok and "." not in start_tok and "." not in end_tok and len(start_tok) == len(end_tok):
            lo, hi = int(start_tok), int(end_tok)
            if 0 < hi - lo <= 50:
                width = len(start_tok)
                codes.extend(f"{n:0{width}d}" for n in range(lo, hi + 1))
                continue
        codes.append(start_tok)
        if end_tok:
            codes.append(end_tok)
    return ", ".join(dict.fromkeys(codes)) if codes else raw_exclusion.strip()


def _parse_single_psr_conjunct(token: str) -> dict[str, str]:
    cleaned = token.strip().rstrip(",;.")
    upper = cleaned.upper()
    if upper == "WO":
        return {"kind": "WO", "raw": cleaned}
    rvc_match = re.match(r"^(?:RVC|QVC)\s*\(?(\d+(?:\.\d+)?)\)?$", upper)
    if rvc_match:
        return {"kind": "RVC", "threshold_percent": rvc_match.group(1), "raw": cleaned}
    exc_match = re.match(r"^(CC|CTH|CTSH)\s+(?:except|exc\.?)\s+(?:from\s+)?(.+)$", cleaned, re.I)
    if exc_match:
        return {
            "kind": exc_match.group(1).upper(),
            "except_from": expand_hs_exclusions(exc_match.group(2)),
            "raw": cleaned,
        }
    if upper in {"CC", "CTH", "CTSH"}:
        return {"kind": upper, "raw": cleaned}
    if upper == "CR" or "CHEMICAL" in upper:
        return {"kind": "CHEMICAL_REACTION", "raw": cleaned}
    if "CUT AND SEWN" in upper:
        return {"kind": "CUT_AND_SEWN_AMS", "raw": cleaned}
    if "REFINING" in upper:
        return {"kind": "REFINING_PROCESS", "raw": cleaned}
    if "TEXTILE" in upper:
        return {"kind": "TEXTILE_PROCESS", "raw": cleaned}
    return {"kind": "OTHER", "raw": cleaned}


_PSR_ALT_SPLIT_RE = re.compile(
    r"(?:\s*;\s*(?:or\s+)?|(?:\s*,\s*|\s+)or\s+(?=(?:WO\b|(?:RVC|QVC)\s*\(?\d|CC\b|CTH\b|CTSH\b|CR\b|SP\b|cut\s+and\s+sewn\b|textile\b|chemical\b|refining\b|process\b)))",
    re.I,
)


def parse_psr_criteria_tokens(concise_rule: str) -> tuple[tuple[dict[str, str], ...], ...]:
    """Decompose a concise PSR expression into disjunctive alternatives of conjunct tokens."""
    cleaned = re.sub(r"\s+", " ", concise_rule.strip())
    if not cleaned:
        return ()
    alternatives: list[tuple[dict[str, str], ...]] = []
    for alt in _PSR_ALT_SPLIT_RE.split(cleaned):
        alt_clean = alt.strip().rstrip(",;.")
        if not alt_clean:
            continue
        # Protect phrases containing 'and' that must stay atomic before conjunct splitting.
        protected = re.sub(r"\bcut\s+and\s+sewn\s+in\s+any\s+AMS\b", "__CUT_AND_SEWN_AMS__", alt_clean, flags=re.I)
        # Keep tariff-shift exclusions (e.g. "CTH except from 7208 and 7211" or "CTH except from heading 72.08 or 72.09") intact.
        if re.match(r"^(?:CC|CTH|CTSH)\s+(?:except|exc\.?)\s+", protected, re.I) and "+" not in protected and "provided" not in protected.lower():
            alternatives.append((_parse_single_psr_conjunct(alt_clean),))
            continue

        conjuncts: list[dict[str, str]] = []
        # Split first on '+' or 'provided there is' (e.g. "RVC35+CTSH except from 8541.43", "CC provided there is QVC40").
        for plus_part in re.split(r"\s*\+\s*|\s+provided\s+(?:there\s+is\s+)?", protected, flags=re.I):
            if re.match(r"^(?:CC|CTH|CTSH)\s+(?:except|exc\.?)\s+", plus_part.strip(), re.I):
                conjuncts.append(_parse_single_psr_conjunct(plus_part.replace("__CUT_AND_SEWN_AMS__", "cut and sewn in any AMS")))
                continue
            for part in re.split(r"\s+and\s+", plus_part.strip(), flags=re.I):
                restored = part.replace("__CUT_AND_SEWN_AMS__", "cut and sewn in any AMS").strip()
                if restored:
                    conjuncts.append(_parse_single_psr_conjunct(restored))
        if conjuncts:
            alternatives.append(tuple(conjuncts))
    return tuple(alternatives)


def _split_psr_merged_fields(desc_text: str, concise_text: str, full_text: str) -> tuple[str, str, str]:
    desc = re.sub(r"\s+", " ", desc_text).strip()
    concise = re.sub(r"\s+", " ", concise_text).strip()
    full = re.sub(r"\s+", " ", full_text).strip()

    if not full and concise:
        full_match = _PSR_FULL_START_RE.search(concise)
        if full_match:
            full = concise[full_match.start() :].strip()
            concise = concise[: full_match.start()].strip()

    if not full:
        full_match = _PSR_FULL_START_RE.search(desc)
        if full_match:
            full = desc[full_match.start() :].strip()
            desc = desc[: full_match.start()].strip()

    if not concise:
        tail_match = _PSR_CONCISE_TAIL_RE.search(desc)
        if tail_match:
            concise = tail_match.group("rule").strip()
            desc = desc[: tail_match.start()].strip()

    return desc, concise, full


def parse_atiga_psr_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    start_page: int = 1,
    end_page: int | None = None,
    max_records: int | None = None,
) -> list[PsrRecord]:
    """Parse ATIGA Annex 3 (HS 2022) Product Specific Rules table from PDF."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    records: list[PsrRecord] = []

    for page_no in range(first, last + 1):
        items: list[tuple[float, float, str]] = []

        def visitor(text, cm, tm, font_dict, font_size):
            cleaned = text.strip()
            if cleaned:
                items.append((float(tm[5]), float(tm[4]), cleaned))

        reader.pages[page_no - 1].extract_text(visitor_text=visitor)

        current: dict | None = None

        def flush_current():
            nonlocal current
            if current is None:
                return
            desc, concise, full = _split_psr_merged_fields(
                " ".join(current["desc"]),
                " ".join(current["concise"]),
                " ".join(current["full"]),
            )
            hs6 = current["hs6"]
            row_no = current["row_no"]
            records.append(PsrRecord(
                source_key=source_meta.source_key,
                source_id=source_meta.source_id,
                content_hash=source_meta.content_hash,
                row_no=row_no,
                hs6=hs6,
                hs6_digits=hs6.replace(".", ""),
                description=desc,
                concise_rule=concise,
                full_rule=full,
                criteria_alternatives=parse_psr_criteria_tokens(concise),
                page=page_no,
                locator=f"Annex 3 HS 2022, baris {row_no} ({hs6})",
                agreement="ATIGA",
                hs_level="SUBHEADING",
                hs_code=hs6,
                hs_digits=hs6.replace(".", ""),
            ))
            current = None

        for _y, x, text in items:
            if text in {"ANNEX 3", "PRODUCT SPECIFIC RULES", "(concise)", "(full)"} or text.startswith("No. HS 2022"):
                continue
            if x < 120.0:
                anchor = _PSR_ANCHOR_RE.match(text)
                if anchor:
                    flush_current()
                    if max_records is not None and len(records) >= max_records:
                        return records
                    current = {
                        "row_no": int(anchor.group(1)),
                        "hs6": anchor.group(2),
                        "desc": [anchor.group(3).strip()] if anchor.group(3).strip() else [],
                        "concise": [],
                        "full": [],
                    }
                    continue
            if current is None:
                continue
            if x < 340.0:
                current["desc"].append(text)
            elif x < 460.0:
                current["concise"].append(text)
            else:
                current["full"].append(text)

        flush_current()
        if max_records is not None and len(records) >= max_records:
            return records[:max_records]

    return records


# --- 1b. RCEP Annex 3A (HS 2022) Product-Specific Rules parser ---

_RCEP_CHAPTER_RE = re.compile(r"^\d{2}$")
_RCEP_HEADING_RE = re.compile(r"^\d{2}\.\d{2}$")
_RCEP_SUBHEADING_RE = re.compile(r"^\d{4}\.\d{2}$")


def _join_pdf_fragments(parts: list[str]) -> str:
    joined = " ".join(parts)
    joined = re.sub(r"\s*-\s*(?=[a-zA-Z])", "-", joined)
    joined = re.sub(r"\s+([,.;:)])", r"\1", joined)
    joined = re.sub(r"([(])\s+", r"\1", joined)
    return re.sub(r"\s+", " ", joined).strip()


def parse_rcep_psr_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    start_page: int = 1,
    end_page: int | None = None,
    max_records: int | None = None,
) -> list[PsrRecord]:
    """Parse RCEP Annex 3A (HS 2022) 5-column Product-Specific Rules table (Chapter/Heading/Subheading)."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    records: list[PsrRecord] = []
    seq = 0

    for page_no in range(first, last + 1):
        items: list[tuple[float, float, str]] = []

        def visitor(text, cm, tm, font_dict, font_size):
            cleaned = text.strip()
            if cleaned:
                items.append((float(tm[5]), float(tm[4]), cleaned))

        reader.pages[page_no - 1].extract_text(visitor_text=visitor)

        # Page 1 intro sits above y=555; subsequent pages have column headers above y=695.
        table_top_y = 540.0 if page_no == 1 else 696.0
        current: dict | None = None

        def flush_current():
            nonlocal current, seq
            if current is None:
                return
            rule_text = _join_pdf_fragments(current["rule"])
            if not rule_text:
                current = None
                return
            desc_text = _join_pdf_fragments(current["desc"])
            if current["subheading"]:
                hs_level: Literal["SUBHEADING", "HEADING", "CHAPTER"] = "SUBHEADING"
                hs_code = current["subheading"]
                hs_digits = hs_code.replace(".", "")
                hs6, hs6_digits = hs_code, hs_digits
            elif current["heading"]:
                hs_level = "HEADING"
                hs_code = current["heading"]
                hs_digits = hs_code.replace(".", "")
                hs6, hs6_digits = "", ""
            elif current["chapter"]:
                hs_level = "CHAPTER"
                hs_code = current["chapter"]
                hs_digits = hs_code
                hs6, hs6_digits = "", ""
            else:
                current = None
                return

            seq += 1
            records.append(PsrRecord(
                source_key=source_meta.source_key,
                source_id=source_meta.source_id,
                content_hash=source_meta.content_hash,
                row_no=seq,
                hs6=hs6,
                hs6_digits=hs6_digits,
                description=desc_text,
                concise_rule=rule_text,
                full_rule="",
                criteria_alternatives=parse_psr_criteria_tokens(rule_text),
                page=page_no,
                locator=f"RCEP Annex 3A HS 2022, p.{page_no} ({hs_code})",
                agreement="RCEP",
                hs_level=hs_level,
                hs_code=hs_code,
                hs_digits=hs_digits,
            ))
            current = None

        for y, x, text in items:
            if y < 80.0 or y > table_top_y:
                continue
            if x < 125.0 and _RCEP_CHAPTER_RE.match(text):
                flush_current()
                if max_records is not None and len(records) >= max_records:
                    return records
                current = {"chapter": text, "heading": "", "subheading": "", "code_y": y, "desc": [], "rule": []}
                continue
            if 125.0 <= x < 170.0 and _RCEP_HEADING_RE.match(text):
                if current is not None and (current["desc"] or current["rule"] or abs(y - current["code_y"]) > 3.5):
                    flush_current()
                    if max_records is not None and len(records) >= max_records:
                        return records
                if current is None:
                    current = {"chapter": "", "heading": text, "subheading": "", "code_y": y, "desc": [], "rule": []}
                else:
                    current["heading"] = text
                continue
            if 170.0 <= x < 220.0 and _RCEP_SUBHEADING_RE.match(text):
                if current is not None and (current["desc"] or current["rule"] or abs(y - current["code_y"]) > 3.5):
                    flush_current()
                    if max_records is not None and len(records) >= max_records:
                        return records
                if current is None:
                    current = {"chapter": "", "heading": "", "subheading": text, "code_y": y, "desc": [], "rule": []}
                else:
                    current["subheading"] = text
                continue

            if current is None:
                continue

            if 220.0 <= x < 418.0:
                # A new description-only group header (e.g. '- Coffee, roasted') appearing after a rule completes the row.
                if current["rule"]:
                    flush_current()
                    if max_records is not None and len(records) >= max_records:
                        return records
                    continue
                current["desc"].append(text)
            elif x >= 418.0:
                current["rule"].append(text)

        flush_current()
        if max_records is not None and len(records) >= max_records:
            return records[:max_records]

    return records


# --- 2. PMK 43/2022 ATIGA Tariff Schedule parser ---

_TARIFF_ROW_RE = re.compile(r"^\s*['|]?\s*(\d{1,5})\s+(\d{4}[.,:]\s*[0-9lIOJ]\s*[0-9lIOJ][.,:]\s*[0-9lIOJS]\s*[0-9lIOJS])\s+(.*)$")
_RCEP_ROW_LOOSE_RE = re.compile(
    r"^(?:[\s|_t+-]*?(\d{1,5}))?\s+(\d{4}[.,:]\s*[0-9lIOJ]\s*[0-9lIOJ][.,:]\s*[0-9lIOJS]\s*[0-9lIOJS])\s+(.*)$"
)
_TARIFF_HEADING_RE = re.compile(r"^\s+(\d{2}\.\d{2}|\d{4}\.\d{2})\s+(.*)$")
_TARIFF_RATE_RE = re.compile(r"[:;']?(\d+[,.]\d+\s*%|Free|Bebas|MFN)[\s'|.:;,]*$", re.I)
_TARIFF_HEADER_NOISE_RE = re.compile(
    r"^\s*(?:-\s*\d+\s*-|I?l?f?o\.|No\.|Pos\s*Tar|HS\s*Code|Uraian|Description|Bea\s*Masuk|ATIGA|ACFTA|IJEPA|RCEP|IKCEPA|IA\s*CEPA|Ill\b|\(1\)|\(2\)|121\b)",
    re.I,
)
_RCEP_STAGING_WINDOWS: tuple[tuple[str, str], ...] = (
    ("2023-01-02", "2023-04-01"),
    ("2023-04-01", "2024-04-01"),
    ("2024-04-01", "2025-04-01"),
    ("2025-04-01", "2026-04-01"),
    ("2026-04-01", "2027-04-01"),
    ("2027-04-01", "2028-04-01"),
)
_IKCEPA_STAGING_WINDOWS: tuple[tuple[str, str], ...] = (
    ("2023-01-01", "2024-01-01"),
    ("2024-01-01", "2025-01-01"),
    ("2025-01-01", "2026-01-01"),
    ("2026-01-01", "2027-01-01"),
    ("2027-01-01", "2028-01-01"),
)
_IACEPA_STAGING_WINDOWS: tuple[tuple[str, str], ...] = (
    ("2022-04-01", "2023-01-01"),
    ("2023-01-01", "2024-01-01"),
    ("2024-01-01", "2025-01-01"),
    ("2025-01-01", "2026-01-01"),
    ("2026-01-01", "2027-01-01"),
    ("2027-01-01", "2028-01-01"),
)
_RCEP_RATE_TOKEN_RE = re.compile(
    r"(?<![\d.])(\d{1,2}\s*[,.;-]\s*\d{1,2}\s*(?:%|°/o|o/o|0/o|9/o|6/o|'/o|\"/o|<\?'0|°/4|0/4|°1<|Yo|¾)|M[FLI1'l]{1,2}[PN]|TRQ\d*)",
    re.I,
)
_RATE_PERCENT_SUFFIX_RE = re.compile(r"(?:%|°/o|o/o|0/o|9/o|6/o|'/o|\"/o|<\?'0|°/4|0/4|°1<|Yo|¾).*$", re.I)


def _normalize_ocr_hs8(raw_hs8: str) -> str:
    cleaned = raw_hs8.replace(" ", "").replace(",", ".").replace(":", ".")
    cleaned = cleaned.replace("l", "1").replace("I", "1").replace("J", "1").replace("O", "0").replace("S", "5")
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 8:
        return f"{digits[:4]}.{digits[4:6]}.{digits[6:8]}"
    return cleaned


def _split_bilingual_columns(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped:
        return "", ""
    # Prefer splitting at a wide whitespace gap (>= 3 spaces) closest to the midpoint.
    gaps = list(re.finditer(r"\s{3,}", stripped))
    if gaps:
        mid = len(stripped) / 2
        best = min(gaps, key=lambda m: abs((m.start() + m.end()) / 2 - mid))
        return stripped[: best.start()].strip(), stripped[best.end() :].strip()
    return stripped, ""


def _parse_rate_percent(raw_rate: str | None) -> str | None:
    if not raw_rate:
        return None
    cleaned = raw_rate.strip().lower()
    if cleaned in {"free", "bebas"}:
        return "0.00"
    if cleaned.startswith("m") and len(cleaned) <= 5:
        return None
    cleaned = _RATE_PERCENT_SUFFIX_RE.sub("", cleaned)
    numeric = re.sub(r"[^0-9,.;-]", "", cleaned).replace(",", ".").replace(";", ".").replace("-", ".")
    try:
        dec = Decimal(numeric)
        return f"{dec:.2f}"
    except InvalidOperation:
        return None


def parse_pmk_tariff_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    start_page: int = 1,
    end_page: int | None = None,
    max_records: int | None = None,
    agreement: str = "ATIGA",
    partner_countries: tuple[str, ...] = (),
    effective_from: str | None = None,
) -> list[TariffRecord]:
    """Parse Indonesian PMK tariff schedule table (e.g. PMK 43/2022 ATIGA, 46/2022 ACFTA, 50/2022 IJEPA, 26/2022 MFN)."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    records: list[TariffRecord] = []

    for page_no in range(first, last + 1):
        layout = reader.pages[page_no - 1].extract_text(extraction_mode="layout") or ""
        current: dict | None = None

        def flush_current():
            nonlocal current
            if current is None:
                return
            hs8 = current["hs8"]
            hs8_digits = hs8.replace(".", "")
            hs6 = f"{hs8_digits[:4]}.{hs8_digits[4:6]}"
            rate_raw = current["rate_raw"]
            mfn_excl = bool(rate_raw and rate_raw.strip().upper() == "MFN")
            records.append(TariffRecord(
                source_key=source_meta.source_key,
                source_id=source_meta.source_id,
                content_hash=source_meta.content_hash,
                row_no=current["row_no"],
                hs8=hs8,
                hs8_digits=hs8_digits,
                hs6=hs6,
                hs6_digits=hs8_digits[:6],
                heading_4=f"{hs8_digits[:2]}.{hs8_digits[2:4]}",
                description_id=re.sub(r"\s+", " ", " ".join(current["id_parts"])).strip(),
                description_en=re.sub(r"\s+", " ", " ".join(current["en_parts"])).strip(),
                tariff_rate_raw=rate_raw,
                tariff_rate_percent=None if mfn_excl else _parse_rate_percent(rate_raw),
                page=page_no,
                locator=f"Lampiran, baris {current['row_no']} ({hs8})",
                agreement=agreement,
                partner_countries=partner_countries,
                effective_from=effective_from,
                mfn_excluded=mfn_excl,
            ))
            current = None

        for raw_line in layout.splitlines():
            if not raw_line.strip():
                continue
            if _TARIFF_HEADER_NOISE_RE.match(raw_line):
                continue

            row_match = _TARIFF_ROW_RE.match(raw_line)
            if row_match:
                flush_current()
                if max_records is not None and len(records) >= max_records:
                    return records
                rest = row_match.group(3)
                rate_raw: str | None = None
                rate_match = _TARIFF_RATE_RE.search(rest)
                if rate_match:
                    rate_raw = rate_match.group(1).strip()
                    rest = rest[: rate_match.start()].rstrip()
                    # Strip preceding year column in 2-column schedules (e.g. IJEPA 50/2022: 2022 | 2023+).
                    prev_match = _TARIFF_RATE_RE.search(rest)
                    if prev_match:
                        rest = rest[: prev_match.start()].rstrip()
                desc_id, desc_en = _split_bilingual_columns(rest)
                current = {
                    "row_no": int(row_match.group(1)),
                    "hs8": _normalize_ocr_hs8(row_match.group(2)),
                    "id_parts": [desc_id] if desc_id else [],
                    "en_parts": [desc_en] if desc_en else [],
                    "rate_raw": rate_raw,
                }
                continue

            # Stop continuation if a 4-digit heading or 6-digit subheading header appears.
            if _TARIFF_HEADING_RE.match(raw_line):
                flush_current()
                continue

            if current is not None:
                stripped = raw_line.strip()
                # Skip structural dash group lines that belong to the next sub-group (ending with ':')
                if stripped.startswith("-") and stripped.endswith(":"):
                    flush_current()
                    continue
                if current["rate_raw"] is None:
                    rate_match = _TARIFF_RATE_RE.search(raw_line)
                    if rate_match:
                        current["rate_raw"] = rate_match.group(1).strip()
                        raw_line = raw_line[: rate_match.start()]
                        prev_match = _TARIFF_RATE_RE.search(raw_line)
                        if prev_match:
                            raw_line = raw_line[: prev_match.start()]
                cont_id, cont_en = _split_bilingual_columns(raw_line)
                if cont_id:
                    current["id_parts"].append(cont_id)
                if cont_en:
                    current["en_parts"].append(cont_en)

        flush_current()
        if max_records is not None and len(records) >= max_records:
            return records[:max_records]

    return records


def locate_hs8_pages(
    pdf_path: Path,
    target_hs8_codes: Iterable[str],
    *,
    start_page: int = 6,
    end_page: int | None = None,
) -> dict[str, int]:
    """Binary-search sorted Kemenkeu BTKI/PMK tariff PDFs to locate the exact page for each requested HS8 code."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    loose_hs8_re = re.compile(r"\b(\d{4}[.,:]\s*[0-9lIOJ]\s*[0-9lIOJ][.,:]\s*[0-9lIOJS]\s*[0-9lIOJS])\b")
    page_cache: dict[int, tuple[str, ...]] = {}

    def page_codes(pg: int) -> tuple[str, ...]:
        if pg not in page_cache:
            txt = reader.pages[pg - 1].extract_text() or ""
            found = []
            for m in loose_hs8_re.finditer(txt):
                norm = _normalize_ocr_hs8(m.group(1))
                if _HS8_RE.match(norm):
                    found.append(norm.replace(".", ""))
            page_cache[pg] = tuple(found)
        return page_cache[pg]

    # Trim trailing non-schedule appendix pages (e.g. RCEP Lampiran II DV20 or IA-CEPA TRQ tables)
    # where HS codes restart from Chapter 01..99!
    schedule_last = last
    probe_codes = page_codes(schedule_last)
    if probe_codes and probe_codes[0] < "90000000":
        # Walk binary/coarse backwards to find the end of Lampiran I (where HS hits >= 96000000)
        lo_t, hi_t = first, schedule_last
        while lo_t < hi_t:
            mid = (lo_t + hi_t) // 2
            mc = page_codes(mid)
            if not mc:
                hi_t = mid - 1
                continue
            # Check if Lampiran II header appears at or before mid
            txt = (reader.pages[mid - 1].extract_text() or "").upper()
            if "LAMPIRAN II" in txt or "APPENDIX TO ANNEX" in txt or "TARIFF RATE QUOTA" in txt or (mid > first + 100 and mc[0] < "10000000"):
                hi_t = mid - 1
            else:
                lo_t = mid + 1
        schedule_last = max(first, hi_t)

    located: dict[str, int] = {}
    for raw_code in sorted(set(target_hs8_codes)):
        norm = _normalize_ocr_hs8(raw_code)
        target = norm.replace(".", "")
        if len(target) != 8:
            continue
        lo, hi = first, schedule_last
        hit: int | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            codes = page_codes(mid)
            if not codes:
                if mid < first + 35:
                    lo = mid + 1
                    continue
                found_n = False
                for step in (1, -1, 2, -2, 3, -3):
                    np = mid + step
                    if lo <= np <= hi and page_codes(np):
                        mid = np
                        codes = page_codes(np)
                        found_n = True
                        break
                if not found_n:
                    break
            if target in codes:
                hit = mid
                break
            c_min, c_max = min(codes), max(codes)
            if target < c_min:
                hi = mid - 1
            elif target > c_max:
                lo = mid + 1
            else:
                hit = mid
                break
        if hit is not None:
            matched_pg = hit
            for cand_pg in (hit, hit - 1, hit + 1):
                if first <= cand_pg <= schedule_last and target in page_codes(cand_pg):
                    matched_pg = cand_pg
                    break
            located[norm] = matched_pg
    return located


def locate_psr_pages(
    pdf_path: Path,
    target_prefixes: Iterable[str],
    *,
    start_page: int = 1,
    end_page: int | None = None,
) -> dict[str, int]:
    """Binary-search sorted ATIGA / RCEP Annex PSR PDFs using left-column row anchors only."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    # Match only left-column HS anchors (ignoring 'except from heading 72.08' in right-side rule prose)
    atiga_row_re = re.compile(r"^\s*\d{1,4}\s+(\d{4}\.\d{2})\b", re.M)
    rcep_row_re = re.compile(r"^\s*(\d{2}\.\d{2}|\d{4}\.\d{2})\b", re.M)
    page_cache: dict[int, tuple[str, ...]] = {}

    def page_codes(pg: int) -> tuple[str, ...]:
        if pg not in page_cache:
            txt = reader.pages[pg - 1].extract_text() or ""
            raw = atiga_row_re.findall(txt) or rcep_row_re.findall(txt)
            found = [c.replace(".", "").ljust(6, "0") for c in raw]
            page_cache[pg] = tuple(found)
        return page_cache[pg]

    located: dict[str, int] = {}
    for raw in sorted(set(target_prefixes)):
        digits = re.sub(r"\D+", "", raw)
        if not digits:
            continue
        target6 = digits[:6].ljust(6, "0")
        lo, hi = first, last
        best: int | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            codes = page_codes(mid)
            if not codes:
                lo = mid + 1
                continue
            if any(c.startswith(digits) for c in codes):
                best = mid
                break
            c_min, c_max = codes[0], codes[-1]
            if target6 < c_min:
                hi = mid - 1
            elif target6 > c_max:
                lo = mid + 1
            else:
                best = mid
                break
        if best is not None:
            located[raw] = best
    return located


def parse_staged_tariff_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    agreement: str,
    partner_countries: tuple[str, ...],
    windows: tuple[tuple[str, str], ...],
    start_page: int = 1,
    end_page: int | None = None,
    hs8_filter: set[str] | None = None,
    x0_range: tuple[float, float] = (290.0, 306.0),
    default_x0: float = 298.5,
    col_pitch: float = 25.7,
    header_hint: str = "Maret",
) -> list[TariffRecord]:
    """Parse multi-year staged Kemenkeu PMK schedules (RCEP 221-225/2022, IK-CEPA 227/2022, IA-CEPA 54/2022) via PDF coordinates."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    records: list[TariffRecord] = []
    hs8_anchor_re = re.compile(
        r"(?:^|\s)(\d{1,5})?\s*(\d{4}[.,:]\s*[0-9lIOJ]\s*[0-9lIOJ][.,:]\s*[0-9lIOJS]\s*[0-9lIOJS])\b(.*)$"
    )
    heading_anchor_re = re.compile(r"^\d{2}\.\d{2}\b|^\d{4}\.\d{2}\b")

    for page_no in range(first, last + 1):
        items: list[tuple[float, float, str]] = []

        def visitor(text, cm, tm, font_dict, font_size):
            cleaned = text.strip()
            if cleaned:
                items.append((float(tm[5]), float(tm[4]), cleaned))

        reader.pages[page_no - 1].extract_text(visitor_text=visitor)

        col0_xs = [
            x for y, x, t in items
            if x0_range[0] <= x <= x0_range[1] and (header_hint in t or _RCEP_RATE_TOKEN_RE.search(t))
        ]
        x0 = sorted(col0_xs)[len(col0_xs) // 2] if col0_xs else default_x0

        # Merge left-column OCR fragments sharing the same baseline y (e.g. '6352 7210.12.' + '10')
        left_lines: list[tuple[float, list[tuple[float, str]]]] = []
        for y, x, text in sorted(items, key=lambda it: (-it[0], it[1])):
            if x > 135.0 or y > 508.0 or y < 35.0:
                continue
            if left_lines and abs(left_lines[-1][0] - y) <= 1.8:
                left_lines[-1][1].append((x, text))
            else:
                left_lines.append((y, [(x, text)]))

        anchors: list[dict] = []
        for y, segs in left_lines:
            segs.sort(key=lambda s: s[0])
            text = " ".join(seg_t for _, seg_t in segs)
            m = hs8_anchor_re.search(text)
            if m:
                cand = _normalize_ocr_hs8(m.group(2))
                if _HS8_RE.match(cand):
                    anchors.append({
                        "kind": "hs8",
                        "y": y,
                        "row_no": int(m.group(1)) if m.group(1) else 1,
                        "hs8": cand,
                        "desc": m.group(3).strip(),
                    })
                    continue
            if heading_anchor_re.match(text):
                anchors.append({"kind": "heading", "y": y})

        anchors.sort(key=lambda a: -a["y"])

        for idx_a, anc in enumerate(anchors):
            if anc["kind"] != "hs8":
                continue
            hs8 = anc["hs8"]
            if hs8_filter is not None and hs8 not in hs8_filter:
                continue
            y_top = anc["y"] + 2.5
            y_bot = (anchors[idx_a + 1]["y"] + 1.5) if idx_a + 1 < len(anchors) else 35.0
            if y_bot > anc["y"] - 2.0:
                y_bot = anc["y"] - 3.0

            assigned: dict[int, str] = {}
            desc_parts = [anc["desc"]] if anc["desc"] else []
            for y, x, text in items:
                if not (y_bot <= y <= y_top):
                    continue
                toks = [m.group(1).strip() for m in _RCEP_RATE_TOKEN_RE.finditer(text)]
                if toks:
                    start_col = 0 if x < x0 - 12.0 else max(0, round((x - x0) / col_pitch))
                    for offset, tok in enumerate(toks):
                        c_idx = start_col + offset
                        if 0 <= c_idx < len(windows) and c_idx not in assigned:
                            assigned[c_idx] = tok
                    first_m = _RCEP_RATE_TOKEN_RE.search(text)
                    if first_m and x < x0 - 12.0:
                        prefix_desc = text[: first_m.start()].strip()
                        if prefix_desc:
                            desc_parts.append(prefix_desc)
                elif 125.0 <= x < x0 - 12.0:
                    desc_parts.append(text)

            if not assigned:
                continue
            parsed_vals = {
                "MFN" if tok.upper().startswith(("M", "T")) else _parse_rate_percent(tok)
                for tok in assigned.values()
            }
            if len(parsed_vals) == 1 and len(assigned) >= 2:
                sole_tok = next(iter(assigned.values()))
                for c_idx in range(len(windows)):
                    assigned.setdefault(c_idx, sole_tok)
            hs8_digits = hs8.replace(".", "")
            hs6 = f"{hs8_digits[:4]}.{hs8_digits[4:6]}"
            desc_id, desc_en = _split_bilingual_columns(" ".join(desc_parts))
            for c_idx, (eff_from, eff_until) in enumerate(windows):
                tok = assigned.get(c_idx)
                if tok is None:
                    continue
                is_mfn = tok.upper().startswith("M")
                if tok.upper().startswith("T"):
                    # TRQ cell (e.g. IA-CEPA in-quota special handling) -> skip standard non-TRQ rate
                    continue
                pct = None if is_mfn else _parse_rate_percent(tok)
                if not is_mfn and pct is None:
                    continue
                records.append(TariffRecord(
                    source_key=source_meta.source_key,
                    source_id=source_meta.source_id,
                    content_hash=source_meta.content_hash,
                    row_no=anc["row_no"],
                    hs8=hs8,
                    hs8_digits=hs8_digits,
                    hs6=hs6,
                    hs6_digits=hs8_digits[:6],
                    heading_4=f"{hs8_digits[:2]}.{hs8_digits[2:4]}",
                    description_id=desc_id or hs8,
                    description_en=desc_en,
                    tariff_rate_raw="MFN" if is_mfn else f"{pct}%",
                    tariff_rate_percent=pct,
                    page=page_no,
                    locator=f"Lampiran I, baris {anc['row_no']} ({hs8}, {eff_from}..{eff_until})",
                    agreement=agreement,
                    partner_countries=partner_countries,
                    effective_from=eff_from,
                    effective_until_exclusive=eff_until,
                    mfn_excluded=is_mfn,
                ))
    return records


def parse_rcep_staged_tariff_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    partner_countries: tuple[str, ...],
    start_page: int = 1,
    end_page: int | None = None,
    max_periods: int = 5,
    hs8_filter: set[str] | None = None,
) -> list[TariffRecord]:
    """Parse Indonesia RCEP staged-year PMK tariff schedules (221-225/PMK.010/2022) via PDF point coordinates."""
    return parse_staged_tariff_pdf(
        pdf_path,
        source_meta=source_meta,
        agreement="RCEP",
        partner_countries=partner_countries,
        windows=_RCEP_STAGING_WINDOWS[:max_periods],
        start_page=start_page,
        end_page=end_page,
        hs8_filter=hs8_filter,
        x0_range=(290.0, 306.0),
        default_x0=298.5,
        col_pitch=25.7,
        header_hint="Maret",
    )


def parse_ikcepa_staged_tariff_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    start_page: int = 1,
    end_page: int | None = None,
    max_periods: int = 5,
    hs8_filter: set[str] | None = None,
) -> list[TariffRecord]:
    """Parse Indonesia IK-CEPA staged-year tariff schedule (PMK 227/PMK.010/2022) for Republic of Korea."""
    return parse_staged_tariff_pdf(
        pdf_path,
        source_meta=source_meta,
        agreement="IKCEPA",
        partner_countries=("southKorea",),
        windows=_IKCEPA_STAGING_WINDOWS[:max_periods],
        start_page=start_page,
        end_page=end_page,
        hs8_filter=hs8_filter,
        x0_range=(290.0, 308.0),
        default_x0=298.0,
        col_pitch=28.25,
        header_hint="2023",
    )


def parse_iacepa_staged_tariff_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    start_page: int = 1,
    end_page: int | None = None,
    max_periods: int = 6,
    hs8_filter: set[str] | None = None,
) -> list[TariffRecord]:
    """Parse Indonesia IA-CEPA staged-year tariff schedule (PMK 54/PMK.010/2022) for Australia."""
    return parse_staged_tariff_pdf(
        pdf_path,
        source_meta=source_meta,
        agreement="IACEPA",
        partner_countries=("australia",),
        windows=_IACEPA_STAGING_WINDOWS[:max_periods],
        start_page=start_page,
        end_page=end_page,
        hs8_filter=hs8_filter,
        x0_range=(395.0, 418.0),
        default_x0=405.3,
        col_pitch=30.8,
        header_hint="2022",
    )


def parse_rcep_appendix_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    start_page: int,
    end_page: int | None = None,
) -> list[dict]:
    """Parse Lampiran II (Appendix to Annex I — RCEP Tariff Differentials / DV20 list) from an RCEP tariff PMK."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    results: list[dict] = []
    for page_no in range(first, last + 1):
        layout = reader.pages[page_no - 1].extract_text(extraction_mode="layout") or ""
        for raw_line in layout.splitlines():
            m = _TARIFF_ROW_RE.match(raw_line)
            if not m:
                continue
            hs8 = _normalize_ocr_hs8(m.group(2))
            if not _HS8_RE.match(hs8):
                continue
            results.append({
                "record_kind": "rcep_appendix",
                "source_key": source_meta.source_key,
                "source_id": source_meta.source_id,
                "row_no": int(m.group(1)),
                "hs8": hs8,
                "hs8_digits": hs8.replace(".", ""),
                "page": page_no,
                "locator": f"Lampiran II (Appendix to Annex I), baris {int(m.group(1))} ({hs8})",
            })
    return results


@dataclass(frozen=True, slots=True)
class LartasRecord:
    source_key: str
    source_id: str
    content_hash: str
    row_no: int
    hs8: str
    hs8_digits: str
    description: str
    commodity_group: str
    supervision_mode: str  # BORDER | POST_BORDER | PROHIBITED
    requires_pi: bool
    requires_ls: bool
    requires_it: bool = False
    requires_ip: bool = False
    prohibited: bool = False
    allowed_api: tuple[str, ...] = ()
    required_doc_types: tuple[str, ...] = ()
    effective_from: str | None = None
    page: int = 1
    locator: str = ""
    record_kind: str = "lartas"

    def to_dict(self) -> dict:
        return asdict(self)


def parse_permendag_lartas_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    commodity_group: str,
    start_page: int = 1,
    end_page: int | None = None,
    effective_from: str | None = None,
    allowed_api: tuple[str, ...] = ("apiP", "apiU"),
    hs8_filter: set[str] | None = None,
) -> list[LartasRecord]:
    """Parse Kemendag 2025 sectoral Lartas tables (IT|IP|PI|LS|Post Border checkmarks) via PDF point coordinates."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    records: list[LartasRecord] = []

    for page_no in range(first, last + 1):
        items: list[tuple[float, float, str]] = []

        def visitor(text, cm, tm, font_dict, font_size):
            cleaned = text.strip()
            if cleaned:
                items.append((float(tm[5]), float(tm[4]), cleaned))

        reader.pages[page_no - 1].extract_text(visitor_text=visitor)

        # Locate column header rows (a page may contain multiple table headers, e.g. Permendag 17 p.80).
        headers: list[dict[str, float]] = []
        for y, x, text in items:
            if text == "PI" and x > 700.0:
                col_map: dict[str, float] = {"_y": y, "PI": x}
                for hy, hx, ht in items:
                    if abs(hy - y) <= 12.0 and hx > 700.0:
                        if ht in ("IT", "IP", "LS"):
                            col_map[ht] = hx
                        elif ht in ("Post", "Border"):
                            col_map["POST_BORDER"] = max(col_map.get("POST_BORDER", 0.0), hx)
                headers.append(col_map)
        headers.sort(key=lambda h: -h["_y"])

        # Find HS8 row anchors on the left side of the table.
        hs_rows: list[tuple[float, str, int, str]] = []
        for y, x, text in items:
            if x > 140.0 or not _HS8_RE.match(text):
                continue
            if hs8_filter is not None and text not in hs8_filter:
                continue
            row_no = 0
            desc_parts: list[str] = []
            for ry, rx, rt in items:
                if abs(ry - y) <= 4.5:
                    if rx < x - 5.0 and re.match(r"^\d{1,5}\.?$", rt):
                        row_no = int(rt.rstrip("."))
                    elif x + 20.0 < rx < 420.0:
                        desc_parts.append(rt)
            hs_rows.append((y, text, row_no, " ".join(desc_parts)))
        hs_rows.sort(key=lambda r: -r[0])

        for y, hs8, row_no, desc in hs_rows:
            active_hdr = None
            for hdr in headers:
                if hdr["_y"] >= y:
                    active_hdr = hdr
            if active_hdr is None and headers:
                active_hdr = headers[0]
            if active_hdr is None:
                continue

            flags = {"IT": False, "IP": False, "PI": False, "LS": False, "POST_BORDER": False}
            for cy, cx, ct in items:
                if abs(cy - y) <= 4.5 and ("√" in ct or "✓" in ct) and cx > 700.0:
                    best_col = min(
                        (k for k in flags if k in active_hdr),
                        key=lambda k: abs(cx - active_hdr[k]),
                        default=None,
                    )
                    if best_col is not None and abs(cx - active_hdr[best_col]) <= 22.0:
                        flags[best_col] = True

            req_docs: list[str] = []
            if flags["PI"]:
                req_docs.append("persetujuanImpor")
            if flags["LS"]:
                req_docs.append("laporanSurveyor")
            if not req_docs and not flags["IT"] and not flags["IP"]:
                continue

            mode = "POST_BORDER" if flags["POST_BORDER"] else "BORDER"
            records.append(LartasRecord(
                source_key=source_meta.source_key,
                source_id=source_meta.source_id,
                content_hash=source_meta.content_hash,
                row_no=row_no,
                hs8=hs8,
                hs8_digits=hs8.replace(".", ""),
                description=desc or hs8,
                commodity_group=commodity_group,
                supervision_mode=mode,
                requires_pi=flags["PI"],
                requires_ls=flags["LS"],
                requires_it=flags["IT"],
                requires_ip=flags["IP"],
                prohibited=False,
                allowed_api=allowed_api,
                required_doc_types=tuple(req_docs),
                effective_from=effective_from,
                page=page_no,
                locator=f"Lampiran I, baris {row_no} ({hs8}, {commodity_group})",
            ))
    return records


def parse_permendag_prohibited_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    commodity_group: str,
    start_page: int = 1,
    end_page: int | None = None,
    effective_from: str | None = None,
    hs8_filter: set[str] | None = None,
    include_ex: bool = False,
) -> list[LartasRecord]:
    """Parse Kemendag prohibited-import (Barang Dilarang Impor, e.g. Permendag 47/2025) Lampiran rows."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    row_re = re.compile(r"^\s*(\d{1,4})\.\s+(ex\s+)?(\d{4}\.\d{2}\.\d{2})\s+(.*)$")
    records: list[LartasRecord] = []
    for page_no in range(first, last + 1):
        text = reader.pages[page_no - 1].extract_text() or ""
        for raw_line in text.splitlines():
            m = row_re.match(raw_line)
            if not m:
                continue
            if m.group(2) and not include_ex:
                continue
            row_no, hs8, desc = int(m.group(1)), m.group(3), m.group(4).strip()
            if hs8_filter is not None and hs8 not in hs8_filter:
                continue
            records.append(LartasRecord(
                source_key=source_meta.source_key,
                source_id=source_meta.source_id,
                content_hash=source_meta.content_hash,
                row_no=row_no,
                hs8=hs8,
                hs8_digits=hs8.replace(".", ""),
                description=desc or hs8,
                commodity_group=commodity_group,
                supervision_mode="PROHIBITED",
                requires_pi=False,
                requires_ls=False,
                prohibited=True,
                allowed_api=(),
                required_doc_types=(),
                effective_from=effective_from,
                page=page_no,
                locator=f"Lampiran, baris {row_no} ({hs8}, {commodity_group})",
            ))
    return records


@dataclass(frozen=True, slots=True)
class ExportLartasRecord:
    source_key: str
    source_id: str
    content_hash: str
    row_no: int
    hs8: str
    hs8_digits: str
    description: str
    commodity_group: str
    supervision_mode: str  # RESTRICTED_EXPORT | PROHIBITED_EXPORT
    requires_et: bool
    requires_pe: bool
    requires_ls: bool
    prohibited: bool = False
    required_doc_types: tuple[str, ...] = ()
    effective_from: str | None = None
    page: int = 1
    locator: str = ""
    record_kind: str = "export_lartas"

    def to_dict(self) -> dict:
        return asdict(self)


def parse_permendag_export_lartas_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    commodity_group: str,
    start_page: int = 25,
    end_page: int | None = None,
    effective_from: str | None = None,
    hs8_filter: set[str] | None = None,
) -> list[ExportLartasRecord]:
    """Parse Kemendag Export Lartas tables (ET | PE | LS checkmarks, e.g. Permendag 23/2023 & 9/2025) via PDF coordinates."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    records: list[ExportLartasRecord] = []

    for page_no in range(first, last + 1):
        items: list[tuple[float, float, str]] = []

        def visitor(text, cm, tm, font_dict, font_size):
            cleaned = text.strip()
            if cleaned:
                items.append((float(tm[5]), float(tm[4]), cleaned))

        reader.pages[page_no - 1].extract_text(visitor_text=visitor)

        headers: list[dict[str, float]] = []
        for y, x, text in items:
            if text == "PE" and x > 650.0:
                col_map: dict[str, float] = {"_y": y, "PE": x}
                for hy, hx, ht in items:
                    if abs(hy - y) <= 12.0 and hx > 650.0 and ht in ("ET", "LS"):
                        col_map[ht] = hx
                headers.append(col_map)
        headers.sort(key=lambda h: -h["_y"])

        hs_rows: list[tuple[float, str, int, str]] = []
        for y, x, text in items:
            hs_clean = re.sub(r"^ex\s+", "", text, flags=re.I).strip()
            if x > 180.0 or not _HS8_RE.match(hs_clean):
                continue
            if hs8_filter is not None and hs_clean not in hs8_filter:
                continue
            row_no = 0
            desc_parts: list[str] = []
            for ry, rx, rt in items:
                if abs(ry - y) <= 6.0:
                    if rx < x - 5.0 and re.match(r"^\d{1,5}\.?$", rt):
                        row_no = int(rt.rstrip("."))
                    elif x + 20.0 < rx < 420.0:
                        desc_parts.append(rt)
            hs_rows.append((y, hs_clean, row_no, " ".join(desc_parts)))
        hs_rows.sort(key=lambda r: -r[0])

        for y, hs8, row_no, desc in hs_rows:
            active_hdr = None
            for hdr in headers:
                if hdr["_y"] >= y:
                    active_hdr = hdr
            if active_hdr is None and headers:
                active_hdr = headers[0]
            if active_hdr is None:
                continue

            flags = {"ET": False, "PE": False, "LS": False}
            for cy, cx, ct in items:
                if abs(cy - y) <= 10.0 and ("√" in ct or "✓" in ct) and cx > 650.0:
                    best_col = min(
                        (k for k in flags if k in active_hdr),
                        key=lambda k: abs(cx - active_hdr[k]),
                        default=None,
                    )
                    if best_col is not None and abs(cx - active_hdr[best_col]) <= 22.0:
                        flags[best_col] = True

            req_docs: list[str] = []
            if flags["ET"]:
                req_docs.append("eksportirTerdaftar")
            if flags["PE"]:
                req_docs.append("persetujuanEkspor")
            if flags["LS"]:
                req_docs.append("laporanSurveyorEkspor")
            if not req_docs:
                continue

            records.append(ExportLartasRecord(
                source_key=source_meta.source_key,
                source_id=source_meta.source_id,
                content_hash=source_meta.content_hash,
                row_no=row_no,
                hs8=hs8,
                hs8_digits=hs8.replace(".", ""),
                description=desc or hs8,
                commodity_group=commodity_group,
                supervision_mode="RESTRICTED_EXPORT",
                requires_et=flags["ET"],
                requires_pe=flags["PE"],
                requires_ls=flags["LS"],
                prohibited=False,
                required_doc_types=tuple(req_docs),
                effective_from=effective_from,
                page=page_no,
                locator=f"Lampiran, baris {row_no} ({hs8}, {commodity_group})",
            ))
    return records


def parse_permendag_prohibited_export_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    commodity_group: str,
    start_page: int = 4,
    end_page: int | None = None,
    effective_from: str | None = None,
    hs8_filter: set[str] | None = None,
    include_ex: bool = False,
) -> list[ExportLartasRecord]:
    """Parse Kemendag prohibited-export (Barang Dilarang Ekspor, e.g. Permendag 22/2023 & 8/2025) Lampiran rows."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)
    row_re = re.compile(r"^\s*(\d{1,4})\.\s+(ex\s+)?(\d{4}\.\d{2}\.\d{2})\s+(.*)$")
    records: list[ExportLartasRecord] = []
    for page_no in range(first, last + 1):
        text = reader.pages[page_no - 1].extract_text() or ""
        for raw_line in text.splitlines():
            m = row_re.match(raw_line)
            if not m:
                continue
            if m.group(2) and not include_ex:
                continue
            row_no, hs8, desc = int(m.group(1)), m.group(3), m.group(4).strip()
            if hs8_filter is not None and hs8 not in hs8_filter:
                continue
            records.append(ExportLartasRecord(
                source_key=source_meta.source_key,
                source_id=source_meta.source_id,
                content_hash=source_meta.content_hash,
                row_no=row_no,
                hs8=hs8,
                hs8_digits=hs8.replace(".", ""),
                description=desc or hs8,
                commodity_group=commodity_group,
                supervision_mode="PROHIBITED_EXPORT",
                requires_et=False,
                requires_pe=False,
                requires_ls=False,
                prohibited=True,
                required_doc_types=(),
                effective_from=effective_from,
                page=page_no,
                locator=f"Lampiran, baris {row_no} ({hs8}, {commodity_group})",
            ))
    return records


@dataclass(frozen=True, slots=True)
class ExportDutyRecord:
    source_key: str
    source_id: str
    content_hash: str
    hs8: str
    hs8_digits: str
    ex_code: bool
    commodity_group: str
    description: str
    lampiran_section: str  # A | B | C | E | F | G
    duty_basis: str  # AD_VALOREM | SPECIFIC_USD_PER_MT
    rate_value: str  # decimal string (% for AD_VALOREM, USD/MT for SPECIFIC_USD_PER_MT)
    reference_price_min_exclusive: str | None = None
    reference_price_max_inclusive: str | None = None
    tier_column: int | None = None
    effective_from: str | None = None
    effective_to_exclusive: str | None = None
    page: int = 1
    locator: str = ""
    record_kind: str = "export_duty"

    def to_dict(self) -> dict:
        return asdict(self)


_COCOA_HR_TIERS: tuple[tuple[int, str | None, str | None], ...] = (
    (1, None, "2000.00"),
    (2, "2000.00", "2750.00"),
    (3, "2750.00", "3500.00"),
    (4, "3500.00", None),
)

_CPO_HR_TIERS: tuple[tuple[int, str | None, str | None], ...] = (
    (1, None, "680.00"),
    (2, "680.00", "730.00"),
    (3, "730.00", "780.00"),
    (4, "780.00", "830.00"),
    (5, "830.00", "880.00"),
    (6, "880.00", "930.00"),
    (7, "930.00", "980.00"),
    (8, "980.00", "1030.00"),
    (9, "1030.00", "1080.00"),
    (10, "1080.00", "1130.00"),
    (11, "1130.00", "1180.00"),
    (12, "1180.00", "1230.00"),
    (13, "1230.00", "1280.00"),
    (14, "1280.00", "1330.00"),
    (15, "1330.00", "1380.00"),
    (16, "1380.00", "1430.00"),
    (17, "1430.00", None),
)

_BK_SECTION_HEADERS: dict[str, tuple[str, str]] = {
    "A": ("Kulit dan Kayu", "AD_VALOREM"),
    "B": ("Biji Kakao", "AD_VALOREM"),
    "C": ("Kelapa Sawit, CPO, dan Produk Turunannya", "SPECIFIC_USD_PER_MT"),
    "E": ("Produk Hasil Pengolahan Mineral Logam", "AD_VALOREM"),
    "F": ("Produk Mineral Logam dengan Kriteria Tertentu", "AD_VALOREM"),
    "G": ("Getah Pinus", "AD_VALOREM"),
}


def _norm_bk_decimal(raw: str) -> str:
    return f"{Decimal(raw.strip().replace(',', '.')):.2f}"


def parse_pmk_export_duty_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    effective_from: str,
    effective_to_exclusive: str | None = None,
    sections: set[str] | None = None,
    hs8_filter: set[str] | None = None,
) -> list[ExportDutyRecord]:
    """Parse Indonesian Kemenkeu Bea Keluar Lampiran tables (PMK 38/2024 & PMK 68/2025) into typed ExportDutyRecords."""
    reader = PdfReader(str(pdf_path))
    # Locate start of Lampiran
    lampiran_start = 1
    for idx, page in enumerate(reader.pages, 1):
        txt = page.extract_text() or ""
        if "A. BESARAN TARIF BEA KELUAR" in txt.upper():
            lampiran_start = idx
            break

    sec_re = re.compile(r"^\s*([A-G])\.\s+(?:BESARAN TARIF BEA KELUAR|JENIS BARANG)", re.I)
    hs_token_re = re.compile(r"\b(ex\s+)?(\d{4}\.\d{2}\.\d{2})\b", re.I)
    rate_num_re = re.compile(r"\b(\d+(?:[.,]\d+)?)\b")

    # Collect per-section lines along with page numbers
    section_chunks: dict[str, list[tuple[int, str]]] = {}
    current_sec: str | None = None
    for page_no in range(lampiran_start, len(reader.pages) + 1):
        txt = reader.pages[page_no - 1].extract_text() or ""
        for raw_line in txt.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line or re.match(r"^-\s*\d+\s*-$", line):
                continue
            m_sec = sec_re.match(line)
            if m_sec:
                current_sec = m_sec.group(1).upper()
                section_chunks.setdefault(current_sec, [])
                continue
            if current_sec is not None:
                section_chunks[current_sec].append((page_no, line))

    records: list[ExportDutyRecord] = []

    def _emit_flat(sec_code: str, lines: list[tuple[int, str]]) -> None:
        group_name, basis = _BK_SECTION_HEADERS[sec_code]
        # Walk lines accumulating HS8 codes per row; once a trailing rate number appears after HS8s, emit.
        pending_codes: list[tuple[int, bool, str]] = []
        last_desc = group_name
        skip_row = False
        for page_no, line in lines:
            if any(h in line.upper() for h in ("NO. URAIAN", "TERMASUK DALAM", "POS TARIF", "TARIF BEA", "MENTERI KEUANGAN", "PURBAYA", "SRI MULYANI")):
                continue
            if "DIKECUALIKAN DARI PENGENAAN BEA KELUAR" in line.upper():
                pending_codes.clear()
                skip_row = True
                continue
            matches = list(hs_token_re.finditer(line))
            if matches:
                prefix = line[:matches[0].start()].strip(" -0123456789.aAbBcC)")
                if prefix and len(prefix) >= 3:
                    last_desc = prefix
                    skip_row = False
                for m in matches:
                    if not skip_row:
                        pending_codes.append((page_no, bool(m.group(1)), m.group(2)))
                after = line[matches[-1].end():].strip()
                rate_m = re.match(r"^(\d+(?:[.,]\d+)?)$", after)
                if rate_m and pending_codes and not skip_row:
                    rate_str = _norm_bk_decimal(rate_m.group(1))
                    for pg, is_ex, hs8 in pending_codes:
                        if hs8_filter is not None and hs8 not in hs8_filter:
                            continue
                        records.append(ExportDutyRecord(
                            source_key=source_meta.source_key,
                            source_id=source_meta.source_id,
                            content_hash=source_meta.content_hash,
                            hs8=hs8,
                            hs8_digits=hs8.replace(".", ""),
                            ex_code=is_ex,
                            commodity_group=group_name,
                            description=last_desc,
                            lampiran_section=sec_code,
                            duty_basis=basis,
                            rate_value=rate_str,
                            effective_from=effective_from,
                            effective_to_exclusive=effective_to_exclusive,
                            page=pg,
                            locator=f"Lampiran huruf {sec_code}, hal. {pg} ({'ex ' if is_ex else ''}{hs8})",
                        ))
                    pending_codes.clear()
            else:
                rate_only = re.match(r"^(\d+(?:[.,]\d+)?)$", line)
                if rate_only and pending_codes and not skip_row:
                    rate_str = _norm_bk_decimal(rate_only.group(1))
                    for pg, is_ex, hs8 in pending_codes:
                        if hs8_filter is not None and hs8 not in hs8_filter:
                            continue
                        records.append(ExportDutyRecord(
                            source_key=source_meta.source_key,
                            source_id=source_meta.source_id,
                            content_hash=source_meta.content_hash,
                            hs8=hs8,
                            hs8_digits=hs8.replace(".", ""),
                            ex_code=is_ex,
                            commodity_group=group_name,
                            description=last_desc,
                            lampiran_section=sec_code,
                            duty_basis=basis,
                            rate_value=rate_str,
                            effective_from=effective_from,
                            effective_to_exclusive=effective_to_exclusive,
                            page=pg,
                            locator=f"Lampiran huruf {sec_code}, hal. {pg} ({'ex ' if is_ex else ''}{hs8})",
                        ))
                    pending_codes.clear()
                elif not pending_codes and len(line) > 3 and not line.startswith("("):
                    cleaned = re.sub(r"^(?:[IVX]+|[A-Z]|[a-z]|\d+)[.)]\s*", "", line).strip(" -")
                    if cleaned:
                        last_desc = cleaned
                        skip_row = False

    def _emit_tiered(
        sec_code: str,
        lines: list[tuple[int, str]],
        tiers: tuple[tuple[int, str | None, str | None], ...],
    ) -> None:
        group_name, basis = _BK_SECTION_HEADERS[sec_code]
        expected_n = len(tiers)
        # Merge Lampiran text tokens into logical numbered commodity rows.
        item_re = re.compile(r"^(\d{1,2})\.\s*(.*)$")
        rows: list[tuple[int, str, list[str]]] = []
        for page_no, line in lines:
            up = line.upper()
            if any(h in up for h in (
                "NO. URAIAN", "TERMASUK", "POS TARIF", "TARIF BEA KELUAR", "KOLOM 1",
                "SAMPAI DENGAN", "≤USD", ">USD", "KELOMPOK", "1 2 3 4 5",
            )):
                continue
            if re.match(r"^(?:Ia|Ib|II|III|IV|V)$", line):
                continue
            m_it = item_re.match(line)
            if m_it:
                rows.append((page_no, m_it.group(1), [m_it.group(2)]))
            elif rows:
                rows[-1][2].append(line)

        for pg, row_no, chunks in rows:
            joined = " ".join(chunks)
            matches = list(hs_token_re.finditer(joined))
            if not matches:
                continue
            desc = re.sub(r"\s+", " ", joined[:matches[0].start()]).strip(" -") or group_name
            after = joined[matches[-1].end():].strip()
            nums = rate_num_re.findall(after)
            if len(nums) < expected_n:
                continue
            tier_rates = nums[:expected_n]
            for m in matches:
                is_ex, hs8 = bool(m.group(1)), m.group(2)
                if hs8_filter is not None and hs8 not in hs8_filter:
                    continue
                for (col_no, hr_min, hr_max), raw_rate in zip(tiers, tier_rates):
                    records.append(ExportDutyRecord(
                        source_key=source_meta.source_key,
                        source_id=source_meta.source_id,
                        content_hash=source_meta.content_hash,
                        hs8=hs8,
                        hs8_digits=hs8.replace(".", ""),
                        ex_code=is_ex,
                        commodity_group=group_name,
                        description=desc,
                        lampiran_section=sec_code,
                        duty_basis=basis,
                        rate_value=_norm_bk_decimal(raw_rate),
                        reference_price_min_exclusive=hr_min,
                        reference_price_max_inclusive=hr_max,
                        tier_column=col_no,
                        effective_from=effective_from,
                        effective_to_exclusive=effective_to_exclusive,
                        page=pg,
                        locator=f"Lampiran huruf {sec_code} No. {row_no} Kolom {col_no}, hal. {pg} ({'ex ' if is_ex else ''}{hs8})",
                    ))

    for sec_code, lines in section_chunks.items():
        if sec_code not in _BK_SECTION_HEADERS:
            continue
        if sections is not None and sec_code not in sections:
            continue
        if sec_code == "B":
            _emit_tiered("B", lines, _COCOA_HR_TIERS)
        elif sec_code == "C":
            _emit_tiered("C", lines, _CPO_HR_TIERS)
        else:
            _emit_flat(sec_code, lines)

    return records


# --- 3. Indonesian Legal Statute (PMK / Permendag) clause parser ---

_PAGE_NUM_LINE_RE = re.compile(r"^\s*-\s*\d+\s*-\s*$")
_BAB_LINE_RE = re.compile(r"^\s*B\s*A\s*B\s+([IVXLC]+)\s*$")
_BAGIAN_LINE_RE = re.compile(r"^\s*Bagian\s+(Ke\w+)\s*$", re.I)
_PASAL_LINE_RE = re.compile(r"^\s*P\s*a\s*s\s*a\s*l\s*(\d+[A-Z]?|[IVX]+)\s*$")
_AYAT_START_RE = re.compile(r"(?:^|\n)\(\s*(\d+[a-z]?)\s*\)\s+")
_CROSS_REF_RE = re.compile(
    r"\b("
    r"Pasal\s+\d+[A-Z]?(?:\s+ayat\s+\(\d+[a-z]?\))?"
    r"|Peraturan\s+Menteri\s+[A-Za-z]+\s+Nomor\s+[\w./-]+\s+Tahun\s+\d{4}"
    r"|(?:PMK|Permendag)\s+(?:Nomor\s+)?[\w./-]+(?:\s+Tahun\s+\d{4})?"
    r")\b",
    re.I,
)


def normalize_statute_line(line: str) -> str:
    """Normalize lightweight OCR spacing noise on structural statute headings/markers."""
    text = re.sub(r"\s+", " ", line).strip()
    text = re.sub(r"^P\s*a\s*s\s*a\s*l\s*(\d+[A-Z]?|[IVX]+)$", r"Pasal \1", text)
    text = re.sub(r"^B\s*A\s*B\s+([IVXLC]+)$", r"BAB \1", text)
    text = re.sub(r"^\(\s*(\d+[a-z]?)\s*\)\s*", r"(\1) ", text)
    text = re.sub(r"\bP\s*a\s*s\s*a\s*l\s*(\d+[A-Z]?)\b", r"Pasal \1", text)
    text = re.sub(r"\bayat\s+\(\s*(\d+[a-z]?)\s*\)", r"ayat (\1)", text, flags=re.I)
    return text


def _split_ayat(body: str) -> tuple[dict[str, str], ...]:
    matches = list(_AYAT_START_RE.finditer(body))
    if not matches:
        return ()
    parts: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        clause_text = re.sub(r"\s+", " ", body[start:end]).strip()
        parts.append({"ayat": match.group(1), "text": clause_text})
    return tuple(parts)


def parse_indonesian_statute_pdf(
    pdf_path: Path,
    *,
    source_meta: SourceMeta,
    start_page: int = 1,
    end_page: int | None = None,
    max_records: int | None = None,
) -> list[LegalClauseRecord]:
    """Segment Indonesian PMK / Permendag PDF text into structured BAB / Pasal / Ayat clauses."""
    reader = PdfReader(str(pdf_path))
    first, last = _page_bounds(len(reader.pages), start_page, end_page)

    records: list[LegalClauseRecord] = []
    current_bab: str | None = None
    current_bab_title_lines: list[str] = []
    capturing_bab_title = False
    current_bagian: str | None = None
    active_pasal: dict | None = None

    def flush_pasal():
        nonlocal active_pasal
        if active_pasal is None:
            return
        raw_body = "\n".join(active_pasal["lines"]).strip()
        clean_text = re.sub(r"\s+", " ", raw_body).strip()
        refs = tuple(dict.fromkeys(
            re.sub(r"\s+", " ", m.group(1)).strip()
            for m in _CROSS_REF_RE.finditer(clean_text)
        ))
        pasal_label = active_pasal["pasal"]
        bab = active_pasal["bab"]
        locator = f"{bab}, {pasal_label}" if bab else pasal_label
        records.append(LegalClauseRecord(
            source_key=source_meta.source_key,
            source_id=source_meta.source_id,
            content_hash=source_meta.content_hash,
            bab=bab,
            bab_title=active_pasal["bab_title"],
            bagian=active_pasal["bagian"],
            pasal=pasal_label,
            text=clean_text,
            ayat=_split_ayat(raw_body),
            cross_references=refs,
            page_start=active_pasal["page_start"],
            page_end=active_pasal["page_end"],
            locator=locator,
        ))
        active_pasal = None

    for page_no in range(first, last + 1):
        page_text = reader.pages[page_no - 1].extract_text() or ""
        for raw_line in page_text.splitlines():
            if not raw_line.strip() or _PAGE_NUM_LINE_RE.match(raw_line):
                continue
            line = normalize_statute_line(raw_line)
            if not line:
                continue

            bab_match = _BAB_LINE_RE.match(line)
            if bab_match:
                flush_pasal()
                if max_records is not None and len(records) >= max_records:
                    return records
                current_bab = f"BAB {bab_match.group(1)}"
                current_bab_title_lines = []
                capturing_bab_title = True
                current_bagian = None
                continue

            bagian_match = _BAGIAN_LINE_RE.match(line)
            if bagian_match:
                flush_pasal()
                capturing_bab_title = False
                current_bagian = f"Bagian {bagian_match.group(1)}"
                continue

            pasal_match = _PASAL_LINE_RE.match(line)
            if pasal_match:
                flush_pasal()
                if max_records is not None and len(records) >= max_records:
                    return records
                capturing_bab_title = False
                active_pasal = {
                    "bab": current_bab,
                    "bab_title": " ".join(current_bab_title_lines).strip() or None,
                    "bagian": current_bagian,
                    "pasal": f"Pasal {pasal_match.group(1)}",
                    "lines": [],
                    "page_start": page_no,
                    "page_end": page_no,
                }
                continue

            if capturing_bab_title:
                current_bab_title_lines.append(line)
                continue

            if active_pasal is not None:
                active_pasal["lines"].append(line)
                active_pasal["page_end"] = page_no

    flush_pasal()
    return records[:max_records] if max_records is not None else records


# --- 4. Archived regulatory web guidance HTML parser ---

def parse_guidance_html(
    html_path: Path,
    *,
    source_meta: SourceMeta,
    max_records: int | None = None,
) -> list[GuidanceSectionRecord]:
    """Extract heading-scoped sections and links from archived regulatory guidance HTML."""
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8", errors="replace"), "lxml")
    for tag in soup.find_all(["script", "style", "noscript", "nav", "header", "footer", "svg"]):
        tag.decompose()

    root = soup.find("main") or soup.find("article") or soup.body or soup
    headings = root.find_all(["h1", "h2", "h3", "h4"])
    records: list[GuidanceSectionRecord] = []

    for heading in headings:
        title = re.sub(r"\s+", " ", heading.get_text(" ", strip=True))
        if not title:
            continue
        level = int(heading.name[1])
        body_parts: list[str] = []
        links: list[dict[str, str]] = []
        seen_links: set[str] = set()

        for sibling in heading.find_next_siblings():
            if sibling.name in {"h1", "h2", "h3", "h4"}:
                break
            text = re.sub(r"\s+", " ", sibling.get_text(" ", strip=True))
            if text:
                body_parts.append(text)
            for anchor in sibling.find_all("a", href=True):
                href = anchor["href"].strip()
                label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
                if href and href not in seen_links and not href.startswith(("#", "javascript:")):
                    seen_links.add(href)
                    links.append({"label": label or href, "href": href})

        combined = " ".join(body_parts).strip()
        if not combined:
            continue
        records.append(GuidanceSectionRecord(
            source_key=source_meta.source_key,
            source_id=source_meta.source_id,
            content_hash=source_meta.content_hash,
            heading=title,
            heading_level=level,
            text=combined,
            links=tuple(links),
            locator=title,
        ))
        if max_records is not None and len(records) >= max_records:
            break

    return records


# --- Automated cross-check verification ---

@dataclass(frozen=True, slots=True)
class FlaggedRecord:
    record: dict
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    verified: tuple[dict, ...]
    flagged: tuple[FlaggedRecord, ...]


_HS2_RE = re.compile(r"^\d{2}$")
_HS4_RE = re.compile(r"^\d{2}\.?\d{2}$")
_HS6_RE = re.compile(r"^\d{4}\.\d{2}$")
_HS8_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def verify_record(record: dict, *, known_hs6: set[str] | None = None) -> tuple[str, ...]:
    """Return cross-check failure reasons for a parsed/scraped record (empty tuple means verified)."""
    kind = record.get("record_kind")
    reasons: list[str] = []

    if kind == "psr":
        agreement = str(record.get("agreement") or "ATIGA").upper()
        hs_level = str(record.get("hs_level") or "SUBHEADING").upper()
        hs_code = str(record.get("hs_code") or record.get("hs6") or "")
        hs_digits = str(record.get("hs_digits") or record.get("hs6_digits") or "")
        concise = str(record.get("concise_rule") or "").strip()
        full = str(record.get("full_rule") or "").strip()
        full_lower = full.lower()

        if hs_level == "CHAPTER":
            if not _HS2_RE.match(hs_code) or hs_code != hs_digits:
                reasons.append(f"Invalid HS2 chapter code: {hs_code!r}")
        elif hs_level == "HEADING":
            if not _HS4_RE.match(hs_code) or hs_code.replace(".", "") != hs_digits:
                reasons.append(f"Invalid HS4 heading code: {hs_code!r}")
        else:
            if not _HS6_RE.match(hs_code) or hs_code.replace(".", "") != hs_digits:
                reasons.append(f"Invalid HS6 code: {hs_code!r}")

        if not isinstance(record.get("row_no"), int) or record["row_no"] < 1:
            reasons.append("Invalid row_no")
        if not concise:
            reasons.append("Missing concise_rule")
        alts = record.get("criteria_alternatives") or ()
        if not alts:
            reasons.append("No parsed criteria_alternatives")

        if agreement == "ATIGA":
            if not full:
                reasons.append("Missing full_rule")
            sub_mentions = re.findall(r"subheading\s+(\d{4}\.\d{2})", full_lower)
            if sub_mentions and hs_code not in sub_mentions:
                reasons.append(f"Full rule references {sub_mentions}, expected {hs_code}")

        for alt in alts:
            for token in alt:
                if not isinstance(token, dict):
                    continue
                tkind = token.get("kind")
                if tkind == "OTHER" and not full:
                    reasons.append(f"Unparsed criterion token: {token.get('raw')!r}")
                if not full:
                    continue
                if tkind == "WO" and "wholly obtained" not in full_lower:
                    reasons.append("Concise WO not matched in full_rule")
                elif tkind == "RVC":
                    pct = token.get("threshold_percent", "")
                    if f"{pct} percent" not in full_lower and f"{pct}%" not in full_lower:
                        reasons.append(f"Concise RVC{pct} not matched in full_rule")
                elif tkind == "CC" and "other chapter" not in full_lower:
                    reasons.append("Concise CC not matched in full_rule")
                elif tkind == "CTH" and "other heading" not in full_lower:
                    reasons.append("Concise CTH not matched in full_rule")
                elif tkind == "CTSH" and "other subheading" not in full_lower:
                    reasons.append("Concise CTSH not matched in full_rule")
                elif tkind == "TEXTILE_PROCESS" and not ("textile" in full_lower or "attachment" in full_lower):
                    reasons.append("Concise textile process rule not matched in full_rule")
                elif tkind == "CUT_AND_SEWN_AMS" and "cut and sewn" not in full_lower:
                    reasons.append("Concise cut-and-sewn rule not matched in full_rule")
                elif tkind == "REFINING_PROCESS" and "refining" not in full_lower:
                    reasons.append("Concise refining process rule not matched in full_rule")
                elif tkind == "CHEMICAL_REACTION" and "chemical" not in full_lower:
                    reasons.append("Concise chemical reaction rule not matched in full_rule")
        return tuple(dict.fromkeys(reasons))

    if kind == "tariff":
        hs8 = str(record.get("hs8") or "")
        hs8_digits = str(record.get("hs8_digits") or "")
        hs6 = str(record.get("hs6") or "")
        if not _HS8_RE.match(hs8) or hs8.replace(".", "") != hs8_digits:
            reasons.append(f"Invalid HS8 code: {hs8!r}")
        if not hs8.startswith(hs6):
            reasons.append(f"HS8 {hs8} does not match HS6 {hs6}")
        if known_hs6 is not None and hs6 not in known_hs6:
            reasons.append(f"HS6 {hs6} not found in reference PSR set")
        if not (record.get("description_id") or record.get("description_en")):
            reasons.append("Missing tariff description")
        rate_str = record.get("tariff_rate_percent")
        if record.get("mfn_excluded"):
            if rate_str is not None:
                reasons.append("mfn_excluded tariff should not carry preferential numeric rate")
        elif rate_str is None:
            reasons.append("Missing numeric tariff_rate_percent")
        else:
            try:
                val = Decimal(str(rate_str))
                if val < 0 or val > 1000:
                    reasons.append(f"Tariff rate out of range: {rate_str}")
            except InvalidOperation:
                reasons.append(f"Invalid decimal tariff_rate_percent: {rate_str!r}")
        return tuple(dict.fromkeys(reasons))

    if kind == "rcep_appendix":
        hs8 = str(record.get("hs8") or "")
        hs8_digits = str(record.get("hs8_digits") or "")
        if not _HS8_RE.match(hs8) or hs8.replace(".", "") != hs8_digits:
            reasons.append(f"Invalid HS8 code in RCEP Appendix: {hs8!r}")
        return tuple(reasons)

    if kind == "lartas":
        hs8 = str(record.get("hs8") or "")
        hs8_digits = str(record.get("hs8_digits") or "")
        if not _HS8_RE.match(hs8) or hs8.replace(".", "") != hs8_digits:
            reasons.append(f"Invalid HS8 code in Lartas record: {hs8!r}")
        mode = str(record.get("supervision_mode") or "")
        if mode not in {"BORDER", "POST_BORDER", "PROHIBITED"}:
            reasons.append(f"Invalid Lartas supervision_mode: {mode!r}")
        if record.get("prohibited"):
            if mode != "PROHIBITED":
                reasons.append("Prohibited Lartas record must use PROHIBITED supervision_mode")
        elif not (record.get("required_doc_types") or record.get("requires_pi") or record.get("requires_ls")):
            reasons.append("Non-prohibited Lartas record must declare at least one required document or permit")
        return tuple(reasons)

    if kind == "export_lartas":
        hs8 = str(record.get("hs8") or "")
        hs8_digits = str(record.get("hs8_digits") or "")
        if not _HS8_RE.match(hs8) or hs8.replace(".", "") != hs8_digits:
            reasons.append(f"Invalid HS8 code in export Lartas record: {hs8!r}")
        mode = str(record.get("supervision_mode") or "")
        if mode not in {"RESTRICTED_EXPORT", "PROHIBITED_EXPORT"}:
            reasons.append(f"Invalid export Lartas supervision_mode: {mode!r}")
        if record.get("prohibited"):
            if mode != "PROHIBITED_EXPORT":
                reasons.append("Prohibited export Lartas record must use PROHIBITED_EXPORT supervision_mode")
        elif not (record.get("required_doc_types") or record.get("requires_et") or record.get("requires_pe") or record.get("requires_ls")):
            reasons.append("Non-prohibited export Lartas record must declare at least one required permit (ET/PE/LS)")
        return tuple(reasons)

    if kind == "export_duty":
        hs8 = str(record.get("hs8") or "")
        hs8_digits = str(record.get("hs8_digits") or "")
        if not _HS8_RE.match(hs8) or hs8.replace(".", "") != hs8_digits:
            reasons.append(f"Invalid HS8 code in export_duty record: {hs8!r}")
        basis = str(record.get("duty_basis") or "")
        if basis not in {"AD_VALOREM", "SPECIFIC_USD_PER_MT"}:
            reasons.append(f"Invalid export_duty duty_basis: {basis!r}")
        try:
            rate_dec = Decimal(str(record.get("rate_value")))
            if rate_dec < 0:
                reasons.append(f"Negative export_duty rate_value: {rate_dec}")
            if basis == "AD_VALOREM" and rate_dec > Decimal("100"):
                reasons.append(f"Ad-valorem export_duty rate > 100%: {rate_dec}")
        except (InvalidOperation, TypeError):
            reasons.append(f"Invalid decimal rate_value: {record.get('rate_value')!r}")
        hr_min = record.get("reference_price_min_exclusive")
        hr_max = record.get("reference_price_max_inclusive")
        if hr_min is not None and hr_max is not None:
            try:
                if Decimal(str(hr_min)) >= Decimal(str(hr_max)):
                    reasons.append(f"Invalid reference price range: min {hr_min} >= max {hr_max}")
            except InvalidOperation:
                reasons.append("Invalid decimal in reference price range")
        return tuple(reasons)

    if kind == "statute_clause":
        if not str(record.get("pasal") or "").strip():
            reasons.append("Missing pasal")
        if not str(record.get("text") or "").strip():
            reasons.append("Empty clause text")
        p_start, p_end = record.get("page_start"), record.get("page_end")
        if not isinstance(p_start, int) or not isinstance(p_end, int) or p_start < 1 or p_end < p_start:
            reasons.append("Invalid clause page range")
        return tuple(reasons)

    if kind == "guidance_section":
        if not str(record.get("heading") or "").strip() or not str(record.get("text") or "").strip():
            reasons.append("Missing guidance heading or text")
        return tuple(reasons)

    if kind == "regulation":
        if not str(record.get("number") or "").strip():
            reasons.append("Missing regulation number")
        if not str(record.get("title") or "").strip():
            reasons.append("Missing regulation title")
        if not str(record.get("url") or "").startswith(("http://", "https://")):
            reasons.append("Invalid regulation URL")
        for field_name in ("enacted_date", "promulgated_date", "effective_date"):
            val = record.get(field_name)
            if val is not None and not _ISO_DATE_RE.match(str(val)):
                reasons.append(f"Invalid ISO date for {field_name}: {val!r}")
        return tuple(reasons)

    return (f"Unsupported record_kind: {kind!r}",)


def verify_records(records: Iterable[dict], *, known_hs6: set[str] | None = None) -> VerificationSummary:
    """Partition records into cross-check verified items and flagged anomalies."""
    verified: list[dict] = []
    flagged: list[FlaggedRecord] = []
    for raw in records:
        item = raw.to_dict() if hasattr(raw, "to_dict") else raw
        reasons = verify_record(item, known_hs6=known_hs6)
        if reasons:
            flagged.append(FlaggedRecord(item, reasons))
        else:
            verified.append(item)
    return VerificationSummary(tuple(verified), tuple(flagged))


# --- JSONL I/O ---

def write_jsonl(path: Path, records: Iterable[object]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            payload = record.to_dict() if hasattr(record, "to_dict") else record
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> list[dict]:
    items: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                items.append(json.loads(stripped))
    return items
