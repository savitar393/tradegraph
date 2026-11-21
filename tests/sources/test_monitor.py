from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from tradegraph import cli
from tradegraph.api.app import create_app
from tradegraph.sources import acquisition
from tradegraph.sources.monitor import DEFAULT_WATCHLIST, RegulatoryMonitor
from tradegraph.sources.parsers import write_jsonl
from tradegraph.sources.scrapers import JdihKemendagScraper, JdihKemenkeuScraper


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"
MANIFEST = ROOT / "sources" / "manifest.json"


def _make_kemenkeu_detail_html(payload_data: dict) -> str:
    wrapped = {"data": payload_data}
    return (
        "<html><body><script>"
        f'self.__next_f.push([1,{json.dumps("21:[" + json.dumps(wrapped) + "]")}])'
        "</script></body></html>"
    )


def _sample_new_revoking_pmk() -> dict[str, object]:
    return {
        "record_kind": "regulation",
        "portal": "jdih-kemenkeu",
        "slug": "pmk-99-tahun-2026-trial-bk",
        "url": "https://jdih.kemenkeu.go.id/dok/pmk-99-tahun-2026-trial-bk/summary",
        "number": "PMK 99 TAHUN 2026",
        "title": "Penetapan Barang Ekspor yang Dikenakan Bea Keluar dan Tarif Bea Keluar (Uji Monitor)",
        "authority": "Kementerian Keuangan Republik Indonesia",
        "regulation_type": "Peraturan Menteri",
        "year": 2026,
        "enacted_date": "2026-11-01",
        "promulgated_date": "2026-11-02",
        "effective_date": "2026-11-15",
        "status": "Approved",
        "subjects": ["BEA KELUAR", "EKSPOR"],
        "files": [
            {
                "role": "FULLTEXT_PDF",
                "url": "https://jdih.kemenkeu.go.id/api/download/mock-99-2026/2026pmkeuangan099.pdf",
                "label": "Fulltext PDF (Indonesia)",
                "filename": "2026pmkeuangan099.pdf",
            }
        ],
        "relations": [
            {
                "relation_kind": "REVOKES",
                "raw_label": "Mencabut",
                "target_number": "PMK 68 Tahun 2025",
                "target_title": "Perubahan atas PMK 38 Tahun 2024 tentang Penetapan Barang Ekspor yang Dikenakan Bea Keluar",
                "target_url": "https://jdih.kemenkeu.go.id/dok/pmk-68-tahun-2025/summary",
                "effective_date": "2026-11-15",
            }
        ],
        "scraped_at": "2026-11-21T00:00:00+00:00",
    }


def _sample_new_amending_permendag() -> dict[str, object]:
    return {
        "record_kind": "regulation",
        "portal": "jdih-kemendag",
        "slug": "peraturan-menteri-perdagangan-nomor-55-tahun-2026-uji-ekspor",
        "url": "https://jdih.kemendag.go.id/peraturan/peraturan-menteri-perdagangan-nomor-55-tahun-2026-uji-ekspor",
        "number": "Permendag 55 Tahun 2026",
        "title": "Perubahan Keempat atas Permendag 23 Tahun 2023 tentang Kebijakan dan Pengaturan Ekspor",
        "authority": "Kementerian Perdagangan Republik Indonesia",
        "regulation_type": "Peraturan Menteri",
        "year": 2026,
        "enacted_date": "2026-11-05",
        "promulgated_date": "2026-11-06",
        "effective_date": "2026-11-20",
        "status": "Berlaku",
        "subjects": ["KEBIJAKAN", "EKSPOR"],
        "files": [
            {
                "role": "FULLTEXT_PDF",
                "url": "https://jdih.kemendag.go.id/peraturan/download/mock-55-2026/file_peraturan",
                "label": "Peraturan [ID]",
                "filename": None,
            }
        ],
        "relations": [
            {
                "relation_kind": "AMENDS",
                "raw_label": "MENGUBAH",
                "target_number": "9 Tahun 2025",
                "target_title": "Peraturan Menteri Perdagangan Nomor 9 Tahun 2025",
                "target_url": "https://jdih.kemendag.go.id/peraturan/permendag-9-tahun-2025",
                "effective_date": "2026-11-20",
            }
        ],
        "scraped_at": "2026-11-21T00:00:00+00:00",
    }


def test_offline_local_freshness_audit_real_repo_and_temporal_revocation():
    monitor = RegulatoryMonitor(MANIFEST, CANONICAL_KB)

    audit_june = monitor.audit_local_freshness(as_of_date="2026-06-15", verify_hashes=True)
    assert audit_june.manifest_count >= 40
    assert audit_june.archived_files_missing == 0
    assert audit_june.hash_mismatches == 0
    assert audit_june.revoked_active_count == 0
    assert audit_june.upcoming_revocation_count == 1  # PMK 60/2026 revokes ID-TARIF-IJEPA on 2026-08-01
    assert audit_june.overall_health == "ATTENTION"

    audit_aug = monitor.audit_local_freshness(as_of_date="2026-08-15", verify_hashes=False)
    assert audit_aug.revoked_active_count == 1
    assert audit_aug.overall_health == "ACTION_REQUIRED"
    gap_refs = {g.reg_ref: g for g in audit_aug.unarchived_kb_gaps}
    assert "pmk-60-2026" in gap_refs
    assert gap_refs["pmk-60-2026"].severity == "CRITICAL"
    assert "ID-TARIF-IJEPA" in gap_refs["pmk-60-2026"].impacts_tracked_keys
    assert "permendag-18-2026" in gap_refs
    assert "ID-IMPOR-GENERAL" in gap_refs["permendag-18-2026"].impacts_tracked_keys


def test_offline_freshness_detects_missing_file_and_hash_mismatch(tmp_path):
    raw_file = tmp_path / "corrupt.pdf"
    raw_file.write_bytes(b"%PDF-1.4 corrupted-bytes")
    manifest_copy = tmp_path / "manifest.json"
    manifest_copy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "hash_algorithm": "sha256",
                "sources": [
                    {
                        "key": "ID-BEA-KELUAR",
                        "title": "PMK 38 Tahun 2024, penetapan barang ekspor yang dikenakan bea keluar",
                        "url": "https://jdih.kemenkeu.go.id/api/download/mock/2024pmkeuangan038.pdf",
                        "acquired_at": "2026-11-20T00:00:00+00:00",
                        "content_hash": "0" * 64,
                        "local_ref": str(raw_file),
                        "status": "ACQUIRED",
                        "effective_period": {"start": "2024-06-03", "end_exclusive": None},
                    },
                    {
                        "key": "ID-MISSING-PDF",
                        "title": "PMK 12 Tahun 2024, contoh file hilang",
                        "url": "https://jdih.kemenkeu.go.id/api/download/mock/missing.pdf",
                        "acquired_at": "2026-11-20T00:00:00+00:00",
                        "content_hash": "1" * 64,
                        "local_ref": str(tmp_path / "does-not-exist.pdf"),
                        "status": "ACQUIRED",
                        "effective_period": {"start": "2024-02-01", "end_exclusive": None},
                    },
                ],
                "references_without_snapshot": [],
            }
        ),
        encoding="utf-8",
    )

    monitor = RegulatoryMonitor(manifest_copy, CANONICAL_KB)
    strict_audit = monitor.audit_local_freshness(as_of_date="2026-11-21", verify_hashes=True, check_local_files=True)
    assert strict_audit.archived_files_missing == 1
    assert strict_audit.hash_mismatches == 1
    assert strict_audit.overall_health == "ACTION_REQUIRED"
    by_key = {item.key: item for item in strict_audit.manifest_items}
    assert by_key["ID-MISSING-PDF"].freshness_status == "MISSING_ARCHIVE"
    assert by_key["ID-BEA-KELUAR"].freshness_status == "HASH_MISMATCH"

    container_audit = monitor.audit_local_freshness(as_of_date="2026-11-21", verify_hashes=False, check_local_files=False)
    assert container_audit.archived_files_missing == 0


def test_evaluate_regulations_classifies_all_five_diff_statuses():
    monitor = RegulatoryMonitor(MANIFEST, CANONICAL_KB)

    # 1. UP_TO_DATE: PMK 68 Tahun 2025 already archived + AMENDS PMK 38 Tahun 2024 already in KB
    up_to_date_pmk68 = {
        "record_kind": "regulation",
        "portal": "jdih-kemenkeu",
        "slug": "pmk-68-tahun-2025",
        "url": "https://jdih.kemenkeu.go.id/dok/pmk-68-tahun-2025/summary",
        "number": "PMK 68 Tahun 2025",
        "title": "Perubahan atas PMK 38 Tahun 2024 tentang Penetapan Barang Ekspor yang Dikenakan Bea Keluar",
        "effective_date": "2025-10-22",
        "status": "Approved",
        "files": [
            {
                "role": "FULLTEXT_PDF",
                "url": "https://jdih.kemenkeu.go.id/api/download/dfc692b6-6a37-4d83-aae1-49555a59c372/2025pmkeuangan068.pdf",
                "label": "Fulltext PDF",
            }
        ],
        "relations": [
            {
                "relation_kind": "AMENDS",
                "raw_label": "Mengubah",
                "target_number": "PMK 38 Tahun 2024",
                "effective_date": "2025-10-22",
            }
        ],
    }

    # 2. KB_ONLY_UNARCHIVED: PMK 60 Tahun 2026 exists in KB lineage (revokes PMK 50/2022) but not in manifest.json
    kb_only_pmk60 = {
        "record_kind": "regulation",
        "portal": "jdih-kemenkeu",
        "slug": "pmk-60-tahun-2026",
        "url": "https://jdih.kemenkeu.go.id/dok/pmk-60-tahun-2026/summary",
        "number": "PMK 60 TAHUN 2026",
        "title": "Penetapan Tarif Bea Masuk dalam rangka IJEPA",
        "effective_date": "2026-08-01",
        "status": "Approved",
        "files": [
            {
                "role": "FULLTEXT_PDF",
                "url": "https://jdih.kemenkeu.go.id/api/download/mock-60-2026/2026pmkeuangan060.pdf",
                "label": "Fulltext PDF",
            }
        ],
        "relations": [
            {
                "relation_kind": "REVOKES",
                "raw_label": "Mencabut",
                "target_number": "50/PMK.010/2022",
                "effective_date": "2026-08-01",
            }
        ],
    }

    # 3. RELATION_DRIFT: Existing Permendag 23/2023 now reports AMENDED_BY Permendag 55 Tahun 2026
    rel_drift_permendag23 = {
        "record_kind": "regulation",
        "portal": "jdih-kemendag",
        "slug": "permendag-23-tahun-2023",
        "url": "https://jdih.kemendag.go.id/peraturan/permendag-23-tahun-2023",
        "number": "Permendag 23 Tahun 2023",
        "title": "Kebijakan dan Pengaturan Ekspor",
        "effective_date": "2023-07-19",
        "status": "Berlaku",
        "files": [
            {
                "role": "FULLTEXT_PDF",
                "url": "https://jdih.kemendag.go.id/peraturan/download/da167e38-7dae-4e8e-83f5-be47d5cc6687/file_peraturan",
                "label": "Peraturan [ID]",
            }
        ],
        "relations": [
            {
                "relation_kind": "AMENDED_BY",
                "raw_label": "DIUBAH DENGAN",
                "target_number": "55 Tahun 2026",
                "target_title": "Peraturan Menteri Perdagangan Nomor 55 Tahun 2026",
                "effective_date": "2026-11-20",
            }
        ],
    }

    # 4. METADATA_DRIFT: Existing PMK 38/2024 status flips to Tidak Berlaku
    meta_drift_pmk38 = {
        "record_kind": "regulation",
        "portal": "jdih-kemenkeu",
        "slug": "pmk-38-tahun-2024",
        "url": "https://jdih.kemenkeu.go.id/dok/pmk-38-tahun-2024/summary",
        "number": "PMK 38 Tahun 2024",
        "title": "Penetapan Barang Ekspor yang Dikenakan Bea Keluar dan Tarif Bea Keluar",
        "effective_date": "2024-06-03",
        "status": "Tidak Berlaku",
        "files": [
            {
                "role": "FULLTEXT_PDF",
                "url": "https://jdih.kemenkeu.go.id/api/download/014e3646-7fd0-45d5-ad9a-8bf067e6f1ea/2024pmkeuangan038.pdf",
                "label": "Fulltext PDF",
            }
        ],
        "relations": [],
    }

    report = monitor.evaluate_regulations(
        [
            up_to_date_pmk68,
            kb_only_pmk60,
            _sample_new_revoking_pmk(),
            _sample_new_amending_permendag(),
            rel_drift_permendag23,
            meta_drift_pmk38,
        ],
        as_of_date="2026-11-21",
    )

    by_ref = {f.reg_ref: f for f in report.findings}
    assert by_ref["pmk-68-2025"].finding_status == "UP_TO_DATE"
    assert by_ref["pmk-68-2025"].severity == "INFO"

    assert by_ref["pmk-60-2026"].finding_status == "KB_ONLY_UNARCHIVED"
    assert by_ref["pmk-60-2026"].severity == "CRITICAL"

    assert by_ref["pmk-99-2026"].finding_status == "NEW_UNTRACKED"
    assert by_ref["pmk-99-2026"].severity == "CRITICAL"
    assert "ID-BEA-KELUAR-AMEND25" in by_ref["pmk-99-2026"].impacts_tracked_keys

    assert by_ref["permendag-55-2026"].finding_status == "NEW_UNTRACKED"
    assert by_ref["permendag-55-2026"].severity == "HIGH"
    assert "ID-LARTAS-EKSPOR-AMEND25" in by_ref["permendag-55-2026"].impacts_tracked_keys

    assert by_ref["permendag-23-2023"].finding_status == "RELATION_DRIFT"
    assert by_ref["permendag-23-2023"].severity == "HIGH"
    assert any(
        rel.acting_ref == "permendag-55-2026" and rel.relation_kind == "AMENDS" and rel.target_ref == "permendag-23-2023"
        for rel in by_ref["permendag-23-2023"].new_relations
    )

    assert by_ref["pmk-38-2024"].finding_status == "METADATA_DRIFT"
    assert by_ref["pmk-38-2024"].severity == "CRITICAL"

    assert report.total_scanned == 6
    assert report.up_to_date_count == 1
    assert report.actionable_count == 5
    assert report.critical_count == 3


def test_live_scan_with_mock_scrapers_isolates_portal_failure():
    search_html = '<html><body><a href="/dok/pmk-99-tahun-2026-trial-bk">PMK 99 TAHUN 2026</a></body></html>'
    detail_html = _make_kemenkeu_detail_html(
        {
            "Slug": "pmk-99-tahun-2026-trial-bk",
            "Nomor": "PMK 99 TAHUN 2026",
            "Judul": "Penetapan Barang Ekspor yang Dikenakan Bea Keluar (Uji Monitor)",
            "TEU": "Kementerian Keuangan",
            "Status": "Approved",
            "Jenis": {"Id": 105, "Keterangan": "Peraturan Menteri"},
            "PUU": {
                "No": 99,
                "Tahun": 2026,
                "TglPenetapan": "$D2026-11-01T00:00:00.000Z",
                "TglPengundangan": "$D2026-11-02T00:00:00.000Z",
                "TglMulaiBerlaku": "$D2026-11-15T00:00:00.000Z",
            },
            "Label": [{"Jenis": 1, "Teks": "BEA KELUAR"}],
            "File": [
                {
                    "Jenis": 1,
                    "Keterangan": "Fulltext PDF (Indonesia)",
                    "MediaCatalog": {"Id": "mock-99", "Path": "2026pmkeuangan099.pdf", "Ext": ".pdf", "FileName": "2026pmkeuangan099.pdf"},
                }
            ],
            "Relasi": [
                {
                    "KodeRelasi": "MCB",
                    "Nama": "Mencabut",
                    "TglMulaiBerlaku": "$D2026-11-15T00:00:00.000Z",
                    "Nomor": "PMK 68 Tahun 2025",
                    "Judul": "Perubahan atas PMK 38 Tahun 2024",
                    "Slug": "pmk-68-tahun-2025",
                }
            ],
        }
    )

    def fake_kemenkeu_fetch(url: str) -> str:
        if "/search?" in url:
            return search_html
        if "/dok/pmk-99-tahun-2026-trial-bk/summary" in url:
            return detail_html
        raise AssertionError(f"Unexpected URL: {url}")

    def failing_kemendag_fetch(url: str) -> str:
        raise OSError(f"Portal blocked or rate-limited request (503) for {url}")

    monitor = RegulatoryMonitor(
        MANIFEST,
        CANONICAL_KB,
        kemenkeu_scraper_factory=lambda delay: JdihKemenkeuScraper(fetcher=fake_kemenkeu_fetch, delay_seconds=0),
        kemendag_scraper_factory=lambda delay: JdihKemendagScraper(fetcher=failing_kemendag_fetch, delay_seconds=0),
    )

    report = monitor.scan_live(
        target_ids=["bea-keluar", "lartas-ekspor"],
        limit_per_target=2,
        delay_seconds=0,
        as_of_date="2026-11-21",
    )
    assert report.mode == "live"
    assert report.targets_checked == ("bea-keluar", "lartas-ekspor")
    assert report.total_scanned == 1
    assert report.critical_count == 1
    assert report.findings[0].reg_ref == "pmk-99-2026"
    assert report.findings[0].matched_targets == ("bea-keluar",)
    assert len(report.target_errors) == 1
    assert report.target_errors[0].target_id == "lartas-ekspor"
    assert "503" in report.target_errors[0].error

    with pytest.raises(ValueError, match="limit_per_target"):
        monitor.scan_live(target_ids=["bea-keluar"], limit_per_target=25)
    with pytest.raises(ValueError, match="Unknown monitor target"):
        monitor.scan_live(target_ids=["nonexistent-target"], limit_per_target=1)


def test_staging_diff_pipeline_archives_pdfs_and_passes_real_owlapi_hermit_validation(tmp_path, monkeypatch):
    kb_before_sha = sha256(CANONICAL_KB.read_bytes()).hexdigest()
    manifest_before_sha = sha256(MANIFEST.read_bytes()).hexdigest()

    class FakePdfResponse(BytesIO):
        def __init__(self, body: bytes):
            super().__init__(body)
            self.headers = {"Content-Length": str(len(body))}

    monkeypatch.setattr(acquisition, "urlopen", lambda *a, **kw: FakePdfResponse(b"%PDF-1.4 staged-monitor-sample"))

    monitor = RegulatoryMonitor(MANIFEST, CANONICAL_KB)
    scan_report = monitor.evaluate_regulations(
        [_sample_new_revoking_pmk(), _sample_new_amending_permendag()],
        as_of_date="2026-11-21",
    )

    stage_dir = tmp_path / "staging-run"
    staging = monitor.build_staging_diff(
        scan_report,
        output_dir=stage_dir,
        archive_pdfs=True,
        validate_merge=True,
        run_owl_validator=True,
    )

    assert staging.candidates_selected == 2
    assert staging.net_new_triples > 10
    assert staging.staging_summary is not None
    assert staging.staging_summary.flagged_records == 0
    assert staging.staging_summary.legal_relations_emitted == 2
    assert len(staging.manifest_patch) == 2
    assert len(staging.archived_pdfs) == 2
    assert staging.archive_errors == ()
    assert (stage_dir / "monitor_report.json").is_file()
    assert (stage_dir / "candidates.jsonl").is_file()
    assert (stage_dir / "manifest_patch.json").is_file()
    assert (stage_dir / "staging_diff.ttl").is_file()
    assert list((stage_dir / "raw").glob("*.pdf"))

    assert staging.validation is not None
    assert staging.validation.kb_compiled is True
    assert staging.validation.owl_validated is True
    assert staging.validation.merged_owl_triples > staging.validation.canonical_owl_triples

    # Canonical KB and repo manifest must remain completely untouched
    assert sha256(CANONICAL_KB.read_bytes()).hexdigest() == kb_before_sha
    assert sha256(MANIFEST.read_bytes()).hexdigest() == manifest_before_sha


def test_cli_sources_monitor_targets_offline_audit_and_replay_staging(tmp_path, capsys):
    # 1. List targets
    assert cli.main(["sources", "monitor", "--list-targets"]) == 0
    targets_payload = json.loads(capsys.readouterr().out)
    assert len(targets_payload["targets"]) == len(DEFAULT_WATCHLIST)

    # 2. Offline audit with --fail-on-critical returns exit code 2 on 2026-08-15 (PMK 60/2026 active revocation)
    rc_audit = cli.main([
        "sources", "monitor",
        "--offline-audit",
        "--as-of-date", "2026-08-15",
        "--fail-on-critical",
        "--manifest", str(MANIFEST),
        "--canonical-kb", str(CANONICAL_KB),
    ])
    assert rc_audit == 2
    audit_payload = json.loads(capsys.readouterr().out)
    assert audit_payload["overall_health"] == "ACTION_REQUIRED"
    assert audit_payload["summary"]["revoked_active_count"] == 1

    # 3. Replay JSONL + staging + SHACL validation
    replay_file = tmp_path / "scraped.jsonl"
    write_jsonl(replay_file, [_sample_new_revoking_pmk()])
    stage_dir = tmp_path / "cli-stage"

    rc_replay = cli.main([
        "sources", "monitor",
        "--input-jsonl", str(replay_file),
        "--as-of-date", "2026-11-21",
        "--manifest", str(MANIFEST),
        "--canonical-kb", str(CANONICAL_KB),
        "--stage-dir", str(stage_dir),
        "--validate",
        "--skip-owl-check",
        "--fail-on-critical",
    ])
    assert rc_replay == 2
    replay_payload = json.loads(capsys.readouterr().out)
    assert replay_payload["summary"]["critical_count"] == 1
    assert replay_payload["staging"]["net_new_triples"] > 5
    assert replay_payload["staging"]["validation"]["kb_compiled"] is True
    assert (stage_dir / "staging_diff.ttl").is_file()


def test_api_monitor_endpoints_and_workbench_modal():
    search_html = '<html><body><a href="/dok/pmk-99-tahun-2026-trial-bk">PMK 99 TAHUN 2026</a></body></html>'
    detail_html = _make_kemenkeu_detail_html(
        {
            "Slug": "pmk-99-tahun-2026-trial-bk",
            "Nomor": "PMK 99 TAHUN 2026",
            "Judul": "Uji Monitor API Bea Keluar",
            "TEU": "Kementerian Keuangan",
            "Status": "Approved",
            "PUU": {"No": 99, "Tahun": 2026, "TglMulaiBerlaku": "$D2026-11-15T00:00:00.000Z"},
            "File": [],
            "Relasi": [
                {
                    "KodeRelasi": "MCB",
                    "Nama": "Mencabut",
                    "TglMulaiBerlaku": "$D2026-11-15T00:00:00.000Z",
                    "Nomor": "PMK 68 Tahun 2025",
                    "Judul": "Perubahan PMK 38/2024",
                    "Slug": "pmk-68-tahun-2025",
                }
            ],
        }
    )
    mock_monitor = RegulatoryMonitor(
        MANIFEST,
        CANONICAL_KB,
        kemenkeu_scraper_factory=lambda delay: JdihKemenkeuScraper(
            fetcher=lambda url: search_html if "/search?" in url else detail_html,
            delay_seconds=0,
        ),
    )

    app = create_app(kb_path=CANONICAL_KB, manifest_path=MANIFEST, regulatory_monitor=mock_monitor)
    client = TestClient(app)

    # Workbench UI includes JDIH Monitor controls
    ui = client.get("/")
    assert ui.status_code == 200
    assert 'id="btnMonitor"' in ui.text
    assert 'id="monitorModal"' in ui.text

    # 1. Watchlist targets
    t_resp = client.get("/api/v1/monitor/targets")
    assert t_resp.status_code == 200
    assert t_resp.json()["count"] == len(DEFAULT_WATCHLIST)

    # 2. Offline freshness audit
    f_resp = client.get("/api/v1/monitor/freshness", params={"as_of_date": "2026-08-15"})
    assert f_resp.status_code == 200
    f_data = f_resp.json()
    assert f_data["overall_health"] == "ACTION_REQUIRED"
    assert f_data["summary"]["manifest_count"] >= 40
    assert any(g["reg_ref"] == "pmk-60-2026" for g in f_data["unarchived_kb_gaps"])

    # 3. Replay scan with staging + SHACL trial merge validation
    r_resp = client.post(
        "/api/v1/monitor/scan",
        json={
            "as_of_date": "2026-11-21",
            "regulations": [_sample_new_amending_permendag()],
            "generate_staging": True,
            "validate_merge": True,
            "run_owl_validator": False,
        },
    )
    assert r_resp.status_code == 200
    r_data = r_resp.json()
    assert r_data["mode"] == "replay"
    assert r_data["summary"]["high_count"] == 1
    assert r_data["staging"]["net_new_triples"] > 5
    assert r_data["staging"]["validation"]["kb_compiled"] is True
    assert "delta_turtle" in r_data["staging"] and ":LegalRelationEvent" in r_data["staging"]["delta_turtle"]

    # 4. Bounded mocked live scan
    l_resp = client.post(
        "/api/v1/monitor/scan",
        json={
            "targets": ["bea-keluar"],
            "limit_per_target": 1,
            "delay_seconds": 0,
            "as_of_date": "2026-11-21",
            "generate_staging": True,
        },
    )
    assert l_resp.status_code == 200
    l_data = l_resp.json()
    assert l_data["mode"] == "live"
    assert l_data["summary"]["critical_count"] == 1
    assert l_data["findings"][0]["reg_ref"] == "pmk-99-2026"
