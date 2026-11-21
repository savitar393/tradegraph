"""Dual SQLite + Apache Jena Fuseki TDB2 persistence and readiness probes for TradeGraph compliance cases."""

from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import shutil

from rdflib import Graph, Literal, Namespace, RDF, URIRef, XSD

from tradegraph.cases.facts import TG, serialize_assertions
from tradegraph.cases.repository import CaseNotFound, CaseRepository
from tradegraph.contracts import AssessmentContext, CaseSnapshot, CompiledKB
from tradegraph.graph.store import FusekiGraphStore, project_case_graphs
from tradegraph.optimization.export_service import ExportEvaluationResult, ExportSpec
from tradegraph.optimization.service import OptimizationResult, ShipmentSpec


AUDIT = Namespace("urn:tradegraph:audit#")


def _iso_to_utc_datetime(iso_date: str) -> datetime:
    return datetime.fromisoformat(f"{iso_date.strip()}T00:00:00+00:00").astimezone(UTC)


def _escape_slug(raw: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw.strip()) or "item"


class CasePersistenceService:
    """Persists immutable TradeGraph case snapshots to SQLite and/or Apache Jena Fuseki TDB2."""

    def __init__(
        self,
        *,
        repository: CaseRepository | None = None,
        fuseki: FusekiGraphStore | None = None,
    ):
        self.repository = repository
        self.fuseki = fuseki

    @classmethod
    def from_env(
        cls,
        *,
        case_db_path: Path | None = None,
        fuseki_url: str | None = None,
        fuseki_user: str | None = None,
        fuseki_password: str | None = None,
    ) -> CasePersistenceService:
        db_raw = str(case_db_path) if case_db_path is not None else os.environ.get("TRADEGRAPH_CASE_DB_PATH", "").strip()
        repo = CaseRepository(Path(db_raw)) if db_raw else None

        url_raw = (fuseki_url if fuseki_url is not None else os.environ.get("TRADEGRAPH_FUSEKI_URL", "")).strip()
        store = (
            FusekiGraphStore(url_raw, username=fuseki_user, password=fuseki_password)
            if url_raw else None
        )
        return cls(repository=repo, fuseki=store)

    @property
    def enabled(self) -> bool:
        return self.repository is not None or self.fuseki is not None

    def readiness(self, compiled: CompiledKB, classpath: str) -> tuple[bool, dict[str, object]]:
        """Run production readiness checks across KB, Java 21, SQLite, and Fuseki."""
        java_bin = shutil.which("java")
        cp_entries = [part for part in classpath.split(os.pathsep) if part.strip()]
        kb_ok = bool(compiled.snapshot.sha256 and compiled.snapshot.version)
        java_ok = bool(java_bin and cp_entries)

        sqlite_info: dict[str, object] = {"configured": self.repository is not None, "ready": True}
        if self.repository is not None:
            try:
                self.repository.list_cases(limit=1)
                sqlite_info["path"] = str(self.repository.database)
            except Exception as err:
                sqlite_info["ready"] = False
                sqlite_info["error"] = str(err)

        fuseki_info: dict[str, object] = {"configured": self.fuseki is not None, "reachable": None}
        if self.fuseki is not None:
            ping = self.fuseki.ping()
            fuseki_info.update(ping)

        all_ready = (
            kb_ok
            and java_ok
            and bool(sqlite_info["ready"])
            and (True if self.fuseki is None else bool(fuseki_info.get("reachable")))
        )
        return all_ready, {
            "status": "ready" if all_ready else "degraded",
            "kb": {
                "loaded": kb_ok,
                "version": compiled.snapshot.version,
                "sha256": compiled.snapshot.sha256,
            },
            "java": {
                "available": java_ok,
                "binary": java_bin,
                "classpath_entries": len(cp_entries),
            },
            "sqlite": sqlite_info,
            "fuseki": fuseki_info,
        }

    def sync_kb(self, compiled: CompiledKB) -> dict[str, object]:
        if self.fuseki is None:
            raise RuntimeError("Fuseki persistence is not configured (set TRADEGRAPH_FUSEKI_URL).")
        return self.fuseki.publish_kb(compiled)

    def _commit_snapshot(
        self,
        facts_graph: Graph,
        context: AssessmentContext,
    ) -> dict[str, object]:
        if not self.enabled:
            raise RuntimeError(
                "Case persistence is not configured (set TRADEGRAPH_CASE_DB_PATH and/or TRADEGRAPH_FUSEKI_URL)."
            )
        facts_turtle = serialize_assertions(facts_graph)
        if self.repository is not None:
            snapshot = self.repository.create_case(facts_turtle, context)
            sqlite_stored = True
        else:
            from uuid import uuid4

            snapshot = CaseSnapshot(
                case_id=str(uuid4()),
                revision=1,
                parent_revision=None,
                facts_turtle=facts_turtle,
                evidence_claim_ids=(),
                context=context,
                evidence_claims=(),
                evidence_scope_closed=None,
                created_at=datetime.now(UTC),
                reason=None,
            )
            sqlite_stored = False

        graphs = project_case_graphs(snapshot)
        fuseki_published = False
        if self.fuseki is not None:
            graphs = self.fuseki.publish(snapshot)
            fuseki_published = True

        return {
            "case_id": snapshot.case_id,
            "revision": snapshot.revision,
            "kb_version": context.kb_version,
            "kb_sha256": context.kb_sha256,
            "sqlite_stored": sqlite_stored,
            "fuseki_published": fuseki_published,
            "facts_graph": str(graphs.facts.identifier),
            "context_graph": str(graphs.context.identifier),
            "facts_triples": len(graphs.facts),
            "context_triples": len(graphs.context),
            "created_at": snapshot.created_at.isoformat(),
        }

    def persist_import_case(
        self,
        compiled: CompiledKB,
        spec: ShipmentSpec,
        result: OptimizationResult,
        *,
        reason: str | None = None,
    ) -> dict[str, object]:
        prod_iri = AUDIT[f"import-product-{_escape_slug(result.hs_code)}"]
        ship_iri = AUDIT[f"import-shipment-{_escape_slug(result.hs_code)}-{spec.as_of_date}"]
        facts = Graph()
        facts.add((prod_iri, RDF.type, TG.Product))
        facts.add((prod_iri, TG.hasTariffClassification, URIRef(result.hs_iri)))
        facts.add((ship_iri, RDF.type, TG.Shipment))
        facts.add((ship_iri, AUDIT.tradeDirection, Literal("IMPORT")))
        facts.add((ship_iri, AUDIT.hsCode, Literal(result.hs_code)))
        facts.add((ship_iri, AUDIT.dispatchCountry, Literal(result.dispatch_country)))
        facts.add((ship_iri, AUDIT.destinationCountry, Literal(result.destination_country)))
        facts.add((ship_iri, AUDIT.recommendedAgreement, Literal(result.recommended_agreement)))
        facts.add((ship_iri, AUDIT.lartasStatus, Literal(result.lartas.status)))
        if spec.cif_value is not None:
            facts.add((ship_iri, AUDIT.cifValue, Literal(f"{spec.cif_value:.2f}", datatype=XSD.decimal)))
            facts.add((ship_iri, AUDIT.currency, Literal(spec.currency)))
        winner = next((s for s in result.schemes if s.agreement == result.recommended_agreement), None)
        if winner and winner.tax_breakdown is not None:
            facts.add((ship_iri, AUDIT.totalImportTaxes, Literal(f"{winner.tax_breakdown.total_import_taxes:.2f}", datatype=XSD.decimal)))
        if winner and winner.total_tax_savings is not None:
            facts.add((ship_iri, AUDIT.netTaxSavings, Literal(f"{winner.total_tax_savings:.2f}", datatype=XSD.decimal)))
        if reason:
            facts.add((ship_iri, AUDIT.auditNote, Literal(reason)))

        agr_iri = (
            str(TG[result.recommended_agreement])
            if result.recommended_agreement not in {"MFN", "PROHIBITED"} else None
        )
        ctx = AssessmentContext(
            kb_sha256=compiled.snapshot.sha256,
            engine_profile_id=compiled.snapshot.engine_profile_id,
            kb_version=compiled.snapshot.version,
            jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
            procedure_iri=str(TG.importForUse),
            agreement_iri=agr_iri,
            product_iri=str(prod_iri),
            transaction_at=_iso_to_utc_datetime(spec.as_of_date),
            evaluation_at=datetime.now(UTC),
        )
        record = self._commit_snapshot(facts, ctx)
        record["mode"] = "import"
        record["hs_code"] = result.hs_code
        record["recommended_agreement"] = result.recommended_agreement
        record["lartas_status"] = result.lartas.status
        return record

    def persist_export_case(
        self,
        compiled: CompiledKB,
        spec: ExportSpec,
        result: ExportEvaluationResult,
        *,
        reason: str | None = None,
    ) -> dict[str, object]:
        prod_iri = AUDIT[f"export-product-{_escape_slug(result.hs_code)}"]
        ship_iri = AUDIT[f"export-shipment-{_escape_slug(result.hs_code)}-{spec.as_of_date}"]
        facts = Graph()
        facts.add((prod_iri, RDF.type, TG.Product))
        facts.add((prod_iri, TG.hasTariffClassification, URIRef(result.hs_iri)))
        facts.add((ship_iri, RDF.type, TG.Shipment))
        facts.add((ship_iri, AUDIT.tradeDirection, Literal("EXPORT")))
        facts.add((ship_iri, AUDIT.hsCode, Literal(result.hs_code)))
        facts.add((ship_iri, AUDIT.originCountry, Literal(result.origin_country)))
        facts.add((ship_iri, AUDIT.destinationCountry, Literal(result.destination_country)))
        facts.add((ship_iri, AUDIT.recommendedAgreement, Literal(result.recommended_agreement)))
        facts.add((ship_iri, AUDIT.exportLartasStatus, Literal(result.export_lartas.status)))
        facts.add((ship_iri, AUDIT.exportDutyStatus, Literal(result.export_duty.status)))
        if result.export_duty.estimated_duty_usd is not None:
            facts.add((ship_iri, AUDIT.estimatedExportDutyUsd, Literal(f"{result.export_duty.estimated_duty_usd:.2f}", datatype=XSD.decimal)))
        if result.transshipment_hub:
            facts.add((ship_iri, AUDIT.transshipmentHub, Literal(result.transshipment_hub)))
        if reason:
            facts.add((ship_iri, AUDIT.auditNote, Literal(reason)))

        agr_iri = (
            str(TG[result.recommended_agreement])
            if result.recommended_agreement not in {"NON_PREFERENTIAL", "PROHIBITED_EXPORT"} else None
        )
        ctx = AssessmentContext(
            kb_sha256=compiled.snapshot.sha256,
            engine_profile_id=compiled.snapshot.engine_profile_id,
            kb_version=compiled.snapshot.version,
            jurisdiction_iri=str(TG.jurisdictionIndonesiaCustoms),
            procedure_iri=str(TG.permanentExport),
            agreement_iri=agr_iri,
            product_iri=str(prod_iri),
            transaction_at=_iso_to_utc_datetime(spec.as_of_date),
            evaluation_at=datetime.now(UTC),
        )
        record = self._commit_snapshot(facts, ctx)
        record["mode"] = "export"
        record["hs_code"] = result.hs_code
        record["recommended_agreement"] = result.recommended_agreement
        record["export_lartas_status"] = result.export_lartas.status
        record["export_duty_status"] = result.export_duty.status
        return record

    def list_cases(self, *, limit: int = 50) -> dict[str, object]:
        if self.repository is not None:
            return {"source": "sqlite", "cases": self.repository.list_cases(limit=limit)}
        if self.fuseki is not None:
            return {"source": "fuseki", "cases": self.fuseki.list_case_revisions(limit=limit)}
        return {"source": "none", "cases": []}

    def get_case(self, case_id: str, *, revision: int | None = None, verify_fuseki: bool = False) -> dict[str, object]:
        if self.repository is None:
            raise CaseNotFound(f"SQLite case repository is not configured to load case {case_id}.")
        snapshot = (
            self.repository.load_case(case_id, revision)
            if revision is not None else self.repository.load_head(case_id)
        )
        graphs = project_case_graphs(snapshot)
        payload: dict[str, object] = {
            "case_id": snapshot.case_id,
            "revision": snapshot.revision,
            "parent_revision": snapshot.parent_revision,
            "kb_version": snapshot.context.kb_version,
            "kb_sha256": snapshot.context.kb_sha256,
            "agreement_iri": snapshot.context.agreement_iri,
            "procedure_iri": snapshot.context.procedure_iri,
            "product_iri": snapshot.context.product_iri,
            "transaction_at": snapshot.context.transaction_at.isoformat() if snapshot.context.transaction_at else None,
            "created_at": snapshot.created_at.isoformat(),
            "facts_graph": str(graphs.facts.identifier),
            "context_graph": str(graphs.context.identifier),
            "facts_triples": len(graphs.facts),
            "context_triples": len(graphs.context),
            "facts_turtle": snapshot.facts_turtle,
        }
        if verify_fuseki and self.fuseki is not None:
            remote_facts = self.fuseki.load_graph(graphs.facts.identifier)
            remote_ctx = self.fuseki.load_graph(graphs.context.identifier)
            payload["fuseki_verification"] = {
                "verified": len(remote_facts) == len(graphs.facts) and len(remote_ctx) == len(graphs.context),
                "remote_facts_triples": len(remote_facts),
                "remote_context_triples": len(remote_ctx),
            }
        return payload
