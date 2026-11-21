"""End-to-end Fuseki HTTP persistence, startup KB sync, readiness probes, container classpath, and Docker packaging tests."""

from base64 import b64decode
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest
from rdflib import Dataset, Graph, Namespace, RDF, URIRef
from rdflib.compare import isomorphic

from tradegraph.api import create_app
from tradegraph.cases.repository import CaseRepository
from tradegraph.cli import main as cli_main
from tradegraph.contracts import AssessmentContext, EvidenceClaim, FactPatch
from tradegraph.graph.store import FusekiGraphStore, GraphRevisionConflict, TG
from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.optimization import MultiFtaOptimizer


ROOT = Path(__file__).resolve().parents[2]
CANONICAL_KB = ROOT / "knowledge" / "tradegraph.ttl"
EX = Namespace("urn:example:fuseki-e2e#")


@contextmanager
def _run_local_fuseki_server(*, username: str = "admin", password: str = "secret-pass"):
    """Lightweight in-process HTTP server implementing Fuseki /$/ping, SPARQL query/update, and GSP data with Basic Auth."""
    dataset = Dataset()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def _check_auth(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Fuseki"')
                self.end_headers()
                return False
            decoded = b64decode(header[6:].strip()).decode("utf-8", errors="replace")
            if decoded != f"{username}:{password}":
                self.send_response(401)
                self.end_headers()
                return False
            return True

        def do_GET(self):  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/$/ping":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if not self._check_auth():
                return
            qs = parse_qs(parsed.query)
            if parsed.path == "/tradegraph/query":
                q = qs.get("query", [""])[0]
                with lock:
                    res = dataset.query(q)
                    body = res.serialize(format="json")
                self.send_response(200)
                self.send_header("Content-Type", "application/sparql-results+json")
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else body.encode("utf-8"))
                return
            if parsed.path == "/tradegraph/data":
                graph_iri = qs.get("graph", [None])[0]
                with lock:
                    g = dataset.graph(URIRef(graph_iri)) if graph_iri else dataset.default_context
                    if len(g) == 0:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = g.serialize(format="nt", encoding="utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/n-triples; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):  # noqa: N802
            parsed = urlsplit(self.path)
            if not self._check_auth():
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            if parsed.path == "/tradegraph/update":
                with lock:
                    dataset.update(raw)
                self.send_response(204)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def do_PUT(self):  # noqa: N802
            parsed = urlsplit(self.path)
            if not self._check_auth():
                return
            qs = parse_qs(parsed.query)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            if parsed.path == "/tradegraph/data":
                graph_iri = qs.get("graph", [None])[0]
                with lock:
                    target = dataset.graph(URIRef(graph_iri)) if graph_iri else dataset.default_context
                    target.remove((None, None, None))
                    target.parse(data=raw, format="nt")
                self.send_response(200)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/tradegraph", server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def warm_optimizer() -> MultiFtaOptimizer:
    kb = compile_kb(CANONICAL_KB, ENGINE_PROFILE_ID)
    return MultiFtaOptimizer(kb, resolve_runtime_classpath())


def test_fuseki_http_publish_auth_atomic_idempotency_conflict_and_kb_sync(tmp_path, warm_optimizer):
    with _run_local_fuseki_server(username="admin", password="secret-pass") as (url, _):
        # 1. Basic Auth rejection vs acceptance via ping()
        bad_store = FusekiGraphStore(url, username="admin", password="wrong")
        bad_ping = bad_store.ping()
        assert bad_ping["reachable"] is False
        assert "401" in str(bad_ping["error"])

        store = FusekiGraphStore(url, username="admin", password="secret-pass")
        ok_ping = store.ping()
        assert ok_ping["reachable"] is True
        assert ok_ping["error"] is None

        # 2. Atomic, idempotent concurrent publish + conflict rejection + branch revision
        repo = CaseRepository(tmp_path / "cases.db")
        ctx = AssessmentContext(
            kb_sha256=warm_optimizer.kb.snapshot.sha256,
            engine_profile_id=ENGINE_PROFILE_ID,
            kb_version=warm_optimizer.kb.snapshot.version,
            jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
            evidence_scope_id="scope-1",
        )
        claim = EvidenceClaim(str(EX.claim), str(EX.product), str(EX.origin), str(EX.source), value=EX.country)
        root = repo.create_case(
            "@prefix ex: <urn:example:fuseki-e2e#> . ex:product ex:pack [ ex:count 1 ] .",
            ctx, evidence_claims=(claim,), evidence_scope_closed=True,
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, duplicate = list(pool.map(store.publish, (root, root)))
        assert first.facts.identifier == duplicate.facts.identifier
        assert isomorphic(store.load_graph(first.facts.identifier), first.facts)
        assert (EX.claim, RDF.type, TG.EvidenceClaim) in store.load_graph(first.evidence.identifier)

        incompatible = replace(root, context=replace(ctx, jurisdiction_iri=str(EX.otherJurisdiction)))
        with pytest.raises(GraphRevisionConflict):
            store.publish(incompatible)

        branch = repo.revise_case(root.case_id, 1, FactPatch(
            reason="Branch revision", context=replace(ctx, jurisdiction_iri=str(EX.otherJurisdiction)),
        ), branch=True)
        alt = store.publish(branch)
        assert isomorphic(store.load_graph(alt.facts.identifier), alt.facts)
        assert alt.context.identifier != first.context.identifier

        listed = store.list_case_revisions(limit=10)
        assert len(listed) == 2
        assert {item["revision"] for item in listed} == {1, 2}

        # 3. Canonical KB snapshot sync (idempotent PUBLISHED -> ALREADY_SYNCED)
        first_sync = store.publish_kb(warm_optimizer.kb)
        assert first_sync["status"] == "PUBLISHED"
        assert first_sync["kb_version"] == "0.15.0"
        assert first_sync["triples"] > 1000

        second_sync = store.publish_kb(warm_optimizer.kb)
        assert second_sync["status"] == "ALREADY_SYNCED"


def test_api_readiness_fuseki_sync_and_case_persistence_end_to_end(tmp_path, warm_optimizer):
    db_path = tmp_path / "nested" / "cases.db"
    with _run_local_fuseki_server(username="admin", password="secret-pass") as (url, server):
        app = create_app(
            warm_optimizer,
            case_db_path=db_path,
            fuseki_url=url,
            fuseki_user="admin",
            fuseki_password="secret-pass",
            sync_kb_on_startup=True,
        )
        with TestClient(app) as client:
            # Startup lifespan already synced the canonical KB into Fuseki!
            h = client.get("/api/v1/health").json()
            assert h["persistence"] == {"sqlite_enabled": True, "fuseki_enabled": True}

            r_ok = client.get("/api/v1/ready")
            assert r_ok.status_code == 200
            rb = r_ok.json()
            assert rb["status"] == "ready"
            assert rb["kb"]["loaded"] is True
            assert rb["java"]["available"] is True
            assert rb["sqlite"]["ready"] is True
            assert rb["fuseki"]["reachable"] is True

            sync = client.post("/api/v1/fuseki/sync-kb")
            assert sync.status_code == 200
            assert sync.json()["status"] == "ALREADY_SYNCED"

            # Persist an Export CPO Bea Keluar case into BOTH SQLite and Fuseki TDB2
            pers = client.post("/api/v1/cases/persist", json={
                "mode": "export",
                "reason": "Audit snapshot for Nov 2025 CPO shipment",
                "shipment": {
                    "hs_code": "1511.10.00",
                    "origin_country": "ID",
                    "destination_country": "CN",
                    "as_of_date": "2025-11-15",
                    "fob_value": "950000",
                    "quantity_mt": "1000",
                    "reference_price_usd": "950.00",
                    "wo_confirmed": True,
                },
            })
            assert pers.status_code == 200
            pb = pers.json()
            assert pb["persisted"]["sqlite_stored"] is True
            assert pb["persisted"]["fuseki_published"] is True
            assert pb["assessment"]["export_duty"]["estimated_duty_usd"] == "124000.00"
            case_id = pb["persisted"]["case_id"]

            # List & reload case with live Fuseki triple verification
            listed = client.get("/api/v1/cases").json()
            assert listed["source"] == "sqlite"
            assert any(c["case_id"] == case_id for c in listed["cases"])

            detail = client.get(f"/api/v1/cases/{case_id}", params={"verify_fuseki": "true"})
            assert detail.status_code == 200
            db_body = detail.json()
            assert db_body["fuseki_verification"]["verified"] is True
            assert db_body["fuseki_verification"]["remote_facts_triples"] == db_body["facts_triples"]

            # Shut down Fuseki -> /api/v1/ready immediately reports 503 degraded!
            server.shutdown()
            r_down = client.get("/api/v1/ready")
            assert r_down.status_code == 503
            assert r_down.json()["status"] == "degraded"
            assert r_down.json()["fuseki"]["reachable"] is False


def test_cli_fuseki_ping_and_sync_kb(capsys):
    with _run_local_fuseki_server(username="admin", password="secret-pass") as (url, _):
        rc_ping = cli_main(["fuseki", "ping", "--url", url, "--user", "admin", "--password", "secret-pass"])
        assert rc_ping == 0
        assert json.loads(capsys.readouterr().out)["reachable"] is True

        rc_sync1 = cli_main([
            "fuseki", "sync-kb", "--url", url, "--kb", str(CANONICAL_KB),
            "--user", "admin", "--password", "secret-pass",
        ])
        assert rc_sync1 == 0
        assert json.loads(capsys.readouterr().out)["status"] == "PUBLISHED"

        rc_sync2 = cli_main([
            "fuseki", "sync-kb", "--url", url, "--kb", str(CANONICAL_KB),
            "--user", "admin", "--password", "secret-pass",
        ])
        assert rc_sync2 == 0
        assert json.loads(capsys.readouterr().out)["status"] == "ALREADY_SYNCED"


def test_container_classpath_resolution_and_packaging_artifacts(tmp_path, monkeypatch):
    # 1. TRADEGRAPH_CLASSPATH override takes immediate priority
    monkeypatch.setenv("TRADEGRAPH_CLASSPATH", "/app/semantic/target/classes:/app/semantic/target/lib/*")
    assert resolve_runtime_classpath() == "/app/semantic/target/classes:/app/semantic/target/lib/*"

    # 2. Pre-bundled semantic/target/classes + semantic/target/lib/*.jar resolution
    monkeypatch.delenv("TRADEGRAPH_CLASSPATH", raising=False)
    fake_sem = tmp_path / "semantic"
    (fake_sem / "target" / "classes").mkdir(parents=True)
    (fake_sem / "target" / "lib").mkdir(parents=True)
    (fake_sem / "pom.xml").write_text("<project/>", encoding="utf-8")
    jar_a = fake_sem / "target" / "lib" / "owlapi.jar"
    jar_a.write_bytes(b"PK")
    resolved = resolve_runtime_classpath(fake_sem / "pom.xml")
    assert str(fake_sem / "target" / "classes") in resolved
    assert str(jar_a) in resolved

    # 3. Validate production packaging artifacts exist & are syntactically valid
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "eclipse-temurin:21-jre" in dockerfile
    assert "TRADEGRAPH_CLASSPATH" in dockerfile
    assert "/api/v1/ready" in dockerfile
    assert (ROOT / ".dockerignore").is_file()
    assert (ROOT / ".env.example").is_file()
    assert (ROOT / "docker-compose.yml").is_file()

    # Fuseki TDB2 assembler config must be valid Turtle defining /tradegraph dataset
    fuseki_cfg = Graph().parse(ROOT / "deploy" / "fuseki" / "tradegraph.ttl", format="turtle")
    assert len(fuseki_cfg) >= 10

    # If Docker CLI is installed, validate docker-compose.yml schema cleanly
    if shutil.which("docker"):
        proc = subprocess.run(
            ["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "config", "--quiet"],
            cwd=str(ROOT), capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, f"docker compose config failed: {proc.stderr}"
