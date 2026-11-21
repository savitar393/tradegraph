"""Build revision-scoped RDF views and publish them transactionally to Fuseki."""

from base64 import b64encode
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

from rdflib import Graph, Literal, RDF, URIRef, XSD

from tradegraph.cases.facts import CASE_DATA, TG, parse_persisted_facts, require_iri, serialize_assertions
from tradegraph.contracts import CaseSnapshot, CompiledKB
from tradegraph.kb.rdf import parse_turtle


class GraphRevisionConflict(ValueError):
    """The named graphs for a revision already contain a different projection."""


@dataclass(frozen=True, slots=True)
class CaseGraphs:
    facts: Graph
    context: Graph
    evidence: Graph


def _revision_iri(case_iri: URIRef, revision: int) -> URIRef:
    if type(revision) is not int or revision < 1:
        raise ValueError("Expected a positive integer case revision")
    return URIRef(f"{case_iri}:revision:{revision}")


def _utc_literal(timestamp: datetime) -> Literal:
    if timestamp.utcoffset() is None:
        raise ValueError("Assessment timestamps require a timezone")
    value = timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return Literal(value, datatype=XSD.dateTime, normalize=False)


def project_case_graphs(snapshot: CaseSnapshot) -> CaseGraphs:
    """Project one pinned snapshot without treating evidence claims as asserted facts."""
    case_iri = URIRef(f"urn:tradegraph:case:{UUID(snapshot.case_id)}")
    revision = _revision_iri(case_iri, snapshot.revision)
    facts = Graph(identifier=URIRef(f"{revision}:facts"))
    for triple in parse_persisted_facts(snapshot.facts_turtle, snapshot.case_id):
        facts.add(triple)
    context = Graph(identifier=URIRef(f"{revision}:context"))
    evidence = Graph(identifier=URIRef(f"{revision}:evidence"))
    assessment = context.identifier
    source = snapshot.context
    knowledge = require_iri(f"urn:tradegraph:kb-snapshot:{source.kb_sha256}")

    for triple in (
        (case_iri, RDF.type, TG.TradeCase),
        (revision, RDF.type, TG.TradeCaseRevision),
        (revision, TG.revisionOf, case_iri),
        (revision, TG.caseRevisionNumber, Literal(snapshot.revision)),
        (assessment, RDF.type, TG.AssessmentContext),
        (assessment, TG.assessesRevision, revision),
        (assessment, TG.usesKnowledgeRevision, knowledge),
        (assessment, TG.engineProfileId, Literal(source.engine_profile_id)),
        (knowledge, RDF.type, TG.KnowledgeRevision),
        (knowledge, TG.snapshotSha256, Literal(source.kb_sha256)),
    ):
        context.add(triple)
    if snapshot.parent_revision is not None:
        context.add((revision, TG.previousCaseRevision, _revision_iri(case_iri, snapshot.parent_revision)))
    if source.kb_version is not None:
        context.add((knowledge, TG.knowledgeVersion, Literal(source.kb_version)))
    for predicate, value in (
        (TG.appliesInJurisdiction, source.jurisdiction_iri), (TG.selectedProcedure, source.procedure_iri),
        (TG.selectedAgreement, source.agreement_iri), (TG.assessesProduct, source.product_iri),
        (TG.assessesBatch, source.batch_iri),
    ):
        if value is not None:
            context.add((assessment, predicate, require_iri(value)))
    for predicate, value in ((TG.transactionAt, source.transaction_at), (TG.evaluationAt, source.evaluation_at)):
        if value is not None:
            context.add((assessment, predicate, _utc_literal(value)))

    scope = None
    if source.evidence_scope_id is not None:
        scope = URIRef(f"{revision}:evidence-scope")
        context.add((assessment, TG.usesEvidenceSet, scope))
        evidence.add((scope, RDF.type, TG.EvidenceSet))
        evidence.add((scope, CASE_DATA.scopeId, Literal(source.evidence_scope_id)))
        if snapshot.evidence_scope_closed is not None:
            evidence.add((scope, TG.evidenceScopeClosed, Literal(snapshot.evidence_scope_closed)))
    for claim in snapshot.evidence_claims:
        claim_iri = require_iri(claim.claim_iri)
        evidence.add((claim_iri, RDF.type, TG.EvidenceClaim))
        evidence.add((claim_iri, TG.claimSubject, require_iri(claim.subject_iri)))
        evidence.add((claim_iri, RDF.predicate, require_iri(claim.predicate_iri)))
        evidence.add((claim_iri, CASE_DATA.sourceReference, require_iri(claim.source_iri)))
        evidence.add((claim_iri, CASE_DATA.verificationStatus, Literal(claim.verification_status)))
        if claim.value is not None:
            evidence.add((claim_iri, RDF.object, claim.value))
        if scope is not None:
            evidence.add((scope, TG.containsClaim, claim_iri))
    return CaseGraphs(facts, context, evidence)


class FusekiGraphStore:
    def __init__(
        self,
        dataset_url: str,
        *,
        timeout: float = 15,
        username: str | None = None,
        password: str | None = None,
    ):
        parts = urlsplit(dataset_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
            raise ValueError("Expected an HTTP(S) Fuseki dataset URL")
        self.dataset_url = dataset_url.rstrip("/")
        self.server_base_url = f"{parts.scheme}://{parts.netloc}"
        self.timeout = timeout
        self._username = username if username is not None else os.environ.get("TRADEGRAPH_FUSEKI_USER")
        self._password = password if password is not None else os.environ.get("TRADEGRAPH_FUSEKI_PASSWORD")

    def _auth_header(self) -> str | None:
        if self._username and self._password is not None:
            token = b64encode(f"{self._username}:{self._password}".encode("utf-8")).decode("ascii")
            return f"Basic {token}"
        return None

    def ping(self) -> dict[str, object]:
        """Check Fuseki server & dataset liveness without raising."""
        started = monotonic()
        try:
            # First test the dataset SPARQL endpoint directly (validates dataset existence & auth too)
            raw = self._request(
                "query",
                query={"query": "ASK {}"},
                accept="application/sparql-results+json",
            )
            payload = json.loads(raw)
            ok = bool(payload.get("boolean", True))
            return {
                "reachable": ok,
                "dataset_url": self.dataset_url,
                "latency_ms": round((monotonic() - started) * 1000.0, 2),
                "error": None if ok else "Unexpected SPARQL ASK response",
            }
        except (HTTPError, URLError, OSError, ValueError) as err:
            return {
                "reachable": False,
                "dataset_url": self.dataset_url,
                "latency_ms": round((monotonic() - started) * 1000.0, 2),
                "error": str(err),
            }

    def publish_kb(self, compiled: CompiledKB) -> dict[str, object]:
        """Idempotently publish the canonical KB OWL projection into a snapshot-pinned named graph."""
        snap = compiled.snapshot
        graph_iri = require_iri(f"urn:tradegraph:kb-snapshot:{snap.sha256}")
        ask_q = f"ASK {{ GRAPH {graph_iri.n3()} {{ ?s ?p ?o }} }}"
        ask_res = json.loads(self._request(
            "query", query={"query": ask_q}, accept="application/sparql-results+json",
        ))
        owl_graph = compiled.dataset.graph(compiled.owl_projection_ref)
        if ask_res.get("boolean") is True:
            return {
                "graph_iri": str(graph_iri),
                "kb_version": snap.version,
                "kb_sha256": snap.sha256,
                "triples": len(owl_graph),
                "status": "ALREADY_SYNCED",
            }
        nt_bytes = owl_graph.serialize(format="nt", encoding="utf-8")
        self._request(
            "data",
            query={"graph": str(graph_iri)},
            body=nt_bytes,
            content_type="application/n-triples; charset=utf-8",
            method="PUT",
        )
        return {
            "graph_iri": str(graph_iri),
            "kb_version": snap.version,
            "kb_sha256": snap.sha256,
            "triples": len(owl_graph),
            "status": "PUBLISHED",
        }

    def list_case_revisions(self, *, limit: int = 50) -> list[dict[str, object]]:
        """Query Fuseki across named graphs for published TradeCaseRevision summaries."""
        query = f"""
        PREFIX tg: <urn:tradegraph:kb#>
        SELECT DISTINCT ?graph ?case ?revision ?revNum ?kbVersion ?kbSha ?agreement ?procedure ?product ?txAt
        WHERE {{
            GRAPH ?graph {{
                ?ctx a tg:AssessmentContext ;
                    tg:assessesRevision ?revision ;
                    tg:usesKnowledgeRevision ?kb .
                ?revision tg:revisionOf ?case ;
                    tg:caseRevisionNumber ?revNum .
                OPTIONAL {{ ?kb tg:knowledgeVersion ?kbVersion . }}
                OPTIONAL {{ ?kb tg:snapshotSha256 ?kbSha . }}
                OPTIONAL {{ ?ctx tg:selectedAgreement ?agreement . }}
                OPTIONAL {{ ?ctx tg:selectedProcedure ?procedure . }}
                OPTIONAL {{ ?ctx tg:assessesProduct ?product . }}
                OPTIONAL {{ ?ctx tg:transactionAt ?txAt . }}
            }}
        }}
        ORDER BY DESC(?txAt) DESC(?revNum)
        LIMIT {max(1, int(limit))}
        """
        response = json.loads(self._request(
            "query", query={"query": query}, accept="application/sparql-results+json",
        ))
        items: list[dict[str, object]] = []
        for row in response.get("results", {}).get("bindings", []):
            def _val(key: str) -> str | None:
                cell = row.get(key)
                return cell["value"] if isinstance(cell, dict) and "value" in cell else None

            case_iri = _val("case") or ""
            items.append({
                "case_id": case_iri.removeprefix("urn:tradegraph:case:"),
                "revision": int(_val("revNum") or "1"),
                "context_graph": _val("graph"),
                "kb_version": _val("kbVersion"),
                "kb_sha256": _val("kbSha"),
                "agreement_iri": _val("agreement"),
                "procedure_iri": _val("procedure"),
                "product_iri": _val("product"),
                "transaction_at": _val("txAt"),
            })
        return items

    def publish(self, snapshot: CaseSnapshot) -> CaseGraphs:
        """Publish all named graphs in one guarded SPARQL Update request."""
        graphs = project_case_graphs(snapshot)
        blocks = [
            f"GRAPH {graph.identifier.n3()} {{\n{serialize_assertions(graph)}}}"
            for graph in (graphs.facts, graphs.context, graphs.evidence)
            if graph
        ]
        body = "\n".join(blocks)
        digest = sha256(body.encode("utf-8")).hexdigest()
        publication = URIRef(f"{graphs.context.identifier}:publication").n3()
        digest_property = CASE_DATA.snapshotDigest.n3()
        marker = f"{publication} {digest_property}"
        update = f"""
            INSERT {{
                {body}
                GRAPH {publication} {{ {marker} {Literal(digest).n3()} . }}
            }}
            WHERE {{
                FILTER NOT EXISTS {{ GRAPH {publication} {{ {marker} ?digest }} }}
            }}
        """
        self._request(
            "update", body=update.encode("utf-8"), content_type="application/sparql-update; charset=utf-8",
        )
        query = f"SELECT ?digest WHERE {{ GRAPH {publication} {{ {marker} ?digest }} }}"
        response = json.loads(self._request(
            "query", query={"query": query}, accept="application/sparql-results+json",
        ))
        stored_digests = {row["digest"]["value"] for row in response["results"]["bindings"]}
        if stored_digests != {digest}:
            raise GraphRevisionConflict(f"Cannot publish a different projection for {graphs.context.identifier}")
        return graphs

    def load_graph(self, graph_iri: URIRef) -> Graph:
        name = require_iri(graph_iri)
        try:
            body = self._request("data", query={"graph": str(name)}, accept="application/n-triples")
        except HTTPError as error:
            if error.code != 404:
                raise
            return Graph(identifier=name)
        graph = Graph(identifier=name)
        for triple in parse_turtle(body, str(name)):
            graph.add(triple)
        return graph

    def _request(self, operation, *, query=None, body=None, content_type=None, accept=None, method=None):
        url = f"{self.dataset_url}/{operation}"
        if query is not None:
            url = f"{url}?{urlencode(query)}"
        headers = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if accept is not None:
            headers["Accept"] = accept
        auth = self._auth_header()
        if auth is not None:
            headers["Authorization"] = auth
        req = Request(url, data=body, headers=headers, method=method)
        with urlopen(req, timeout=self.timeout) as response:
            return response.read()
