"""Archive exact response bytes and identify each version of a source URL."""

from datetime import UTC, datetime
from hashlib import file_digest, sha256
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from tradegraph.contracts import SourceRecord


CHUNK_SIZE = 64 * 1024
ARCHIVE_SUFFIXES = {".pdf", ".htm", ".html", ".json", ".txt", ".xml"}


def acquire_source(url: str, destination: Path) -> SourceRecord:
    """Download a public HTTP(S) source into a content-addressed directory."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Source URL must use HTTP(S) and have a hostname")

    suffix = PurePosixPath(parts.path).suffix.lower()
    if suffix not in ARCHIVE_SUFFIXES:
        suffix = ""
    destination.mkdir(parents=True, exist_ok=True)

    request = Request(url, headers={"User-Agent": "TradeGraph/0.1 (source archive)"})
    digest = sha256()
    size = 0
    download = NamedTemporaryFile(dir=destination, prefix=".download-", delete=False)
    temporary = Path(download.name)
    try:
        with download, urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            for block in iter(lambda: response.read(CHUNK_SIZE), b""):
                download.write(block)
                digest.update(block)
                size += len(block)
        if length is not None and size != int(length):
            raise OSError(f"Incomplete source from {url}: got {size} of {length} bytes")
        if size == 0:
            raise OSError(f"Empty source from {url}")

        content_hash = digest.hexdigest()
        snapshot = destination / f"{content_hash}{suffix}"
        if snapshot.exists():
            with snapshot.open("rb") as archived:
                if file_digest(archived, "sha256").hexdigest() != content_hash:
                    raise OSError(f"Archived source has changed: {snapshot}")
        else:
            temporary.replace(snapshot)
    finally:
        temporary.unlink(missing_ok=True)

    version = sha256(f"{url}\0{content_hash}".encode()).hexdigest()
    return SourceRecord(
        id=f"source-{version}",
        url=url,
        authority=None,
        acquired_at=datetime.now(UTC),
        content_hash=content_hash,
        local_ref=snapshot.as_posix(),
        status="ACQUIRED",
        effective_period=None,
    )
