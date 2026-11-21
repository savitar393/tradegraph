"""Scrapers for JDIH Kemenkeu and JDIH Kemendag regulation catalogs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
import json
from pathlib import Path
import re
import time
from typing import Callable, Literal
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

from tradegraph.contracts import SourceRecord
from tradegraph.sources.acquisition import acquire_source


USER_AGENT = "TradeGraph/0.1 (source catalog)"
KEMENKEU_BASE = "https://jdih.kemenkeu.go.id"
KEMENDAG_BASE = "https://jdih.kemendag.go.id"

MONTHS = {
    "january": 1, "januari": 1, "jan": 1,
    "february": 2, "februari": 2, "feb": 2,
    "march": 3, "maret": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5, "mei": 5,
    "june": 6, "juni": 6, "jun": 6,
    "july": 7, "juli": 7, "jul": 7,
    "august": 8, "agustus": 8, "agu": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oktober": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "desember": 12, "des": 12, "dec": 12,
}

type RelationKind = Literal[
    "AMENDS",
    "AMENDED_BY",
    "REVOKES",
    "REVOKED_BY",
    "PARTIALLY_REVOKES",
    "PARTIALLY_REVOKED_BY",
    "RELATED",
]
type FileRole = Literal["FULLTEXT_PDF", "ABSTRACT_PDF", "ATTACHMENT_PDF", "OTHER"]


@dataclass(frozen=True, slots=True)
class SearchHit:
    portal: Literal["jdih-kemenkeu", "jdih-kemendag"]
    slug: str
    url: str
    label: str


@dataclass(frozen=True, slots=True)
class RegulationFile:
    role: FileRole
    url: str
    label: str
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class RegulationRelation:
    relation_kind: RelationKind
    raw_label: str
    target_number: str | None
    target_title: str | None
    target_url: str | None
    effective_date: str | None = None


@dataclass(frozen=True, slots=True)
class ScrapedRegulation:
    portal: Literal["jdih-kemenkeu", "jdih-kemendag"]
    slug: str
    url: str
    number: str
    title: str
    authority: str
    regulation_type: str | None
    year: int | None
    enacted_date: str | None
    promulgated_date: str | None
    effective_date: str | None
    status: str | None
    subjects: tuple[str, ...]
    files: tuple[RegulationFile, ...]
    relations: tuple[RegulationRelation, ...]
    scraped_at: str
    record_kind: Literal["regulation"] = "regulation"

    def fulltext_pdf_url(self) -> str | None:
        for item in self.files:
            if item.role == "FULLTEXT_PDF":
                return item.url
        return None

    def to_dict(self) -> dict:
        return asdict(self)


def _fetch_html(url: str, *, timeout: int = 25) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as error:
        if error.code in {403, 429, 503}:
            raise OSError(f"Portal blocked or rate-limited request ({error.code}) for {url}") from error
        raise


def _normalize_relation(code: str | None, label: str | None) -> RelationKind:
    code_norm = (code or "").strip().upper()
    label_norm = re.sub(r"\s+", " ", (label or "").strip().lower())
    # Check label direction first because JDIH Kemenkeu reuses active codes (e.g. MSB) for passive labels.
    if "dicabut sebagian" in label_norm:
        return "PARTIALLY_REVOKED_BY"
    if "mencabut sebagian" in label_norm:
        return "PARTIALLY_REVOKES"
    if label_norm.startswith("dicabut"):
        return "REVOKED_BY"
    if label_norm.startswith("mencabut"):
        return "REVOKES"
    if label_norm.startswith("diubah"):
        return "AMENDED_BY"
    if label_norm.startswith("mengubah"):
        return "AMENDS"
    if code_norm == "DSB":
        return "PARTIALLY_REVOKED_BY"
    if code_norm == "MSB":
        return "PARTIALLY_REVOKES"
    if code_norm == "DCB":
        return "REVOKED_BY"
    if code_norm == "MCB":
        return "REVOKES"
    if code_norm == "DGB":
        return "AMENDED_BY"
    if code_norm == "MGB":
        return "AMENDS"
    return "RELATED"


def _parse_iso_rsc_date(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.removeprefix("$D").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", cleaned)
    if not match or match.group(1).startswith("1900-"):
        return None
    return match.group(1)


def _parse_human_date(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"\s+", " ", raw.strip())
    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if iso:
        return text
    match = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$", text)
    if not match:
        return None
    day, month_name, year = int(match.group(1)), match.group(2).lower(), int(match.group(3))
    month = MONTHS.get(month_name)
    if month is None:
        return None
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _extract_balanced_json(text: str, start_brace: int) -> dict | None:
    if start_brace < 0 or start_brace >= len(text) or text[start_brace] != "{":
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start_brace, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start_brace : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _extract_kemenkeu_rsc_data(html: str) -> dict | None:
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html, re.S)
    decoded_parts: list[str] = []
    for chunk in chunks:
        try:
            decoded_parts.append(json.loads(f'"{chunk}"'))
        except json.JSONDecodeError:
            continue
    joined = "".join(decoded_parts)
    for match in re.finditer(r'"data"\s*:\s*\{', joined):
        brace = joined.find("{", match.start())
        candidate = _extract_balanced_json(joined, brace)
        if isinstance(candidate, dict) and "Nomor" in candidate and "Judul" in candidate:
            return candidate
    return None


def parse_kemenkeu_search_html(html: str, *, base_url: str = KEMENKEU_BASE) -> list[SearchHit]:
    soup = BeautifulSoup(html, "lxml")
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        match = re.match(r"^(?:https?://[^/]+)?/dok/([^/?#]+?)(?:/summary)?/?$", href)
        if not match:
            continue
        slug = match.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        hits.append(SearchHit(
            portal="jdih-kemenkeu",
            slug=slug,
            url=urljoin(base_url, f"/dok/{slug}/summary"),
            label=label or slug,
        ))
    return hits


def parse_kemenkeu_detail_html(
    html: str,
    url: str,
    *,
    scraped_at: str | None = None,
    base_url: str = KEMENKEU_BASE,
) -> ScrapedRegulation:
    data = _extract_kemenkeu_rsc_data(html)
    timestamp = scraped_at or datetime.now(UTC).isoformat()
    if data is None:
        raise ValueError(f"Could not locate JDIH Kemenkeu regulation payload in {url}")

    puu = data.get("PUU") or {}
    jenis = data.get("Jenis") or {}
    slug = str(data.get("Slug") or urlsplit(url).path.strip("/").split("/")[-2]).strip()
    number = str(data.get("Nomor") or slug).strip()
    title = re.sub(r"\s+", " ", str(data.get("Judul") or "").strip())
    authority = str(data.get("TEU") or "Kementerian Keuangan Republik Indonesia").strip()
    if authority.lower() == "kementerian keuangan":
        authority = "Kementerian Keuangan Republik Indonesia"

    abstract_ids = {
        (item.get("MediaCatalog") or {}).get("Id")
        for item in (data.get("File") or [])
        if item.get("Jenis") == 3
    }
    files: list[RegulationFile] = []
    seen_file_keys: set[tuple[str, str]] = set()
    for item in data.get("File") or []:
        jenis_file = item.get("Jenis")
        media = item.get("MediaCatalog") or {}
        media_id = media.get("Id")
        path = media.get("Path") or media.get("FileName")
        ext = str(media.get("Ext") or "").lower()
        if not media_id or not path or jenis_file == 4 or ext in {".jpg", ".jpeg", ".png"}:
            continue
        if jenis_file == 1 and media_id in abstract_ids:
            continue
        role: FileRole = (
            "FULLTEXT_PDF" if jenis_file == 1
            else "ABSTRACT_PDF" if jenis_file == 3
            else "ATTACHMENT_PDF" if jenis_file == 2
            else "OTHER"
        )
        dedup_key = (role, str(media_id))
        if dedup_key in seen_file_keys:
            continue
        seen_file_keys.add(dedup_key)
        encoded_path = quote(str(path).lstrip("/"), safe="/~.-_")
        files.append(RegulationFile(
            role=role,
            url=f"{base_url.rstrip('/')}/api/download/{media_id}/{encoded_path}",
            label=str(item.get("Keterangan") or role).strip(),
            filename=str(media.get("FileName") or path).strip(),
        ))

    relations: list[RegulationRelation] = []
    for rel in data.get("Relasi") or []:
        rel_slug = rel.get("Slug")
        rel_url = urljoin(base_url, f"/dok/{rel_slug}/summary") if rel_slug else None
        raw_label = re.sub(r"\s+", " ", str(rel.get("Nama") or rel.get("KodeRelasi") or "").strip())
        relations.append(RegulationRelation(
            relation_kind=_normalize_relation(rel.get("KodeRelasi"), raw_label),
            raw_label=raw_label,
            target_number=(str(rel["Nomor"]).strip() if rel.get("Nomor") else None),
            target_title=(re.sub(r"\s+", " ", str(rel["Judul"]).strip()) if rel.get("Judul") else None),
            target_url=rel_url,
            effective_date=_parse_iso_rsc_date(rel.get("TglMulaiBerlaku")),
        ))

    subjects = tuple(dict.fromkeys(
        re.sub(r"\s+", " ", str(lbl.get("Teks") or "").strip())
        for lbl in (data.get("Label") or [])
        if str(lbl.get("Teks") or "").strip()
    ))
    year_val = puu.get("Tahun")
    year = int(year_val) if isinstance(year_val, int) and year_val > 1900 else None
    if year is None:
        m = re.search(r"\b(19|20)\d{2}\b", number)
        year = int(m.group(0)) if m else None

    return ScrapedRegulation(
        portal="jdih-kemenkeu",
        slug=slug,
        url=url,
        number=number,
        title=title,
        authority=authority,
        regulation_type=(str(jenis.get("Keterangan")).strip() if jenis.get("Keterangan") else None),
        year=year,
        enacted_date=_parse_iso_rsc_date(puu.get("TglPenetapan") or data.get("Tanggal")),
        promulgated_date=_parse_iso_rsc_date(puu.get("TglPengundangan")),
        effective_date=_parse_iso_rsc_date(puu.get("TglMulaiBerlaku")),
        status=(str(data.get("Status")).strip() if data.get("Status") else None),
        subjects=subjects,
        files=tuple(files),
        relations=tuple(relations),
        scraped_at=timestamp,
    )


def parse_kemendag_search_html(html: str, *, base_url: str = KEMENDAG_BASE) -> list[SearchHit]:
    soup = BeautifulSoup(html, "lxml")
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"].strip())
        parsed = urlsplit(href)
        path = parsed.path.rstrip("/")
        if not path.startswith("/peraturan/") or path.startswith("/peraturan/download/"):
            continue
        slug = path.removeprefix("/peraturan/")
        if "/" in slug or not slug or slug in seen:
            continue
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        if not label:
            continue
        seen.add(slug)
        hits.append(SearchHit(portal="jdih-kemendag", slug=slug, url=href, label=label))
    return hits


def parse_kemendag_detail_html(
    html: str,
    url: str,
    *,
    scraped_at: str | None = None,
    base_url: str = KEMENDAG_BASE,
) -> ScrapedRegulation:
    soup = BeautifulSoup(html, "lxml")
    timestamp = scraped_at or datetime.now(UTC).isoformat()

    meta: dict[str, str] = {}
    relations: list[RegulationRelation] = []
    active_relation_label: str | None = None

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            key = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True))
            val = re.sub(r"\s+", " ", cells[1].get_text(" ", strip=True))
            if key.lower() == "keterangan status":
                active_relation_label = val if val and val != "-" else None
            else:
                active_relation_label = None
                if key and key.lower() != "meta":
                    meta[key] = val
        elif len(cells) == 1 and active_relation_label:
            cell = cells[0]
            text = re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
            anchor = cell.find("a", href=True)
            target_url = urljoin(base_url, anchor["href"].strip()) if anchor else None
            num_match = re.search(r"Nomor\s+(\S+\s+Tahun\s+\d{4})", text, re.I)
            if text and text != "-":
                relations.append(RegulationRelation(
                    relation_kind=_normalize_relation(None, active_relation_label),
                    raw_label=active_relation_label,
                    target_number=(num_match.group(1) if num_match else None),
                    target_title=text,
                    target_url=target_url,
                ))

    title = meta.get("Judul") or (soup.title.get_text(" ", strip=True) if soup.title else "")
    if not title:
        raise ValueError(f"Could not extract title from JDIH Kemendag detail page {url}")

    slug = urlsplit(url).path.rstrip("/").split("/")[-1]
    raw_number = meta.get("Nomor Peraturan") or ""
    year_match = re.search(r"Tahun\s+(\d{4})", title, re.I) or re.search(r"\b(19|20)\d{2}\b", slug)
    year = int(year_match.group(1) if year_match and year_match.lastindex else year_match.group(0)) if year_match else None
    reg_type = meta.get("Jenis / Bentuk Peraturan")
    if raw_number and year and str(year) not in raw_number:
        prefix = "Permendag" if (reg_type or "").lower().startswith("peraturan menteri") else (reg_type or "Peraturan")
        number = f"{prefix} {raw_number} Tahun {year}"
    else:
        number = raw_number or slug

    enacted_date: str | None = None
    promulgated_date: str | None = None
    date_pair = meta.get("Tanggal Penetapan / Pengundangan") or ""
    if "/" in date_pair:
        left, right = [part.strip() for part in date_pair.split("/", 1)]
        enacted_date = _parse_human_date(left)
        promulgated_date = _parse_human_date(right)
    elif date_pair:
        enacted_date = _parse_human_date(date_pair)

    authority = meta.get("T.E.U. Badan/Pengarang") or "Kementerian Perdagangan Republik Indonesia"
    if "kementerian perdagangan" in authority.lower():
        authority = "Kementerian Perdagangan Republik Indonesia"

    subjects_raw = meta.get("Subjek") or ""
    subjects = tuple(
        part.strip() for part in re.split(r"\s+-\s+|;", subjects_raw)
        if part.strip() and part.strip() != "-"
    )

    files: list[RegulationFile] = []
    seen_urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"].strip())
        if "/peraturan/download/" not in href or href in seen_urls:
            continue
        seen_urls.add(href)
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
        lower_href = href.lower()
        role: FileRole = (
            "FULLTEXT_PDF" if lower_href.endswith("/file_peraturan")
            else "ABSTRACT_PDF" if lower_href.endswith("/file_abstract")
            else "ATTACHMENT_PDF" if "lampiran" in lower_href or "lampiran" in label.lower()
            else "OTHER"
        )
        files.append(RegulationFile(role=role, url=href, label=label or role, filename=None))

    return ScrapedRegulation(
        portal="jdih-kemendag",
        slug=slug,
        url=url,
        number=number,
        title=title,
        authority=authority,
        regulation_type=reg_type,
        year=year,
        enacted_date=enacted_date,
        promulgated_date=promulgated_date,
        effective_date=None,
        status=meta.get("Status Peraturan"),
        subjects=subjects,
        files=tuple(files),
        relations=tuple(relations),
        scraped_at=timestamp,
    )


class JdihKemenkeuScraper:
    def __init__(self, *, fetcher: Callable[[str], str] = _fetch_html, delay_seconds: float = 0.5):
        self.fetcher = fetcher
        self.delay_seconds = delay_seconds

    def search(
        self,
        query: str,
        *,
        bentuk: str = "Peraturan Menteri",
        limit: int = 5,
    ) -> list[SearchHit]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        params = urlencode({"q": query, "bentuk": bentuk, "order": "desc"})
        html = self.fetcher(f"{KEMENKEU_BASE}/search?{params}")
        return parse_kemenkeu_search_html(html)[:limit]

    def fetch_detail(self, slug_or_url: str) -> ScrapedRegulation:
        url = (
            slug_or_url
            if slug_or_url.startswith(("http://", "https://"))
            else f"{KEMENKEU_BASE}/dok/{slug_or_url.strip('/')}/summary"
        )
        return parse_kemenkeu_detail_html(self.fetcher(url), url)

    def scrape(self, query: str, *, bentuk: str = "Peraturan Menteri", limit: int = 5) -> list[ScrapedRegulation]:
        hits = self.search(query, bentuk=bentuk, limit=limit)
        results: list[ScrapedRegulation] = []
        for index, hit in enumerate(hits):
            if index and self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
            results.append(self.fetch_detail(hit.url))
        return results


class JdihKemendagScraper:
    def __init__(self, *, fetcher: Callable[[str], str] = _fetch_html, delay_seconds: float = 0.5):
        self.fetcher = fetcher
        self.delay_seconds = delay_seconds

    def search(
        self,
        query: str,
        *,
        tahun: int | None = None,
        status: str | None = None,
        limit: int = 5,
    ) -> list[SearchHit]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        query_params: dict[str, str] = {"search": query}
        if tahun is not None:
            query_params["tahunreg"] = str(tahun)
        if status:
            query_params["status"] = status
        html = self.fetcher(f"{KEMENDAG_BASE}/peraturan?{urlencode(query_params)}")
        return parse_kemendag_search_html(html)[:limit]

    def fetch_detail(self, url: str) -> ScrapedRegulation:
        return parse_kemendag_detail_html(self.fetcher(url), url)

    def scrape(
        self,
        query: str,
        *,
        tahun: int | None = None,
        status: str | None = None,
        limit: int = 5,
    ) -> list[ScrapedRegulation]:
        hits = self.search(query, tahun=tahun, status=status, limit=limit)
        results: list[ScrapedRegulation] = []
        for index, hit in enumerate(hits):
            if index and self.delay_seconds > 0:
                time.sleep(self.delay_seconds)
            results.append(self.fetch_detail(hit.url))
        return results


def archive_regulation(regulation: ScrapedRegulation, destination: Path) -> SourceRecord | None:
    """Archive a scraped regulation's primary fulltext PDF via acquire_source."""
    pdf_url = regulation.fulltext_pdf_url()
    if not pdf_url:
        return None
    record = acquire_source(pdf_url, destination)
    return SourceRecord(
        id=record.id,
        url=record.url,
        authority=regulation.authority,
        acquired_at=record.acquired_at,
        content_hash=record.content_hash,
        local_ref=record.local_ref,
        status=record.status,
        effective_period=record.effective_period,
    )
