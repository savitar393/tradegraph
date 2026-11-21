"""Persist immutable case revisions and their assessment context in SQLite."""

from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from rdflib import Graph, Literal, URIRef

from tradegraph.cases.facts import (
    apply_fact_patch,
    parse_input_facts,
    parse_persisted_facts,
    rebase_triple,
    require_iri,
    revision_ref,
    serialize_assertions,
)
from tradegraph.contracts import AssessmentContext, CaseSnapshot, EvidenceClaim, FactPatch


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    head_revision INTEGER NOT NULL,
    FOREIGN KEY (case_id, head_revision) REFERENCES revisions(case_id, revision)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS revisions (
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    parent_revision INTEGER,
    facts_turtle TEXT NOT NULL,
    context_json TEXT NOT NULL,
    claims_json TEXT NOT NULL,
    evidence_scope_closed INTEGER CHECK (evidence_scope_closed IN (0, 1)),
    created_at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (case_id, revision),
    FOREIGN KEY (case_id, parent_revision) REFERENCES revisions(case_id, revision)
);
"""

TIME_FIELDS = ("transaction_at", "evaluation_at")
IRI_FIELDS = ("jurisdiction_iri", "procedure_iri", "agreement_iri", "product_iri", "batch_iri")
VERIFICATION_STATUSES = {"UNVERIFIED", "VERIFIED", "REJECTED"}


class CaseNotFound(LookupError):
    pass


class RevisionConflict(ValueError):
    def __init__(self, case_id: str, base_revision: int, head_revision: int):
        super().__init__(f"Case {case_id} is at revision {head_revision}, supplied base {base_revision}")
        self.case_id = case_id
        self.base_revision = base_revision
        self.head_revision = head_revision


class CaseRepository:
    def __init__(self, database: Path):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def _connection(self):
        with closing(sqlite3.connect(self.database, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection

    def create_case(
        self,
        facts_turtle: str,
        context: AssessmentContext,
        *,
        evidence_claims: tuple[EvidenceClaim, ...] = (),
        evidence_scope_closed: bool | None = None,
    ) -> CaseSnapshot:
        context = _validate_context(context)
        claims = _validate_claims(evidence_claims)
        _validate_closure(context, evidence_scope_closed)
        case_id = str(uuid4())
        facts = serialize_assertions(parse_input_facts(facts_turtle, case_id))
        snapshot = _snapshot(case_id, 1, None, facts, context, claims, evidence_scope_closed, None)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO cases VALUES (?, 1)", (case_id,))
            _insert_revision(connection, snapshot)
        return snapshot

    def revise_case(
        self, case_id: str, base_revision: int, patch: FactPatch, *, branch: bool = False
    ) -> CaseSnapshot:
        if not isinstance(patch.reason, str) or not patch.reason.strip():
            raise ValueError("A case revision requires a reason")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            head = _head(connection, case_id)
            if not branch and base_revision != head:
                raise RevisionConflict(case_id, base_revision, head)
            base = _load_revision(connection, case_id, base_revision)
            graph = parse_persisted_facts(base.facts_turtle, case_id)
            apply_fact_patch(graph, patch)
            facts = serialize_assertions(graph)
            context = base.context if patch.context is None else _validate_context(patch.context)
            claims, closed = _revise_evidence(base, patch, facts, context)
            revision = connection.execute(
                "SELECT max(revision) + 1 FROM revisions WHERE case_id = ?", (case_id,)
            ).fetchone()[0]
            if branch:
                source, target = revision_ref(case_id, base_revision), revision_ref(case_id, revision)
                rebased = Graph()
                for triple in graph:
                    rebased.add(rebase_triple(triple, source, target))
                facts = serialize_assertions(rebased)
            snapshot = _snapshot(case_id, revision, base_revision, facts, context, claims, closed, patch.reason)
            _insert_revision(connection, snapshot)
            if not branch:
                connection.execute("UPDATE cases SET head_revision = ? WHERE case_id = ?", (revision, case_id))
        return snapshot

    def load_case(self, case_id: str, revision: int) -> CaseSnapshot:
        with self._connection() as connection:
            return _load_revision(connection, case_id, revision)

    def load_head(self, case_id: str) -> CaseSnapshot:
        with self._connection() as connection:
            return _load_revision(connection, case_id, _head(connection, case_id))

    def list_revisions(self, case_id: str) -> list[CaseSnapshot]:
        with self._connection() as connection:
            _head(connection, case_id)
            rows = connection.execute(
                "SELECT revision FROM revisions WHERE case_id = ? ORDER BY revision ASC",
                (case_id,),
            ).fetchall()
            return [_load_revision(connection, case_id, int(row["revision"])) for row in rows]

    def list_cases(self, *, limit: int = 50) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT c.case_id, c.head_revision, r.context_json, r.created_at, r.reason
                FROM cases c
                JOIN revisions r ON r.case_id = c.case_id AND r.revision = c.head_revision
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            out: list[dict[str, object]] = []
            for row in rows:
                ctx = json.loads(row["context_json"])
                out.append({
                    "case_id": row["case_id"],
                    "head_revision": int(row["head_revision"]),
                    "kb_version": ctx.get("kb_version"),
                    "kb_sha256": ctx.get("kb_sha256"),
                    "agreement_iri": ctx.get("agreement_iri"),
                    "procedure_iri": ctx.get("procedure_iri"),
                    "product_iri": ctx.get("product_iri"),
                    "transaction_at": ctx.get("transaction_at"),
                    "created_at": row["created_at"],
                    "reason": row["reason"],
                })
            return out


def _validate_context(context):
    if not isinstance(context.kb_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", context.kb_sha256):
        raise ValueError("Assessment context requires a KB SHA-256 digest")
    if not isinstance(context.engine_profile_id, str) or not context.engine_profile_id.strip():
        raise ValueError("Assessment context requires an engine profile ID")
    for field in ("kb_version", "evidence_scope_id"):
        value = getattr(context, field)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{field} must be nonempty when provided")
    for field in TIME_FIELDS:
        value = getattr(context, field)
        if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
            raise ValueError(f"{field} requires a timezone-aware datetime")
    for field in IRI_FIELDS:
        value = getattr(context, field)
        if value is not None:
            require_iri(value)
    return replace(context, kb_sha256=context.kb_sha256.lower())


def _validate_claims(claims):
    seen = set()
    for claim in claims:
        for field in ("claim_iri", "subject_iri", "predicate_iri", "source_iri"):
            require_iri(getattr(claim, field))
        if claim.claim_iri in seen:
            raise ValueError(f"Duplicate evidence claim {claim.claim_iri}")
        seen.add(claim.claim_iri)
        if claim.verification_status not in VERIFICATION_STATUSES:
            raise ValueError(f"Invalid evidence verification status {claim.verification_status!r}")
        if isinstance(claim.value, URIRef):
            require_iri(claim.value)
        elif isinstance(claim.value, Literal):
            if claim.value.datatype is not None:
                require_iri(claim.value.datatype)
        elif claim.value is not None:
            raise ValueError("Evidence claim value requires an IRI, RDF literal, or null")
    return tuple(sorted(claims, key=lambda claim: claim.claim_iri))


def _validate_closure(context, closed):
    if closed is not None and (not isinstance(closed, bool) or context.evidence_scope_id is None):
        raise ValueError("Evidence closure requires a boolean and a named evidence_scope_id")


def _revise_evidence(base, patch, facts, context):
    if patch.evidence_claims is not None:
        claims = _validate_claims(patch.evidence_claims)
    elif base.context.evidence_scope_id == context.evidence_scope_id:
        claims = base.evidence_claims
    else:
        claims = ()

    closed = patch.evidence_scope_closed
    if closed is None and context == base.context and claims == base.evidence_claims and facts == base.facts_turtle:
        closed = base.evidence_scope_closed
    _validate_closure(context, closed)
    return claims, closed


def _snapshot(case_id, revision, parent_revision, facts, context, claims, closed, reason):
    return CaseSnapshot(
        case_id, revision, parent_revision, facts,
        tuple(claim.claim_iri for claim in claims), context, claims, closed,
        datetime.now(UTC), reason,
    )


def _encode_claim(claim):
    data = asdict(claim)
    if isinstance(claim.value, URIRef):
        data["value"] = {"iri": str(claim.value)}
    elif isinstance(claim.value, Literal):
        data["value"] = {
            "lexical": str(claim.value),
            "datatype": str(claim.value.datatype) if claim.value.datatype is not None else None,
            "language": claim.value.language,
        }
    return data


def _decode_claim(data):
    value = data["value"]
    if value is not None:
        if "iri" in value:
            data["value"] = URIRef(value["iri"])
        else:
            data["value"] = Literal(
                value["lexical"], datatype=value["datatype"], lang=value["language"], normalize=False,
            )
    return EvidenceClaim(**data)


def _insert_revision(connection, snapshot):
    context = asdict(snapshot.context)
    for field in TIME_FIELDS:
        if context[field] is not None:
            context[field] = context[field].isoformat()
    connection.execute(
        """INSERT INTO revisions (
            case_id, revision, parent_revision, facts_turtle, context_json, claims_json,
            evidence_scope_closed, created_at, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            snapshot.case_id, snapshot.revision, snapshot.parent_revision, snapshot.facts_turtle,
            json.dumps(context), json.dumps([_encode_claim(claim) for claim in snapshot.evidence_claims]),
            snapshot.evidence_scope_closed, snapshot.created_at.isoformat(), snapshot.reason,
        ),
    )


def _head(connection, case_id):
    row = connection.execute("SELECT head_revision FROM cases WHERE case_id = ?", (case_id,)).fetchone()
    if row is None:
        raise CaseNotFound(f"Unknown case {case_id}")
    return row["head_revision"]


def _load_revision(connection, case_id, revision):
    row = connection.execute(
        "SELECT * FROM revisions WHERE case_id = ? AND revision = ?", (case_id, revision)
    ).fetchone()
    if row is None:
        raise CaseNotFound(f"Unknown revision {revision} for case {case_id}")
    context = json.loads(row["context_json"])
    for field in TIME_FIELDS:
        if context[field] is not None:
            context[field] = datetime.fromisoformat(context[field])
    claims = tuple(_decode_claim(claim) for claim in json.loads(row["claims_json"]))
    closed = row["evidence_scope_closed"]
    return CaseSnapshot(
        row["case_id"], row["revision"], row["parent_revision"], row["facts_turtle"],
        tuple(claim.claim_iri for claim in claims), AssessmentContext(**context), claims,
        None if closed is None else bool(closed), datetime.fromisoformat(row["created_at"]), row["reason"],
    )
