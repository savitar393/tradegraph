from datetime import UTC, date, datetime
from hashlib import file_digest, sha256
from io import BytesIO
import json
from pathlib import Path

import pytest

from tradegraph.sources import acquisition


URL = "https://example.org/bulletin.pdf"


class Response(BytesIO):
    def __init__(self, body: bytes, declared_length: int | None = None):
        super().__init__(body)
        length = len(body) if declared_length is None else declared_length
        self.headers = {"Content-Length": str(length)}


def test_source_version_is_stable_for_repeated_bytes_and_changes_for_new_bytes(
    tmp_path, monkeypatch
):
    responses = iter((b"first edition", b"first edition", b"second edition"))
    monkeypatch.setattr(
        acquisition, "urlopen", lambda *args, **kwargs: Response(next(responses))
    )

    first = acquisition.acquire_source(URL, tmp_path)
    repeated = acquisition.acquire_source(URL, tmp_path)
    changed = acquisition.acquire_source(URL, tmp_path)

    assert first.content_hash == repeated.content_hash == sha256(b"first edition").hexdigest()
    assert first.id == repeated.id
    assert first.local_ref == repeated.local_ref
    assert changed.content_hash == sha256(b"second edition").hexdigest()
    assert changed.content_hash != first.content_hash
    assert changed.id != first.id
    assert first.url == changed.url == URL
    assert first.status == "ACQUIRED"
    assert first.authority is None
    assert first.effective_period is None
    assert first.acquired_at.tzinfo is UTC
    assert Path(first.local_ref).read_bytes() == b"first edition"
    assert Path(changed.local_ref).read_bytes() == b"second edition"
    assert len(list(tmp_path.iterdir())) == 2


def test_same_bytes_at_different_urls_keep_distinct_source_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(
        acquisition, "urlopen", lambda *args, **kwargs: Response(b"same document")
    )

    first = acquisition.acquire_source(URL, tmp_path)
    mirror = acquisition.acquire_source("https://mirror.example/bulletin.pdf", tmp_path)

    assert first.content_hash == mirror.content_hash
    assert first.local_ref == mirror.local_ref
    assert first.id != mirror.id


def test_incomplete_response_does_not_publish_an_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(
        acquisition, "urlopen", lambda *args, **kwargs: Response(b"partial", 100)
    )

    with pytest.raises(OSError, match="Incomplete source"):
        acquisition.acquire_source(URL, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_manifest_snapshots_match_recorded_bytes():
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((root / "sources/manifest.json").read_text(encoding="utf-8"))
    assert manifest["hash_algorithm"] == "sha256"
    records = manifest["sources"]
    assert records
    assert len({record["id"] for record in records}) == len(records)
    for record in records:
        local_ref = Path(record["local_ref"])
        snapshot = (root / local_ref).resolve()
        assert not local_ref.is_absolute()
        assert snapshot.is_relative_to(root / "sources" / "raw")
        with snapshot.open("rb") as stream:
            digest = file_digest(stream, "sha256").hexdigest()
        assert digest == record["content_hash"]
        version_key = f'{record["url"]}\0{digest}'.encode()
        assert record["id"] == f"source-{sha256(version_key).hexdigest()}"
        assert record["key"]
        assert record["status"] == "ACQUIRED"
        assert record["authority"]
        assert datetime.fromisoformat(record["acquired_at"]).utcoffset().total_seconds() == 0
        if period := record["effective_period"]:
            assert date.fromisoformat(period["start"])
            if period["end_exclusive"]:
                assert date.fromisoformat(period["end_exclusive"]) > date.fromisoformat(
                    period["start"]
                )
