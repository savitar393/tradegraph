from hashlib import sha256
import json
from pathlib import Path

from tradegraph import cli
from tradegraph.sources import parsers, rdf_staging


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"
MANIFEST = ROOT / "sources" / "manifest.json"


def test_generate_staging_turtle_and_validate_trial_merge_without_modifying_kb(tmp_path):
    before_sha = sha256(CANONICAL_KB.read_bytes()).hexdigest()

    psr_path, psr_meta = parsers.resolve_manifest_source(MANIFEST, "ATIGA-PSR22")
    tariff_path, tariff_meta = parsers.resolve_manifest_source(MANIFEST, "ID-TARIF-ATIGA")
    statute_path, statute_meta = parsers.resolve_manifest_source(MANIFEST, "ID-IMPOR-GENERAL")

    psr_sample = parsers.parse_atiga_psr_pdf(psr_path, source_meta=psr_meta, start_page=40, end_page=40, max_records=4)
    tariff_sample = parsers.parse_pmk_tariff_pdf(tariff_path, source_meta=tariff_meta, start_page=81, end_page=81, max_records=4)
    statute_sample = parsers.parse_indonesian_statute_pdf(statute_path, source_meta=statute_meta, start_page=15, end_page=16, max_records=2)

    scraped_reg = {
        "record_kind": "regulation",
        "portal": "jdih-kemendag",
        "slug": "permendag-99-tahun-2026-trial-fixture",
        "url": "https://jdih.kemendag.go.id/peraturan/permendag-99-tahun-2026-trial-fixture",
        "number": "Permendag 99 Tahun 2026",
        "title": "Perubahan Uji Coba atas Permendag 16 Tahun 2025 tentang Kebijakan dan Pengaturan Impor",
        "authority": "Kementerian Perdagangan Republik Indonesia",
        "status": "Berlaku",
        "enacted_date": "2026-07-01",
        "promulgated_date": "2026-07-02",
        "relations": [{"relation_kind": "AMENDS", "target_number": "16 Tahun 2025"}],
    }

    records = [
        scraped_reg,
        *(r.to_dict() for r in psr_sample),
        *(r.to_dict() for r in tariff_sample),
        *(r.to_dict() for r in statute_sample),
    ]
    turtle, summary = rdf_staging.generate_staging_turtle(records, canonical_kb_path=CANONICAL_KB)

    assert summary.records_processed == len(records)
    assert summary.verified_records == len(records)
    assert summary.flagged_records == 0
    assert summary.triples_generated > 20
    assert summary.source_documents_emitted == 1  # existing manifest docs reused by sourceKey
    assert summary.hs_codes_emitted >= 3
    assert summary.psr_rules_emitted >= 3
    assert summary.legal_relations_emitted == 1
    assert '"EXTRACTED"' not in turtle
    assert '"SOURCE_REVIEWED"' not in turtle
    assert "axiomReviewStatus" not in turtle

    staging_file = tmp_path / "trial.ttl"
    staging_file.write_text(turtle, encoding="utf-8")

    validation = rdf_staging.validate_trial_merge(CANONICAL_KB, staging_file, run_owl_validator=False)
    assert validation.kb_compiled is True
    assert validation.canonical_sha256 == before_sha
    assert validation.merged_owl_triples > validation.canonical_owl_triples
    assert sha256(CANONICAL_KB.read_bytes()).hexdigest() == before_sha


def test_cli_sources_parse_and_to_rdf_end_to_end(tmp_path, capsys):
    jsonl_out = tmp_path / "psr_sample.jsonl"
    ttl_out = tmp_path / "psr_sample.ttl"

    exit_parse = cli.main([
        "sources", "parse",
        "--kind", "psr",
        "--source-key", "ATIGA-PSR22",
        "--manifest", str(MANIFEST),
        "--start-page", "11",
        "--end-page", "11",
        "--max-records", "3",
        "--output", str(jsonl_out),
    ])
    assert exit_parse == 0
    parse_payload = json.loads(capsys.readouterr().out)
    assert parse_payload["count"] == 3

    exit_rdf = cli.main([
        "sources", "to-rdf",
        str(jsonl_out),
        "--output", str(ttl_out),
        "--canonical-kb", str(CANONICAL_KB),
        "--validate",
        "--skip-owl-check",
    ])
    assert exit_rdf == 0
    rdf_payload = json.loads(capsys.readouterr().out)
    assert rdf_payload["records_processed"] == 3
    assert rdf_payload["validation"]["kb_compiled"] is True
    assert ttl_out.is_file()


def test_long_shared_prefix_regulations_emit_distinct_source_documents():
    regs = [
        {
            "record_kind": "regulation",
            "portal": "jdih-kemendag",
            "slug": "peraturan-menteri-perdagangan-republik-indonesia-nomor-91-tahun-2026-tentang-perubahan-uji-satu",
            "url": "https://jdih.kemendag.go.id/peraturan/peraturan-menteri-perdagangan-republik-indonesia-nomor-91-tahun-2026",
            "number": "Permendag 91 Tahun 2026",
            "title": "Perubahan Uji Satu Permendag 16/2025",
        },
        {
            "record_kind": "regulation",
            "portal": "jdih-kemendag",
            "slug": "peraturan-menteri-perdagangan-republik-indonesia-nomor-92-tahun-2026-tentang-perubahan-uji-dua",
            "url": "https://jdih.kemendag.go.id/peraturan/peraturan-menteri-perdagangan-republik-indonesia-nomor-92-tahun-2026",
            "number": "Permendag 92 Tahun 2026",
            "title": "Perubahan Uji Dua Permendag 24/2025",
        },
    ]
    _, summary = rdf_staging.generate_staging_turtle(regs, canonical_kb_path=CANONICAL_KB)
    assert summary.source_documents_emitted == 2
