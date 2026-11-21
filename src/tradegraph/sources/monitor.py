"""Automated JDIH regulatory freshness monitor, semantic diff engine, and trial OWL staging pipeline."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from hashlib import file_digest, sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Literal

from rdflib import Graph, OWL, RDF, RDFS, URIRef, XSD

from tradegraph.contracts import CompiledKB
from tradegraph.kb.loader import ENGINE_PROFILE_ID, TG
from tradegraph.kb.rdf import parse_turtle
from tradegraph.sources.parsers import write_jsonl
from tradegraph.sources.rdf_staging import (
    CATALOG,
    SH,
    SWRL,
    SWRLB,
    StagingBuildSummary,
    TrialValidationResult,
    _infer_kind_from_url,
    _normalize_reg_ref,
    _slugify,
    generate_staging_turtle,
    validate_trial_merge,
)
from tradegraph.sources.scrapers import (
    JdihKemendagScraper,
    JdihKemenkeuScraper,
    RegulationFile,
    RegulationRelation,
    ScrapedRegulation,
    archive_regulation,
)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_KB_RELATION_KIND_MAP: dict[str, str] = {
    "relAmends": "AMENDS",
    "relRevokes": "REVOKES",
    "relPartiallyRevokes": "PARTIALLY_REVOKES",
    "relRelated": "RELATED",
}

_SEVERITY_ORDER: dict[str, int] = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "INFO": 4,
}

_FINDING_STATUS_ORDER: dict[str, int] = {
    "NEW_UNTRACKED": 0,
    "RELATION_DRIFT": 1,
    "KB_ONLY_UNARCHIVED": 2,
    "METADATA_DRIFT": 3,
    "UP_TO_DATE": 4,
}

type FindingStatus = Literal[
    "NEW_UNTRACKED",
    "KB_ONLY_UNARCHIVED",
    "RELATION_DRIFT",
    "METADATA_DRIFT",
    "UP_TO_DATE",
]
type SeverityLevel = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]


@dataclass(frozen=True, slots=True)
class WatchTarget:
    """Bounded, domain-scoped JDIH search target."""

    target_id: str
    portal: Literal["kemenkeu", "kemendag"]
    query: str
    category: str
    description: str
    bentuk: str = "Peraturan Menteri"
    tahun: int | None = None
    status: str | None = None
    tracked_keys: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "portal": self.portal,
            "query": self.query,
            "category": self.category,
            "description": self.description,
            "bentuk": self.bentuk if self.portal == "kemenkeu" else None,
            "tahun": self.tahun,
            "status": self.status,
            "tracked_keys": list(self.tracked_keys),
        }


DEFAULT_WATCHLIST: tuple[WatchTarget, ...] = (
    WatchTarget(
        target_id="bea-keluar",
        portal="kemenkeu",
        query="bea keluar",
        category="EXPORT_DUTY",
        description="PMK penetapan barang ekspor yang dikenakan Bea Keluar dan tarif Bea Keluar",
        tracked_keys=("ID-BEA-KELUAR", "ID-BEA-KELUAR-AMEND25"),
    ),
    WatchTarget(
        target_id="tarif-fta",
        portal="kemenkeu",
        query="tarif bea masuk dalam rangka",
        category="PREFERENTIAL_TARIFF",
        description="PMK penetapan tarif bea masuk preferensi (ATIGA, ACFTA, IJEPA, IK-CEPA, IA-CEPA, RCEP)",
        tracked_keys=(
            "ID-TARIF-ATIGA",
            "ID-TARIF-ACFTA",
            "ID-TARIF-IJEPA",
            "ID-TARIF-IKCEPA",
            "ID-TARIF-IACEPA",
            "ID-TARIF-RCEP-ASEAN",
            "ID-TARIF-RCEP-CN",
            "ID-TARIF-RCEP-JP",
            "ID-TARIF-RCEP-AU",
            "ID-TARIF-RCEP-KR",
        ),
    ),
    WatchTarget(
        target_id="asal-barang-ska",
        portal="kemenkeu",
        query="surat keterangan asal",
        category="ORIGIN_PROCEDURE",
        description="PMK tata cara pengenaan tarif preferensi, SKA, DAB, dan ketentuan asal barang",
        tracked_keys=(
            "ID-SKA-DAB",
            "ID-ASAL-ATIGA",
            "ID-ASAL-ATIGA-AMEND",
            "ID-ASAL-RCEP",
            "ID-ASAL-ACFTA",
            "ID-ASAL-IJEPA",
            "ID-ASAL-IKCEPA",
            "ID-ASAL-IACEPA",
        ),
    ),
    WatchTarget(
        target_id="btki-mfn",
        portal="kemenkeu",
        query="sistem klasifikasi barang",
        category="MFN_TARIFF",
        description="PMK BTKI dan pembebanan tarif bea masuk MFN atas barang impor",
        tracked_keys=("ID-TARIF-MFN",),
    ),
    WatchTarget(
        target_id="lartas-impor",
        portal="kemendag",
        query="kebijakan dan pengaturan impor",
        category="IMPORT_LARTAS",
        description="Permendag kebijakan dan pengaturan impor (umum, tekstil, pertanian, elektronik, industri)",
        tracked_keys=(
            "ID-IMPOR-GENERAL",
            "ID-LARTAS-TEKSTIL",
            "ID-LARTAS-PERTANIAN",
            "ID-LARTAS-ELEKTRONIK",
            "ID-LARTAS-INDUSTRI",
        ),
    ),
    WatchTarget(
        target_id="larangan-impor",
        portal="kemendag",
        query="barang yang dilarang untuk diimpor",
        category="IMPORT_PROHIBITION",
        description="Permendag daftar barang yang dilarang untuk diimpor",
        tracked_keys=("ID-LARTAS-LARANGAN",),
    ),
    WatchTarget(
        target_id="lartas-ekspor",
        portal="kemendag",
        query="kebijakan dan pengaturan ekspor",
        category="EXPORT_LARTAS",
        description="Permendag kebijakan dan pengaturan ekspor (ET, PE, LS Ekspor)",
        tracked_keys=("ID-LARTAS-EKSPOR", "ID-LARTAS-EKSPOR-AMEND25"),
    ),
    WatchTarget(
        target_id="larangan-ekspor",
        portal="kemendag",
        query="barang yang dilarang untuk diekspor",
        category="EXPORT_PROHIBITION",
        description="Permendag daftar barang yang dilarang untuk diekspor",
        tracked_keys=("ID-LARTAS-EKSPOR-LARANGAN", "ID-LARTAS-EKSPOR-LARANGAN-AMEND25"),
    ),
)

WATCHLIST_BY_ID: dict[str, WatchTarget] = {item.target_id: item for item in DEFAULT_WATCHLIST}


@dataclass(frozen=True, slots=True)
class KbSourceMeta:
    iri: str
    reg_ref: str | None
    source_key: str | None
    regulation_number: str | None
    label: str | None
    effective_from: str | None
    status: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class BaselineRegulationIndex:
    """In-memory normalized baseline of sources/manifest.json and knowledge/tradegraph.ttl."""

    manifest_path: Path
    canonical_kb_path: Path
    kb_version: str | None
    kb_sha256: str
    manifest_by_key: dict[str, dict]
    manifest_by_ref: dict[str, dict]
    manifest_by_url: dict[str, dict]
    references_without_snapshot: tuple[dict, ...]
    kb_docs_by_ref: dict[str, KbSourceMeta]
    kb_docs_by_key: dict[str, KbSourceMeta]
    kb_relations: frozenset[tuple[str, str, str]]
    kb_relation_dates: dict[tuple[str, str, str], str | None]
    kb_triples: frozenset[tuple]

    def tracked_keys_for_ref(self, reg_ref: str | None) -> tuple[str, ...]:
        if not reg_ref:
            return ()
        keys: list[str] = []
        manifest_entry = self.manifest_by_ref.get(reg_ref)
        if manifest_entry and manifest_entry.get("key"):
            keys.append(str(manifest_entry["key"]))
        kb_meta = self.kb_docs_by_ref.get(reg_ref)
        if kb_meta and kb_meta.source_key and kb_meta.source_key not in keys:
            keys.append(kb_meta.source_key)
        return tuple(keys)


@dataclass(frozen=True, slots=True)
class ManifestFreshnessItem:
    key: str
    reg_ref: str | None
    title: str
    effective_from: str | None
    acquired_at: str | None
    age_days: int | None
    local_ref: str | None
    file_exists: bool
    hash_verified: bool | None
    freshness_status: str  # CURRENT | REVOKED | UPCOMING_REVOCATION | AMENDED_GAP | AMENDED | STALE | MISSING_ARCHIVE | HASH_MISMATCH
    severity: SeverityLevel
    lineage_alerts: tuple[str, ...]
    url: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class UnarchivedKbGap:
    reg_ref: str
    kb_document_iri: str
    source_key: str | None
    regulation_number: str | None
    label: str | None
    effective_from: str | None
    url: str | None
    relation_kind: str
    impacts_tracked_keys: tuple[str, ...]
    active_on_date: bool
    severity: SeverityLevel
    recommended_action: str

    def to_dict(self) -> dict[str, object]:
        return {
            "reg_ref": self.reg_ref,
            "kb_document_iri": self.kb_document_iri,
            "source_key": self.source_key,
            "regulation_number": self.regulation_number,
            "label": self.label,
            "effective_from": self.effective_from,
            "url": self.url,
            "relation_kind": self.relation_kind,
            "impacts_tracked_keys": list(self.impacts_tracked_keys),
            "active_on_date": self.active_on_date,
            "severity": self.severity,
            "recommended_action": self.recommended_action,
        }


@dataclass(frozen=True, slots=True)
class LocalFreshnessAudit:
    as_of_date: str
    stale_days: int
    kb_version: str | None
    kb_sha256: str
    overall_health: Literal["HEALTHY", "ATTENTION", "ACTION_REQUIRED"]
    manifest_count: int
    kb_documents_count: int
    kb_relations_count: int
    archived_files_missing: int
    hash_mismatches: int
    revoked_active_count: int
    upcoming_revocation_count: int
    amended_unarchived_gap_count: int
    stale_count: int
    references_without_snapshot_count: int
    unarchived_kb_gaps: tuple[UnarchivedKbGap, ...]
    manifest_items: tuple[ManifestFreshnessItem, ...]
    references_without_snapshot: tuple[dict, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "as_of_date": self.as_of_date,
            "stale_days": self.stale_days,
            "kb_version": self.kb_version,
            "kb_sha256": self.kb_sha256,
            "overall_health": self.overall_health,
            "summary": {
                "manifest_count": self.manifest_count,
                "kb_documents_count": self.kb_documents_count,
                "kb_relations_count": self.kb_relations_count,
                "archived_files_missing": self.archived_files_missing,
                "hash_mismatches": self.hash_mismatches,
                "revoked_active_count": self.revoked_active_count,
                "upcoming_revocation_count": self.upcoming_revocation_count,
                "amended_unarchived_gap_count": self.amended_unarchived_gap_count,
                "stale_count": self.stale_count,
                "references_without_snapshot_count": self.references_without_snapshot_count,
            },
            "unarchived_kb_gaps": [item.to_dict() for item in self.unarchived_kb_gaps],
            "manifest_items": [item.to_dict() for item in self.manifest_items],
            "references_without_snapshot": list(self.references_without_snapshot),
        }


@dataclass(frozen=True, slots=True)
class DetectedRelationDiff:
    acting_ref: str
    relation_kind: str  # AMENDS | REVOKES | PARTIALLY_REVOKES | RELATED
    target_ref: str
    effective_date: str | None
    raw_label: str
    impacts_tracked_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "acting_ref": self.acting_ref,
            "relation_kind": self.relation_kind,
            "target_ref": self.target_ref,
            "effective_date": self.effective_date,
            "raw_label": self.raw_label,
            "impacts_tracked_keys": list(self.impacts_tracked_keys),
        }


@dataclass(frozen=True, slots=True)
class MonitorFinding:
    portal: str
    slug: str
    reg_ref: str | None
    number: str
    title: str
    url: str
    fulltext_pdf_url: str | None
    effective_date: str | None
    status_label: str | None
    finding_status: FindingStatus
    severity: SeverityLevel
    matched_targets: tuple[str, ...]
    manifest_key: str | None
    kb_document_iri: str | None
    impacts_tracked_keys: tuple[str, ...]
    new_relations: tuple[DetectedRelationDiff, ...]
    metadata_diffs: tuple[str, ...]
    recommended_action: str
    regulation: dict[str, object]

    def to_dict(self, *, include_regulation: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "portal": self.portal,
            "slug": self.slug,
            "reg_ref": self.reg_ref,
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "fulltext_pdf_url": self.fulltext_pdf_url,
            "effective_date": self.effective_date,
            "status_label": self.status_label,
            "finding_status": self.finding_status,
            "severity": self.severity,
            "matched_targets": list(self.matched_targets),
            "manifest_key": self.manifest_key,
            "kb_document_iri": self.kb_document_iri,
            "impacts_tracked_keys": list(self.impacts_tracked_keys),
            "new_relations": [rel.to_dict() for rel in self.new_relations],
            "metadata_diffs": list(self.metadata_diffs),
            "recommended_action": self.recommended_action,
        }
        if include_regulation:
            payload["regulation"] = self.regulation
        return payload


@dataclass(frozen=True, slots=True)
class TargetScanError:
    target_id: str
    portal: str
    query: str
    error: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MonitorScanReport:
    scanned_at: str
    as_of_date: str
    mode: Literal["live", "replay"]
    kb_version: str | None
    kb_sha256: str
    targets_checked: tuple[str, ...]
    total_scanned: int
    actionable_count: int
    critical_count: int
    high_count: int
    medium_count: int
    up_to_date_count: int
    status_counts: dict[str, int]
    target_errors: tuple[TargetScanError, ...]
    findings: tuple[MonitorFinding, ...]

    def to_dict(self, *, include_regulations: bool = False) -> dict[str, object]:
        return {
            "scanned_at": self.scanned_at,
            "as_of_date": self.as_of_date,
            "mode": self.mode,
            "kb_version": self.kb_version,
            "kb_sha256": self.kb_sha256,
            "targets_checked": list(self.targets_checked),
            "summary": {
                "total_scanned": self.total_scanned,
                "actionable_count": self.actionable_count,
                "critical_count": self.critical_count,
                "high_count": self.high_count,
                "medium_count": self.medium_count,
                "up_to_date_count": self.up_to_date_count,
                "status_counts": dict(self.status_counts),
                "target_error_count": len(self.target_errors),
            },
            "target_errors": [err.to_dict() for err in self.target_errors],
            "findings": [item.to_dict(include_regulation=include_regulations) for item in self.findings],
        }


@dataclass(frozen=True, slots=True)
class StagingDiffResult:
    candidates_selected: int
    staging_summary: StagingBuildSummary | None
    canonical_triples: int
    staging_triples: int
    net_new_triples: int
    delta_turtle: str
    manifest_patch: tuple[dict[str, object], ...]
    archived_pdfs: tuple[dict[str, object], ...]
    archive_errors: tuple[str, ...]
    validation: TrialValidationResult | None
    written_files: dict[str, str]

    def to_dict(self, *, include_turtle: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "candidates_selected": self.candidates_selected,
            "canonical_triples": self.canonical_triples,
            "staging_triples": self.staging_triples,
            "net_new_triples": self.net_new_triples,
            "staging_summary": (
                {
                    "records_processed": self.staging_summary.records_processed,
                    "verified_records": self.staging_summary.verified_records,
                    "flagged_records": self.staging_summary.flagged_records,
                    "flagged_diagnostics": list(self.staging_summary.flagged_diagnostics),
                    "triples_generated": self.staging_summary.triples_generated,
                    "source_documents_emitted": self.staging_summary.source_documents_emitted,
                    "legal_relations_emitted": self.staging_summary.legal_relations_emitted,
                }
                if self.staging_summary is not None
                else None
            ),
            "manifest_patch": list(self.manifest_patch),
            "archived_pdfs": list(self.archived_pdfs),
            "archive_errors": list(self.archive_errors),
            "validation": (
                {
                    "kb_compiled": self.validation.kb_compiled,
                    "owl_validated": self.validation.owl_validated,
                    "canonical_sha256": self.validation.canonical_sha256,
                    "merged_sha256": self.validation.merged_sha256,
                    "canonical_owl_triples": self.validation.canonical_owl_triples,
                    "merged_owl_triples": self.validation.merged_owl_triples,
                    "diagnostics": list(self.validation.diagnostics),
                }
                if self.validation is not None
                else None
            ),
            "written_files": dict(self.written_files),
        }
        if include_turtle:
            payload["delta_turtle"] = self.delta_turtle
        return payload


def _validate_iso_date(value: str | None) -> str:
    if value is None or not value.strip():
        return date.today().isoformat()
    cleaned = value.strip()
    if not _ISO_DATE_RE.match(cleaned):
        raise ValueError(f"Date must use YYYY-MM-DD format: {value!r}")
    date.fromisoformat(cleaned)
    return cleaned


def _portal_default_kind(portal: str | None, url: str | None = None) -> str:
    low = (portal or "").lower()
    if "kemendag" in low:
        return "permendag"
    if "kemenkeu" in low:
        return "pmk"
    return _infer_kind_from_url(url)


def _normalize_scraped_ref(reg: dict[str, object]) -> str | None:
    default_kind = _portal_default_kind(str(reg.get("portal") or ""), str(reg.get("url") or ""))
    number = str(reg.get("number") or "")
    title = str(reg.get("title") or "")
    slug = str(reg.get("slug") or "")
    return (
        _normalize_reg_ref(number, default_kind=default_kind)
        or _normalize_reg_ref(f"{number} {title}", default_kind=default_kind)
        or _normalize_reg_ref(slug.replace("-", " "), default_kind=default_kind)
    )


def _canonicalize_relation(
    source_ref: str,
    relation_kind: str,
    target_ref: str,
) -> tuple[str, str, str]:
    kind = relation_kind.strip().upper()
    if kind == "AMENDED_BY":
        return (target_ref, "AMENDS", source_ref)
    if kind == "REVOKED_BY":
        return (target_ref, "REVOKES", source_ref)
    if kind == "PARTIALLY_REVOKED_BY":
        return (target_ref, "PARTIALLY_REVOKES", source_ref)
    if kind in {"AMENDS", "REVOKES", "PARTIALLY_REVOKES", "RELATED"}:
        return (source_ref, kind, target_ref)
    return (source_ref, "RELATED", target_ref)


def _resolve_doc_ref_in_graph(graph: Graph, subj: URIRef) -> str | None:
    see_also = graph.value(subj, RDFS.seeAlso)
    default_kind = _infer_kind_from_url(str(see_also) if see_also is not None else None)
    for pred in (TG.regulationNumber, RDFS.label):
        for val in graph.objects(subj, pred):
            ref = _normalize_reg_ref(str(val), default_kind=default_kind)
            if ref:
                return ref
    return None


def _coerce_regulation_dict(item: ScrapedRegulation | dict[str, object]) -> dict[str, object]:
    raw = item.to_dict() if isinstance(item, ScrapedRegulation) else dict(item)
    raw.setdefault("record_kind", "regulation")
    raw.setdefault("files", [])
    raw.setdefault("relations", [])
    raw.setdefault("subjects", [])
    return raw


def _extract_fulltext_pdf_url(reg: dict[str, object]) -> str | None:
    for entry in reg.get("files") or ():
        if isinstance(entry, dict) and entry.get("role") == "FULLTEXT_PDF" and entry.get("url"):
            return str(entry["url"])
    return None


def _dict_to_scraped_regulation(reg: dict[str, object]) -> ScrapedRegulation:
    files = tuple(
        RegulationFile(
            role=f.get("role", "OTHER"),
            url=str(f["url"]),
            label=str(f.get("label") or f.get("role") or "OTHER"),
            filename=str(f["filename"]) if f.get("filename") else None,
        )
        for f in (reg.get("files") or ())
        if isinstance(f, dict) and f.get("url")
    )
    relations = tuple(
        RegulationRelation(
            relation_kind=r.get("relation_kind", "RELATED"),
            raw_label=str(r.get("raw_label") or ""),
            target_number=str(r["target_number"]) if r.get("target_number") else None,
            target_title=str(r["target_title"]) if r.get("target_title") else None,
            target_url=str(r["target_url"]) if r.get("target_url") else None,
            effective_date=str(r["effective_date"]) if r.get("effective_date") else None,
        )
        for r in (reg.get("relations") or ())
        if isinstance(r, dict)
    )
    portal_val = str(reg.get("portal") or "jdih-kemenkeu")
    portal: Literal["jdih-kemenkeu", "jdih-kemendag"] = (
        "jdih-kemendag" if "kemendag" in portal_val.lower() else "jdih-kemenkeu"
    )
    year_raw = reg.get("year")
    year = int(year_raw) if isinstance(year_raw, int) else None
    return ScrapedRegulation(
        portal=portal,
        slug=str(reg.get("slug") or _slugify(str(reg.get("number") or "regulation"))),
        url=str(reg.get("url") or ""),
        number=str(reg.get("number") or ""),
        title=str(reg.get("title") or ""),
        authority=str(
            reg.get("authority")
            or (
                "Kementerian Perdagangan Republik Indonesia"
                if portal == "jdih-kemendag"
                else "Kementerian Keuangan Republik Indonesia"
            )
        ),
        regulation_type=str(reg["regulation_type"]) if reg.get("regulation_type") else None,
        year=year,
        enacted_date=str(reg["enacted_date"]) if reg.get("enacted_date") else None,
        promulgated_date=str(reg["promulgated_date"]) if reg.get("promulgated_date") else None,
        effective_date=str(reg["effective_date"]) if reg.get("effective_date") else None,
        status=str(reg["status"]) if reg.get("status") else None,
        subjects=tuple(str(s) for s in (reg.get("subjects") or ())),
        files=files,
        relations=relations,
        scraped_at=str(reg.get("scraped_at") or datetime.now(UTC).isoformat()),
    )


def _propose_manifest_key(reg: dict[str, object], reg_ref: str | None) -> str:
    if reg_ref:
        parts = reg_ref.split("-")
        if len(parts) == 3:
            prefix = "ID-PMK" if parts[0] == "pmk" else ("ID-PERMENDAG" if parts[0] == "permendag" else "ID-REG")
            return f"{prefix}-{parts[1]}-{parts[2]}"
    portal = str(reg.get("portal") or "JDIH").upper().replace("-", "_")
    slug = _slugify(str(reg.get("number") or reg.get("slug") or "candidate"), max_len=24).upper()
    return f"{portal}-{slug}"


def load_baseline_index(
    manifest_path: Path,
    canonical_kb_path: Path,
    *,
    compiled_kb: CompiledKB | None = None,
) -> BaselineRegulationIndex:
    """Build normalized baseline index from sources/manifest.json and canonical KB."""
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_by_key: dict[str, dict] = {}
    manifest_by_ref: dict[str, dict] = {}
    manifest_by_url: dict[str, dict] = {}

    for entry in manifest_data.get("sources") or ():
        key = str(entry.get("key") or "").strip()
        if key:
            manifest_by_key[key] = entry
        url = str(entry.get("url") or "").strip()
        if url:
            manifest_by_url[url] = entry
        default_kind = _infer_kind_from_url(url)
        ref = _normalize_reg_ref(str(entry.get("title") or ""), default_kind=default_kind)
        if ref:
            manifest_by_ref[ref] = entry

    refs_without = tuple(manifest_data.get("references_without_snapshot") or ())

    if compiled_kb is not None:
        graph = compiled_kb.dataset.graph(compiled_kb.owl_projection_ref)
        kb_version = compiled_kb.snapshot.version
        kb_sha256 = compiled_kb.snapshot.sha256
    else:
        raw_bytes = canonical_kb_path.read_bytes()
        kb_sha256 = sha256(raw_bytes).hexdigest()
        graph = parse_turtle(raw_bytes, canonical_kb_path.resolve().as_uri())
        kb_version = None
        for ont in graph.subjects(RDF.type, OWL.Ontology):
            ver = graph.value(ont, OWL.versionInfo)
            if ver is not None:
                kb_version = str(ver)
                break

    kb_docs_by_ref: dict[str, KbSourceMeta] = {}
    kb_docs_by_key: dict[str, KbSourceMeta] = {}
    doc_ref_by_iri: dict[URIRef, str] = {}

    for subj in sorted(graph.subjects(RDF.type, TG.SourceDocument), key=str):
        if not isinstance(subj, URIRef):
            continue
        reg_ref = _resolve_doc_ref_in_graph(graph, subj)
        source_key = graph.value(subj, TG.sourceKey)
        reg_num = graph.value(subj, TG.regulationNumber)
        label = graph.value(subj, RDFS.label)
        eff = graph.value(subj, TG.effectiveFromDate)
        status = graph.value(subj, TG.regulationStatus)
        see_also = graph.value(subj, RDFS.seeAlso)
        meta = KbSourceMeta(
            iri=str(subj),
            reg_ref=reg_ref,
            source_key=str(source_key) if source_key is not None else None,
            regulation_number=str(reg_num) if reg_num is not None else None,
            label=str(label) if label is not None else None,
            effective_from=str(eff) if eff is not None else None,
            status=str(status) if status is not None else None,
            url=str(see_also) if see_also is not None else None,
        )
        if reg_ref:
            doc_ref_by_iri[subj] = reg_ref
            # Prefer canonical non-staged SourceDocument if both exist
            prev = kb_docs_by_ref.get(reg_ref)
            if prev is None or (prev.iri.split("#")[-1].startswith("staged-") and not meta.iri.split("#")[-1].startswith("staged-")):
                kb_docs_by_ref[reg_ref] = meta
        if meta.source_key:
            kb_docs_by_key[meta.source_key] = meta

    kb_relations: set[tuple[str, str, str]] = set()
    kb_relation_dates: dict[tuple[str, str, str], str | None] = {}

    for ev in graph.subjects(RDF.type, TG.LegalRelationEvent):
        acting = graph.value(ev, TG.relationActingDocument)
        target = graph.value(ev, TG.relationTargetDocument)
        kind_node = graph.value(ev, TG.hasLegalRelationKind)
        eff_node = graph.value(ev, TG.relationEffectiveDate)
        if not isinstance(acting, URIRef) or not isinstance(target, URIRef) or kind_node is None:
            continue
        acting_ref = doc_ref_by_iri.get(acting) or _resolve_doc_ref_in_graph(graph, acting)
        target_ref = doc_ref_by_iri.get(target) or _resolve_doc_ref_in_graph(graph, target)
        if not acting_ref or not target_ref:
            continue
        kind_key = str(kind_node).split("#")[-1]
        canon_kind = _KB_RELATION_KIND_MAP.get(kind_key, "RELATED")
        edge = (acting_ref, canon_kind, target_ref)
        kb_relations.add(edge)
        eff_str = str(eff_node) if eff_node is not None else None
        if edge not in kb_relation_dates or (eff_str and not kb_relation_dates[edge]):
            kb_relation_dates[edge] = eff_str

    for canon_kind, pred in (
        ("AMENDS", TG.amendsDocument),
        ("REVOKES", TG.revokesDocument),
        ("PARTIALLY_REVOKES", TG.partiallyRevokesDocument),
    ):
        for acting, target in graph.subject_objects(pred):
            if not isinstance(acting, URIRef) or not isinstance(target, URIRef):
                continue
            acting_ref = doc_ref_by_iri.get(acting) or _resolve_doc_ref_in_graph(graph, acting)
            target_ref = doc_ref_by_iri.get(target) or _resolve_doc_ref_in_graph(graph, target)
            if not acting_ref or not target_ref:
                continue
            edge = (acting_ref, canon_kind, target_ref)
            kb_relations.add(edge)
            if edge not in kb_relation_dates:
                eff_node = graph.value(acting, TG.effectiveFromDate)
                kb_relation_dates[edge] = str(eff_node) if eff_node is not None else None

    return BaselineRegulationIndex(
        manifest_path=manifest_path,
        canonical_kb_path=canonical_kb_path,
        kb_version=kb_version,
        kb_sha256=kb_sha256,
        manifest_by_key=manifest_by_key,
        manifest_by_ref=manifest_by_ref,
        manifest_by_url=manifest_by_url,
        references_without_snapshot=refs_without,
        kb_docs_by_ref=kb_docs_by_ref,
        kb_docs_by_key=kb_docs_by_key,
        kb_relations=frozenset(kb_relations),
        kb_relation_dates=kb_relation_dates,
        kb_triples=frozenset(graph),
    )


class RegulatoryMonitor:
    """Monitors JDIH Kemenkeu & Kemendag against sources/manifest.json and the canonical OWL KB."""

    def __init__(
        self,
        manifest_path: Path,
        canonical_kb_path: Path,
        *,
        compiled_kb: CompiledKB | None = None,
        engine_profile_id: str = ENGINE_PROFILE_ID,
        kemenkeu_scraper_factory: Callable[[float], JdihKemenkeuScraper] | None = None,
        kemendag_scraper_factory: Callable[[float], JdihKemendagScraper] | None = None,
    ):
        self.manifest_path = manifest_path
        self.canonical_kb_path = canonical_kb_path
        self.compiled_kb = compiled_kb
        self.engine_profile_id = engine_profile_id
        self._kemenkeu_factory = kemenkeu_scraper_factory or (lambda delay: JdihKemenkeuScraper(delay_seconds=delay))
        self._kemendag_factory = kemendag_scraper_factory or (lambda delay: JdihKemendagScraper(delay_seconds=delay))
        self._index: BaselineRegulationIndex | None = None

    def load_index(self, *, force_reload: bool = False) -> BaselineRegulationIndex:
        if self._index is None or force_reload:
            self._index = load_baseline_index(
                self.manifest_path,
                self.canonical_kb_path,
                compiled_kb=self.compiled_kb,
            )
        return self._index

    def list_targets(self) -> tuple[WatchTarget, ...]:
        return DEFAULT_WATCHLIST

    def _resolve_local_archive(self, local_ref: str | None) -> Path | None:
        if not local_ref:
            return None
        candidate = Path(local_ref)
        if candidate.is_absolute():
            return candidate
        repo_root = self.manifest_path.resolve().parent.parent
        under_root = repo_root / local_ref
        if under_root.exists():
            return under_root
        under_manifest = self.manifest_path.resolve().parent / local_ref
        if under_manifest.exists():
            return under_manifest
        return under_root

    def audit_local_freshness(
        self,
        *,
        as_of_date: str | None = None,
        stale_days: int = 180,
        verify_hashes: bool = False,
        check_local_files: bool = True,
    ) -> LocalFreshnessAudit:
        """Offline audit comparing sources/manifest.json archive health against canonical KB lineage."""
        if stale_days < 1:
            raise ValueError("stale_days must be >= 1")
        as_of = _validate_iso_date(as_of_date)
        as_of_dt = date.fromisoformat(as_of)
        index = self.load_index()

        incoming_by_target: dict[str, list[tuple[str, str, str | None]]] = {}
        for acting_ref, kind, target_ref in index.kb_relations:
            eff = index.kb_relation_dates.get((acting_ref, kind, target_ref))
            incoming_by_target.setdefault(target_ref, []).append((acting_ref, kind, eff))

        manifest_items: list[ManifestFreshnessItem] = []
        missing_count = 0
        hash_mismatch_count = 0
        revoked_active_count = 0
        upcoming_revocation_count = 0
        amended_gap_count = 0
        stale_count = 0

        for key, entry in sorted(index.manifest_by_key.items()):
            url = str(entry.get("url") or "") or None
            default_kind = _infer_kind_from_url(url)
            title = str(entry.get("title") or key)
            reg_ref = _normalize_reg_ref(title, default_kind=default_kind)
            eff_period = entry.get("effective_period") or {}
            eff_from = str(eff_period.get("start")) if isinstance(eff_period, dict) and eff_period.get("start") else None
            acquired_raw = str(entry.get("acquired_at") or "") or None
            age_days: int | None = None
            if acquired_raw:
                try:
                    acq_date = datetime.fromisoformat(acquired_raw.replace("Z", "+00:00")).date()
                    age_days = max(0, (as_of_dt - acq_date).days)
                except ValueError:
                    age_days = None

            local_ref = str(entry.get("local_ref") or "") or None
            resolved_path = self._resolve_local_archive(local_ref)
            file_exists = bool(resolved_path and resolved_path.is_file())
            hash_verified: bool | None = None
            expected_hash = str(entry.get("content_hash") or "").strip().lower()
            if verify_hashes and file_exists and resolved_path is not None and expected_hash:
                with resolved_path.open("rb") as handle:
                    actual_hash = file_digest(handle, "sha256").hexdigest().lower()
                hash_verified = actual_hash == expected_hash

            alerts: list[str] = []
            revoked_active = False
            revoked_future = False
            amended_unarchived = False
            amended_archived = False

            for acting_ref, kind, eff in sorted(incoming_by_target.get(reg_ref or "", [])):
                in_manifest = acting_ref in index.manifest_by_ref
                is_active = True if eff is None else (eff <= as_of)
                eff_note = f" eff. {eff}" if eff else ""
                archive_note = "" if in_manifest else " [unarchived in manifest]"
                if kind == "REVOKES":
                    alerts.append(f"REVOKED by {acting_ref}{eff_note}{archive_note}")
                    if is_active:
                        revoked_active = True
                    else:
                        revoked_future = True
                elif kind == "PARTIALLY_REVOKES":
                    alerts.append(f"PARTIALLY REVOKED by {acting_ref}{eff_note}{archive_note}")
                    if not in_manifest:
                        amended_unarchived = True
                    else:
                        amended_archived = True
                elif kind == "AMENDS":
                    alerts.append(f"AMENDED by {acting_ref}{eff_note}{archive_note}")
                    if not in_manifest:
                        amended_unarchived = True
                    else:
                        amended_archived = True

            if check_local_files and not file_exists:
                missing_count += 1
                freshness_status = "MISSING_ARCHIVE"
                severity: SeverityLevel = "CRITICAL"
            elif hash_verified is False:
                hash_mismatch_count += 1
                freshness_status = "HASH_MISMATCH"
                severity = "CRITICAL"
            elif revoked_active:
                revoked_active_count += 1
                freshness_status = "REVOKED"
                severity = "CRITICAL"
            elif revoked_future:
                upcoming_revocation_count += 1
                freshness_status = "UPCOMING_REVOCATION"
                severity = "HIGH"
            elif amended_unarchived:
                amended_gap_count += 1
                freshness_status = "AMENDED_GAP"
                severity = "HIGH"
            elif age_days is not None and age_days > stale_days:
                stale_count += 1
                freshness_status = "STALE"
                severity = "MEDIUM"
            elif amended_archived:
                freshness_status = "AMENDED"
                severity = "LOW"
            else:
                freshness_status = "CURRENT"
                severity = "INFO"

            manifest_items.append(
                ManifestFreshnessItem(
                    key=key,
                    reg_ref=reg_ref,
                    title=title,
                    effective_from=eff_from,
                    acquired_at=acquired_raw,
                    age_days=age_days,
                    local_ref=local_ref,
                    file_exists=file_exists,
                    hash_verified=hash_verified,
                    freshness_status=freshness_status,
                    severity=severity,
                    lineage_alerts=tuple(alerts),
                    url=url,
                )
            )

        # Find KB SourceDocuments that amend/revoke archived manifest sources yet have no PDF archived in manifest.json
        unarchived_map: dict[tuple[str, str], dict[str, object]] = {}
        for acting_ref, kind, target_ref in sorted(index.kb_relations):
            if kind not in {"REVOKES", "AMENDS", "PARTIALLY_REVOKES"}:
                continue
            if acting_ref in index.manifest_by_ref:
                continue
            target_manifest = index.manifest_by_ref.get(target_ref)
            if target_manifest is None:
                continue
            target_key = str(target_manifest["key"])
            kb_meta = index.kb_docs_by_ref.get(acting_ref)
            if kb_meta is None:
                continue
            eff = index.kb_relation_dates.get((acting_ref, kind, target_ref)) or kb_meta.effective_from
            is_active = True if eff is None else (eff <= as_of)
            bucket_key = (acting_ref, kind)
            bucket = unarchived_map.setdefault(
                bucket_key,
                {
                    "meta": kb_meta,
                    "eff": eff,
                    "active": is_active,
                    "keys": [],
                },
            )
            keys_list: list[str] = bucket["keys"]  # type: ignore[assignment]
            if target_key not in keys_list:
                keys_list.append(target_key)
            if is_active:
                bucket["active"] = True

        unarchived_gaps: list[UnarchivedKbGap] = []
        for (acting_ref, kind), bucket in unarchived_map.items():
            kb_meta = bucket["meta"]
            assert isinstance(kb_meta, KbSourceMeta)
            eff = bucket["eff"]
            is_active = bool(bucket["active"])
            impacted_keys = tuple(sorted(bucket["keys"]))  # type: ignore[arg-type]
            if kind == "REVOKES":
                sev: SeverityLevel = "CRITICAL" if is_active else "HIGH"
                action = "ARCHIVE_AND_INGEST_REPLACEMENT_PMK"
            else:
                sev = "HIGH"
                action = "ARCHIVE_AND_DIFF_AMENDMENT"
            unarchived_gaps.append(
                UnarchivedKbGap(
                    reg_ref=acting_ref,
                    kb_document_iri=kb_meta.iri,
                    source_key=kb_meta.source_key,
                    regulation_number=kb_meta.regulation_number,
                    label=kb_meta.label,
                    effective_from=str(eff) if eff else kb_meta.effective_from,
                    url=kb_meta.url,
                    relation_kind=kind,
                    impacts_tracked_keys=impacted_keys,
                    active_on_date=is_active,
                    severity=sev,
                    recommended_action=action,
                )
            )

        unarchived_gaps.sort(
            key=lambda g: (_SEVERITY_ORDER[g.severity], g.effective_from or "9999-99-99", g.reg_ref)
        )
        manifest_items.sort(key=lambda m: (_SEVERITY_ORDER[m.severity], m.key))

        if missing_count or hash_mismatch_count or revoked_active_count:
            overall: Literal["HEALTHY", "ATTENTION", "ACTION_REQUIRED"] = "ACTION_REQUIRED"
        elif upcoming_revocation_count or amended_gap_count or stale_count or unarchived_gaps:
            overall = "ATTENTION"
        else:
            overall = "HEALTHY"

        return LocalFreshnessAudit(
            as_of_date=as_of,
            stale_days=stale_days,
            kb_version=index.kb_version,
            kb_sha256=index.kb_sha256,
            overall_health=overall,
            manifest_count=len(index.manifest_by_key),
            kb_documents_count=len(index.kb_docs_by_ref),
            kb_relations_count=len(index.kb_relations),
            archived_files_missing=missing_count,
            hash_mismatches=hash_mismatch_count,
            revoked_active_count=revoked_active_count,
            upcoming_revocation_count=upcoming_revocation_count,
            amended_unarchived_gap_count=amended_gap_count,
            stale_count=stale_count,
            references_without_snapshot_count=len(index.references_without_snapshot),
            unarchived_kb_gaps=tuple(unarchived_gaps),
            manifest_items=tuple(manifest_items),
            references_without_snapshot=index.references_without_snapshot,
        )

    def _classify_regulation(
        self,
        reg: dict[str, object],
        *,
        matched_targets: tuple[str, ...],
        index: BaselineRegulationIndex,
    ) -> MonitorFinding:
        portal = str(reg.get("portal") or "jdih-kemenkeu")
        slug = str(reg.get("slug") or _slugify(str(reg.get("number") or "regulation")))
        number = str(reg.get("number") or slug)
        title = str(reg.get("title") or number)
        url = str(reg.get("url") or "")
        pdf_url = _extract_fulltext_pdf_url(reg)
        effective = (
            str(reg["effective_date"]) if reg.get("effective_date")
            else str(reg["promulgated_date"]) if reg.get("promulgated_date")
            else str(reg["enacted_date"]) if reg.get("enacted_date")
            else None
        )
        status_label = str(reg["status"]).strip() if reg.get("status") else None
        reg_ref = _normalize_scraped_ref(reg)
        default_kind = _portal_default_kind(portal, url)

        manifest_entry = (
            index.manifest_by_ref.get(reg_ref) if reg_ref else None
        ) or (index.manifest_by_url.get(pdf_url) if pdf_url else None) or (index.manifest_by_url.get(url) if url else None)
        manifest_key = str(manifest_entry["key"]) if manifest_entry and manifest_entry.get("key") else None

        kb_meta = (
            index.kb_docs_by_ref.get(reg_ref) if reg_ref else None
        ) or (index.kb_docs_by_key.get(manifest_key) if manifest_key else None)
        kb_iri = kb_meta.iri if kb_meta is not None else None

        # Diff relations against canonical KB
        new_relations: list[DetectedRelationDiff] = []
        impacted_keys_set: set[str] = set()
        has_critical_rel = False
        has_high_rel = False

        for rel in reg.get("relations") or ():
            if not isinstance(rel, dict):
                continue
            raw_kind = str(rel.get("relation_kind") or "RELATED").upper()
            target_text = f"{rel.get('target_number') or ''} {rel.get('target_title') or ''}".strip()
            rel_default_kind = _infer_kind_from_url(str(rel.get("target_url") or ""))
            if rel_default_kind == "reg":
                rel_default_kind = default_kind
            target_ref = _normalize_reg_ref(target_text, default_kind=rel_default_kind)
            if not reg_ref or not target_ref:
                continue
            acting_ref, canon_kind, affected_ref = _canonicalize_relation(reg_ref, raw_kind, target_ref)
            impacted_target_keys = index.tracked_keys_for_ref(affected_ref)
            if impacted_target_keys:
                impacted_keys_set.update(impacted_target_keys)

            if (acting_ref, canon_kind, affected_ref) not in index.kb_relations:
                rel_eff = str(rel["effective_date"]) if rel.get("effective_date") else effective
                new_relations.append(
                    DetectedRelationDiff(
                        acting_ref=acting_ref,
                        relation_kind=canon_kind,
                        target_ref=affected_ref,
                        effective_date=rel_eff,
                        raw_label=str(rel.get("raw_label") or raw_kind),
                        impacts_tracked_keys=impacted_target_keys,
                    )
                )
                if canon_kind == "REVOKES" and impacted_target_keys:
                    has_critical_rel = True
                elif canon_kind in {"AMENDS", "PARTIALLY_REVOKES"} and impacted_target_keys:
                    has_high_rel = True

        # Check metadata drift for tracked regulations
        metadata_diffs: list[str] = []
        status_indicates_revoked = bool(
            status_label and any(tok in status_label.lower() for tok in ("cabut", "tidak berlaku", "revoked"))
        )
        if kb_meta is not None:
            if status_label and kb_meta.status and status_label.lower() != kb_meta.status.lower():
                metadata_diffs.append(f"status: {kb_meta.status} -> {status_label}")
                if status_indicates_revoked:
                    has_critical_rel = True
            elif status_indicates_revoked and not kb_meta.status:
                metadata_diffs.append(f"status: None -> {status_label}")
                has_critical_rel = True
            if effective and not kb_meta.effective_from:
                metadata_diffs.append(f"effective_from: None -> {effective}")
            elif effective and kb_meta.effective_from and effective != kb_meta.effective_from:
                metadata_diffs.append(f"effective_from: {kb_meta.effective_from} -> {effective}")
        if manifest_entry is not None and pdf_url:
            manifest_url = str(manifest_entry.get("url") or "")
            if manifest_url and manifest_url != pdf_url and "jdih." in manifest_url and "jdih." in pdf_url:
                metadata_diffs.append(f"pdf_url changed: {manifest_url} -> {pdf_url}")

        # Determine finding status, severity, and recommended action
        if manifest_entry is None and kb_meta is None:
            finding_status: FindingStatus = "NEW_UNTRACKED"
            if has_critical_rel:
                severity: SeverityLevel = "CRITICAL"
                action = "STAGE_AND_REVIEW_REVOCATION"
            elif has_high_rel:
                severity = "HIGH"
                action = "STAGE_AND_ARCHIVE_AMENDMENT"
            else:
                severity = "MEDIUM"
                action = "STAGE_NEW_CANDIDATE"
        elif manifest_entry is None and kb_meta is not None:
            finding_status = "KB_ONLY_UNARCHIVED"
            revokes_archived = bool(
                reg_ref
                and any(
                    act == reg_ref and kind == "REVOKES" and tgt in index.manifest_by_ref
                    for act, kind, tgt in index.kb_relations
                )
            )
            if has_critical_rel or revokes_archived:
                severity = "CRITICAL"
                action = "ARCHIVE_PDF_AND_STAGE_REVOCATION"
            elif has_high_rel or impacted_keys_set:
                severity = "HIGH"
                action = "ARCHIVE_PDF_FOR_KB_DOC"
            else:
                severity = "MEDIUM"
                action = "ARCHIVE_PDF_FOR_KB_DOC"
        elif new_relations:
            finding_status = "RELATION_DRIFT"
            if has_critical_rel:
                severity = "CRITICAL"
                action = "STAGE_AND_REVIEW_REVOCATION"
            elif has_high_rel:
                severity = "HIGH"
                action = "STAGE_RELATION_UPDATE"
            else:
                severity = "LOW"
                action = "STAGE_RELATION_UPDATE"
        elif metadata_diffs:
            finding_status = "METADATA_DRIFT"
            severity = "CRITICAL" if has_critical_rel else "MEDIUM"
            action = "SYNC_METADATA"
        else:
            finding_status = "UP_TO_DATE"
            severity = "INFO"
            action = "NO_ACTION"

        return MonitorFinding(
            portal=portal,
            slug=slug,
            reg_ref=reg_ref,
            number=number,
            title=title,
            url=url,
            fulltext_pdf_url=pdf_url,
            effective_date=effective,
            status_label=status_label,
            finding_status=finding_status,
            severity=severity,
            matched_targets=matched_targets,
            manifest_key=manifest_key,
            kb_document_iri=kb_iri,
            impacts_tracked_keys=tuple(sorted(impacted_keys_set)),
            new_relations=tuple(new_relations),
            metadata_diffs=tuple(metadata_diffs),
            recommended_action=action,
            regulation=reg,
        )

    def evaluate_regulations(
        self,
        regulations: Sequence[ScrapedRegulation | dict[str, object]],
        *,
        target_map: dict[str, list[str]] | None = None,
        targets_checked: Sequence[str] = ("replay",),
        mode: Literal["live", "replay"] = "replay",
        as_of_date: str | None = None,
        target_errors: Sequence[TargetScanError] = (),
    ) -> MonitorScanReport:
        """Diff a sequence of scraped regulation payloads against sources/manifest.json and canonical KB."""
        as_of = _validate_iso_date(as_of_date)
        index = self.load_index()

        # Deduplicate by (portal, slug) or normalized reg_ref while preserving target attribution
        deduped: dict[str, tuple[dict[str, object], list[str]]] = {}
        for raw_item in regulations:
            reg = _coerce_regulation_dict(raw_item)
            reg_ref = _normalize_scraped_ref(reg)
            dedup_key = reg_ref or f"{reg.get('portal', 'jdih')}::{reg.get('slug') or reg.get('url') or reg.get('number')}"
            t_list = list((target_map or {}).get(dedup_key, []))
            if dedup_key in deduped:
                existing_reg, existing_targets = deduped[dedup_key]
                for tid in t_list:
                    if tid not in existing_targets:
                        existing_targets.append(tid)
                # Keep richer relations list if the newer payload has more relations
                if len(reg.get("relations") or ()) > len(existing_reg.get("relations") or ()):
                    deduped[dedup_key] = (reg, existing_targets)
            else:
                deduped[dedup_key] = (reg, t_list)

        findings: list[MonitorFinding] = []
        status_counts: dict[str, int] = {
            "NEW_UNTRACKED": 0,
            "KB_ONLY_UNARCHIVED": 0,
            "RELATION_DRIFT": 0,
            "METADATA_DRIFT": 0,
            "UP_TO_DATE": 0,
        }
        for reg, matched in deduped.values():
            finding = self._classify_regulation(
                reg,
                matched_targets=tuple(sorted(set(matched))),
                index=index,
            )
            findings.append(finding)
            status_counts[finding.finding_status] = status_counts.get(finding.finding_status, 0) + 1

        findings.sort(
            key=lambda item: (
                _SEVERITY_ORDER[item.severity],
                _FINDING_STATUS_ORDER[item.finding_status],
                item.effective_date or "",
                item.reg_ref or item.slug,
            )
        )

        critical_count = sum(1 for f in findings if f.severity == "CRITICAL")
        high_count = sum(1 for f in findings if f.severity == "HIGH")
        medium_count = sum(1 for f in findings if f.severity == "MEDIUM")
        up_to_date_count = status_counts["UP_TO_DATE"]
        actionable_count = len(findings) - up_to_date_count

        return MonitorScanReport(
            scanned_at=datetime.now(UTC).isoformat(),
            as_of_date=as_of,
            mode=mode,
            kb_version=index.kb_version,
            kb_sha256=index.kb_sha256,
            targets_checked=tuple(targets_checked),
            total_scanned=len(findings),
            actionable_count=actionable_count,
            critical_count=critical_count,
            high_count=high_count,
            medium_count=medium_count,
            up_to_date_count=up_to_date_count,
            status_counts=status_counts,
            target_errors=tuple(target_errors),
            findings=tuple(findings),
        )

    def resolve_targets(
        self,
        *,
        target_ids: Sequence[str] | None = None,
        portals: Sequence[str] | None = None,
        custom_query: str | None = None,
        custom_portal: Literal["kemenkeu", "kemendag"] = "kemenkeu",
        tahun: int | None = None,
    ) -> tuple[WatchTarget, ...]:
        if custom_query and custom_query.strip():
            return (
                WatchTarget(
                    target_id=f"custom-{custom_portal}-{_slugify(custom_query, max_len=24)}",
                    portal=custom_portal,
                    query=custom_query.strip(),
                    category="CUSTOM",
                    description=f"Custom JDIH {custom_portal} query: {custom_query.strip()}",
                    tahun=tahun,
                ),
            )
        selected: list[WatchTarget] = []
        if target_ids:
            for tid in target_ids:
                cleaned = tid.strip().lower()
                if cleaned not in WATCHLIST_BY_ID:
                    valid = ", ".join(sorted(WATCHLIST_BY_ID))
                    raise ValueError(f"Unknown monitor target {tid!r}. Valid targets: {valid}")
                selected.append(WATCHLIST_BY_ID[cleaned])
        else:
            selected = list(DEFAULT_WATCHLIST)

        if portals:
            allowed = {p.strip().lower() for p in portals if p.strip()}
            unknown = allowed - {"kemenkeu", "kemendag"}
            if unknown:
                raise ValueError(f"Unknown portal(s): {sorted(unknown)}. Allowed: kemenkeu, kemendag")
            selected = [item for item in selected if item.portal in allowed]

        if not selected:
            raise ValueError("No monitor targets matched the requested filters.")
        if tahun is not None:
            selected = [
                WatchTarget(
                    target_id=item.target_id,
                    portal=item.portal,
                    query=item.query,
                    category=item.category,
                    description=item.description,
                    bentuk=item.bentuk,
                    tahun=tahun if item.portal == "kemendag" else item.tahun,
                    status=item.status,
                    tracked_keys=item.tracked_keys,
                )
                for item in selected
            ]
        return tuple(selected)

    def scan_live(
        self,
        *,
        target_ids: Sequence[str] | None = None,
        portals: Sequence[str] | None = None,
        custom_query: str | None = None,
        custom_portal: Literal["kemenkeu", "kemendag"] = "kemenkeu",
        tahun: int | None = None,
        limit_per_target: int = 3,
        delay_seconds: float = 0.5,
        as_of_date: str | None = None,
    ) -> MonitorScanReport:
        """Run a bounded live scan against JDIH Kemenkeu and/or Kemendag."""
        if limit_per_target < 1 or limit_per_target > 20:
            raise ValueError("limit_per_target must be between 1 and 20.")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be >= 0.")

        targets = self.resolve_targets(
            target_ids=target_ids,
            portals=portals,
            custom_query=custom_query,
            custom_portal=custom_portal,
            tahun=tahun,
        )
        kemenkeu = self._kemenkeu_factory(delay_seconds)
        kemendag = self._kemendag_factory(delay_seconds)

        collected: list[dict[str, object]] = []
        target_map: dict[str, list[str]] = {}
        errors: list[TargetScanError] = []

        for target in targets:
            try:
                if target.portal == "kemenkeu":
                    scraped = kemenkeu.scrape(target.query, bentuk=target.bentuk, limit=limit_per_target)
                else:
                    scraped = kemendag.scrape(
                        target.query,
                        tahun=target.tahun,
                        status=target.status,
                        limit=limit_per_target,
                    )
            except Exception as err:  # noqa: BLE001 — isolate per-portal/per-target failures
                errors.append(
                    TargetScanError(
                        target_id=target.target_id,
                        portal=target.portal,
                        query=target.query,
                        error=str(err),
                    )
                )
                continue

            for item in scraped:
                reg = _coerce_regulation_dict(item)
                reg_ref = _normalize_scraped_ref(reg)
                dedup_key = reg_ref or f"{reg.get('portal', 'jdih')}::{reg.get('slug') or reg.get('url') or reg.get('number')}"
                target_map.setdefault(dedup_key, []).append(target.target_id)
                collected.append(reg)

        return self.evaluate_regulations(
            collected,
            target_map=target_map,
            targets_checked=[t.target_id for t in targets],
            mode="live",
            as_of_date=as_of_date,
            target_errors=errors,
        )

    def build_staging_diff(
        self,
        report: MonitorScanReport,
        *,
        include_statuses: Iterable[FindingStatus] = (
            "NEW_UNTRACKED",
            "KB_ONLY_UNARCHIVED",
            "RELATION_DRIFT",
            "METADATA_DRIFT",
        ),
        output_dir: Path | None = None,
        archive_pdfs: bool = False,
        validate_merge: bool = False,
        run_owl_validator: bool = False,
    ) -> StagingDiffResult:
        """Build a net-new trial OWL 2 DL staging Turtle diff and proposed manifest patch without touching the KB."""
        index = self.load_index()
        allowed_statuses = set(include_statuses)
        candidates = [f for f in report.findings if f.finding_status in allowed_statuses]
        candidate_dicts = [dict(f.regulation) for f in candidates]

        archived_pdfs: list[dict[str, object]] = []
        archive_errors: list[str] = []
        manifest_patch: list[dict[str, object]] = []
        written_files: dict[str, str] = {}

        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)

        for finding in candidates:
            if finding.manifest_key is not None:
                continue
            proposed_key = _propose_manifest_key(finding.regulation, finding.reg_ref)
            eff_start = finding.effective_date
            archived_entry: dict[str, object] | None = None
            if archive_pdfs and finding.fulltext_pdf_url and output_dir is not None:
                try:
                    src_rec = archive_regulation(_dict_to_scraped_regulation(finding.regulation), output_dir / "raw")
                    if src_rec is not None:
                        archived_entry = {
                            "key": proposed_key,
                            "title": f"{finding.number}, {finding.title}" if finding.number.lower() not in finding.title.lower() else finding.title,
                            "publisher": finding.regulation.get("authority"),
                            "id": src_rec.id,
                            "url": src_rec.url,
                            "authority": src_rec.authority,
                            "acquired_at": src_rec.acquired_at.isoformat(),
                            "content_hash": src_rec.content_hash,
                            "local_ref": src_rec.local_ref,
                            "status": src_rec.status,
                            "effective_period": {"start": eff_start, "end_exclusive": None} if eff_start else None,
                        }
                        archived_pdfs.append(archived_entry)
                except Exception as err:  # noqa: BLE001
                    archive_errors.append(f"{finding.number}: {err}")

            if archived_entry is not None:
                manifest_patch.append(archived_entry)
            else:
                manifest_patch.append(
                    {
                        "key": proposed_key,
                        "reg_ref": finding.reg_ref,
                        "title": f"{finding.number}, {finding.title}" if finding.number.lower() not in finding.title.lower() else finding.title,
                        "authority": finding.regulation.get("authority"),
                        "url": finding.fulltext_pdf_url or finding.url,
                        "summary_url": finding.url,
                        "status": "STAGED_CANDIDATE",
                        "effective_period": {"start": eff_start, "end_exclusive": None} if eff_start else None,
                    }
                )

        staging_summary: StagingBuildSummary | None = None
        delta_turtle = ""
        staging_triples = 0
        net_new_triples = 0
        validation_result: TrialValidationResult | None = None

        if candidate_dicts:
            raw_turtle, staging_summary = generate_staging_turtle(
                candidate_dicts,
                canonical_kb_path=self.canonical_kb_path,
                verify=True,
                review_status=None,
            )
            staging_graph = parse_turtle(raw_turtle.encode("utf-8"), "http://www.semanticweb.org/tradegraph/staging-diff")
            staging_triples = len(staging_graph)

            delta_graph = Graph()
            delta_graph.bind("", TG)
            delta_graph.bind("catalog", CATALOG)
            delta_graph.bind("owl", OWL)
            delta_graph.bind("rdf", RDF)
            delta_graph.bind("rdfs", RDFS)
            delta_graph.bind("sh", SH)
            delta_graph.bind("swrl", SWRL)
            delta_graph.bind("swrlb", SWRLB)
            delta_graph.bind("xsd", XSD)
            for triple in staging_graph:
                if triple not in index.kb_triples:
                    delta_graph.add(triple)
            net_new_triples = len(delta_graph)
            delta_turtle = delta_graph.serialize(format="turtle") if net_new_triples else ""

        if output_dir is not None:
            report_path = output_dir / "monitor_report.json"
            report_path.write_text(
                json.dumps(report.to_dict(include_regulations=False), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            written_files["monitor_report"] = report_path.as_posix()

            candidates_path = output_dir / "candidates.jsonl"
            write_jsonl(candidates_path, candidate_dicts)
            written_files["candidates_jsonl"] = candidates_path.as_posix()

            manifest_patch_path = output_dir / "manifest_patch.json"
            manifest_patch_path.write_text(
                json.dumps(manifest_patch, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            written_files["manifest_patch"] = manifest_patch_path.as_posix()

            ttl_path = output_dir / "staging_diff.ttl"
            ttl_path.write_text(delta_turtle, encoding="utf-8")
            written_files["staging_ttl"] = ttl_path.as_posix()

            if validate_merge and net_new_triples > 0:
                validation_result = validate_trial_merge(
                    self.canonical_kb_path,
                    ttl_path,
                    engine_profile_id=self.engine_profile_id,
                    run_owl_validator=run_owl_validator,
                )
        elif validate_merge and net_new_triples > 0:
            with TemporaryDirectory(prefix="tradegraph-monitor-validate-") as tmp_dir:
                tmp_ttl = Path(tmp_dir) / "staging_diff.ttl"
                tmp_ttl.write_text(delta_turtle, encoding="utf-8")
                validation_result = validate_trial_merge(
                    self.canonical_kb_path,
                    tmp_ttl,
                    engine_profile_id=self.engine_profile_id,
                    run_owl_validator=run_owl_validator,
                )

        return StagingDiffResult(
            candidates_selected=len(candidates),
            staging_summary=staging_summary,
            canonical_triples=len(index.kb_triples),
            staging_triples=staging_triples,
            net_new_triples=net_new_triples,
            delta_turtle=delta_turtle,
            manifest_patch=tuple(manifest_patch),
            archived_pdfs=tuple(archived_pdfs),
            archive_errors=tuple(archive_errors),
            validation=validation_result,
            written_files=written_files,
        )
