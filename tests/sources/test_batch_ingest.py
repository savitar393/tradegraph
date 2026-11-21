"""Tests for bounded multi-chapter batch ingestion pipeline and CLI."""

import json
from pathlib import Path

from tradegraph.cli import main as cli_main
from tradegraph.sources.batch_ingest import discover_hs8_from_mfn, ingest_hs_batch


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "sources" / "manifest.json"
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"


def test_discover_hs8_from_mfn_enforces_max_hs_bound():
    codes = discover_hs8_from_mfn(MANIFEST, ["8507", "7208"], max_hs=3)
    assert len(codes) == 3
    assert all(code.startswith("8507.") for code in codes)


def test_ingest_hs_batch_and_cli_bounded_sample(tmp_path, capsys):
    out_dir = tmp_path / "batch_out"
    ttl_path = out_dir / "trial.ttl"
    report = ingest_hs_batch(
        MANIFEST,
        hs8_codes=["7210.12.10", "8507.60.33"],
        max_hs=5,
        output_dir=out_dir,
        include_agreements=("MFN", "IKCEPA", "IACEPA", "RCEP"),
        include_psrs=True,
        include_lartas=True,
        emit_ttl=ttl_path,
        canonical_kb=CANONICAL_KB,
    )
    assert report.hs8_codes == ("7210.12.10", "8507.60.33")
    assert report.tariffs_flagged == 0
    assert report.psrs_flagged == 0
    assert report.lartas_flagged == 0
    assert report.per_source_counts["ID-TARIF-IKCEPA"] == 10
    assert report.per_source_counts["ID-TARIF-IACEPA"] == 12
    assert report.per_source_counts["ID-LARTAS-INDUSTRI"] == 1
    assert report.rcep_appendix_hits == 1
    assert ttl_path.is_file()

    # Verify CLI wrapper works cleanly on a 1-code sample
    cli_dir = tmp_path / "cli_out"
    rc = cli_main([
        "sources", "ingest-batch",
        "--hs-codes", "8507.60.33",
        "--max-hs", "2",
        "--agreements", "MFN,IKCEPA",
        "--manifest", str(MANIFEST),
        "--output-dir", str(cli_dir),
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hs8_codes"] == ["8507.60.33"]
    assert payload["tariffs_flagged"] == 0
    assert payload["per_source_counts"]["ID-TARIF-IKCEPA"] == 5
