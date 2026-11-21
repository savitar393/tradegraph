"""Run the pinned Java engine and query the resulting named facts locally."""

import atexit
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
from time import monotonic

from rdflib import Graph, Literal, OWL, URIRef
from rdflib.graph import ReadOnlyGraphAggregate

from tradegraph.contracts import (
    AxiomWitness, CaseSnapshot, CompiledKB, FactSupport, Finding, InferenceResult, ReasoningContext,
    ReasoningJob, RuleWitness,
)
from tradegraph.graph.store import CaseGraphs, project_case_graphs
from tradegraph.kb.loader import ENGINE_PROFILE_ID, KnowledgeBaseError, select_rules
from tradegraph.kb.queries import UnsupportedQuery, context_bindings, prepare_local_query
from tradegraph.kb.validation import resolve_runtime_classpath
from tradegraph.reasoning.slicing import slice_owl_for_case


def _prepare_queries(kb: CompiledKB, query_ids: tuple[str, ...], graphs: CaseGraphs):
    available = context_bindings(graphs.context)
    queries = {}
    for query_id in dict.fromkeys(query_ids):
        definition = kb.query_catalog.get(query_id)
        if definition is None:
            raise UnsupportedQuery(f"Unknown query ID {query_id}.")
        query = prepare_local_query(definition.sparql, "AskQuery", "SelectQuery")
        missing = [name for name in definition.parameters if available.get(name) is None]
        if missing:
            raise UnsupportedQuery(f"Query {query_id} is missing context parameters {', '.join(missing)}.")
        queries[query_id] = query, {name: available[name] for name in definition.parameters}
    return queries


def _fact_from_wire(fact):
    return URIRef(fact["subject"]), URIRef(fact["predicate"]), _term_from_wire(fact)


def _term_from_wire(term):
    value, datatype, language = (term[name] for name in ("value", "datatype", "language"))
    if language is not None:
        return Literal(value, lang=language, normalize=False)
    if datatype is not None:
        return Literal(value, datatype=URIRef(datatype), normalize=False)
    return URIRef(value)


def _fact_to_wire(finding):
    subject, predicate, value = finding.triple
    return {
        "subject": str(subject), "predicate": str(predicate), "value": str(value),
        "datatype": str(value.datatype) if isinstance(value, Literal) and value.datatype else None,
        "language": value.language if isinstance(value, Literal) else None,
        "kind": finding.kind, "iteration": finding.support.iteration,
    }


class _PersistentJavaWorker:
    """Reuses a single warm JVM running id.tradegraph.ReasoningWorker over stdio JSON-lines."""

    def __init__(self, java: str, classpath: str):
        self.java = java
        self.classpath = classpath
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._reader_pool: ThreadPoolExecutor | None = None

    def _start(self) -> None:
        self._stderr_tail.clear()
        self._process = subprocess.Popen(
            [self.java, "-cp", self.classpath, "id.tradegraph.ReasoningWorker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        proc = self._process

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                for line in proc.stderr:
                    self._stderr_tail.append(line.rstrip())
            except Exception:
                pass

        threading.Thread(target=_drain_stderr, daemon=True).start()
        if self._reader_pool is None:
            self._reader_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tg-java-worker")

    def _kill(self) -> None:
        proc = self._process
        self._process = None
        if proc is not None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        pool = self._reader_pool
        self._reader_pool = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def execute(self, entrypoint: str, request_path: Path, result_path: Path, timeout: float) -> None:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._kill()
                self._start()
            proc = self._process
            assert proc is not None and proc.stdin is not None and proc.stdout is not None
            assert self._reader_pool is not None
            command = json.dumps({
                "entrypoint": entrypoint,
                "requestPath": str(request_path),
                "resultPath": str(result_path),
            })
            try:
                proc.stdin.write(command + "\n")
                proc.stdin.flush()
            except OSError as error:
                tail = "\n".join(self._stderr_tail)[-1500:]
                self._kill()
                raise RuntimeError(f"Persistent semantic worker pipe failed: {error}. {tail}") from error

            future = self._reader_pool.submit(proc.stdout.readline)
            try:
                raw_reply = future.result(timeout=timeout)
            except FutureTimeout as error:
                self._kill()
                raise TimeoutError from error

            if not raw_reply:
                code = proc.poll()
                tail = "\n".join(self._stderr_tail)[-1500:]
                self._kill()
                raise RuntimeError(f"Semantic worker exited unexpectedly (status {code}). {tail}")

            reply = json.loads(raw_reply)
            if not reply.get("ok"):
                raise RuntimeError(f"Semantic worker error: {reply.get('error', 'unknown error')}")

    def close(self) -> None:
        with self._lock:
            proc = self._process
            if proc is not None and proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write("exit\n")
                    proc.stdin.flush()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            self._kill()


_WORKERS_LOCK = threading.Lock()
_WORKERS: dict[tuple[str, str], _PersistentJavaWorker] = {}


def _get_worker(java: str, classpath: str) -> _PersistentJavaWorker:
    key = (java, classpath)
    with _WORKERS_LOCK:
        worker = _WORKERS.get(key)
        if worker is None:
            worker = _PersistentJavaWorker(java, classpath)
            _WORKERS[key] = worker
        return worker


def _shutdown_all_workers() -> None:
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(_shutdown_all_workers)


class ReasoningClient:
    def __init__(self, kb: CompiledKB, classpath: str, *, java: str = "java", persistent_worker: bool = True):
        self.kb = kb
        self.classpath = classpath
        self.java = java
        self.persistent_worker = persistent_worker
        self._fixed_kb_nodes: frozenset[URIRef] | None = None

    @staticmethod
    def shutdown_workers() -> None:
        """Terminate any pooled persistent Java worker processes."""
        _shutdown_all_workers()

    @staticmethod
    def build_classpath(pom: Path, output: Path, *, maven: str | None = None) -> str:
        """Compile the adapter and resolve the pinned runtime classpath (Maven or precompiled .m2 fallback)."""
        try:
            return resolve_runtime_classpath(pom, output=output, maven=maven)
        except OSError as error:
            raise RuntimeError(str(error)) from error

    def _fixed_nodes(self) -> frozenset[URIRef]:
        if self._fixed_kb_nodes is None:
            schema = self.kb.dataset.graph(self.kb.owl_projection_ref)
            definitions = self.kb.dataset.graph(self.kb.definitions_graph_ref)
            self._fixed_kb_nodes = frozenset(
                node for graph in (schema, definitions) for node in graph.subjects() if isinstance(node, URIRef)
            )
        return self._fixed_kb_nodes

    def reason(self, job: ReasoningJob) -> InferenceResult:
        deadline = monotonic() + job.limits.timeout_ms / 1000
        snapshot = job.case_snapshot
        graphs = project_case_graphs(snapshot)
        context = ReasoningContext(
            job.kb_sha256, self.kb.snapshot.engine_profile_id, snapshot.case_id,
            snapshot.revision, str(graphs.context.identifier), snapshot.context,
        )
        if self.kb.snapshot.sha256 != job.kb_sha256 or snapshot.context.kb_sha256 != job.kb_sha256:
            return InferenceResult("FAILED", None, context, diagnostics=("KB snapshot hash mismatch.",))
        if (context.engine_profile_id != ENGINE_PROFILE_ID
                or snapshot.context.engine_profile_id != context.engine_profile_id):
            return InferenceResult("UNSUPPORTED", None, context, diagnostics=("Engine profile mismatch.",))

        consistency = None
        try:
            rules, counts = select_rules(self.kb, snapshot.context)
            queries = _prepare_queries(self.kb, job.query_ids, graphs)
            schema = self.kb.dataset.graph(self.kb.owl_projection_ref)
            if self.kb.policy_catalog:
                fixed = self._fixed_nodes()
                for subject, predicate, value in graphs.facts:
                    if (subject in fixed and (subject, predicate, value) not in schema) or (
                        predicate == OWL.sameAs and (subject in fixed or value in fixed)
                    ):
                        raise KnowledgeBaseError(f"Case assertion changes a fixed KB resource at {subject}.")
            sliced_schema = slice_owl_for_case(schema, graphs.facts, graphs.context, rules, for_reasoning=True)
            if sliced_schema is schema:
                core = schema + graphs.facts + graphs.context
            else:
                core = sliced_schema
                for triple in graphs.facts:
                    core.add(triple)
                for triple in graphs.context:
                    core.add(triple)
            with TemporaryDirectory(prefix="tradegraph-reason-") as directory:
                wire = self._execute(Path(directory), job, context, core, rules, counts, deadline)
            status, consistency = wire["executionStatus"], wire["consistency"]
            diagnostics = tuple(wire["diagnostics"])
            if status != "CONVERGED":
                return InferenceResult(status, consistency, context, diagnostics=diagnostics)
            if consistency != "CONSISTENT" or wire["completenessClaim"] != "NOT_CLAIMED":
                raise ValueError("Inconsistent semantic adapter response.")

            inferred = []
            supports = []
            for fact in wire["inferredFacts"]:
                inferred.append(_fact_from_wire(fact))
                supports.append(FactSupport(context, fact["kind"], fact["iteration"]))
            query_results = {}
            if queries:
                query_delta = Graph()
                for grp in (graphs.facts, graphs.context):
                    for triple in grp:
                        if triple not in schema:
                            query_delta.add(triple)
                for fact in inferred:
                    if fact not in schema:
                        query_delta.add(fact)
                query_graph = ReadOnlyGraphAggregate([schema, query_delta])
                for query_id, (query, bindings) in queries.items():
                    result = query_graph.query(query, initBindings=bindings)
                    if result.type == "ASK":
                        query_results[query_id] = bool(result.askAnswer)
                    else:
                        query_results[query_id] = tuple(row.asdict() for row in result)
                    self._remaining(deadline)
            return InferenceResult(
                status, consistency, context, tuple(inferred), query_results, tuple(supports), diagnostics=diagnostics,
            )
        except (KnowledgeBaseError, UnsupportedQuery) as error:
            return InferenceResult("UNSUPPORTED", consistency, context, diagnostics=(str(error),))
        except (TimeoutError, subprocess.TimeoutExpired):
            return InferenceResult("TIMEOUT", consistency, context, diagnostics=("The reasoning budget expired.",))
        except Exception as error:
            return InferenceResult("FAILED", consistency, context, diagnostics=(f"{type(error).__name__}: {error}",))

    def replay_support(
        self, case: CaseSnapshot, finding: Finding, prior_findings: tuple[Finding, ...],
        *, deadline: float, max_owl_checks: int,
    ) -> FactSupport:
        """Request one verified witness using the facts present before the selected derivation."""
        support = finding.support
        context = support.context
        try:
            if (context.kb_sha256, context.engine_profile_id, context.case_id, context.case_revision) != (
                self.kb.snapshot.sha256, self.kb.snapshot.engine_profile_id, case.case_id, case.revision,
            ) or context.assessment_context != case.context:
                raise ValueError("Explanation snapshot mismatch.")
            graphs = project_case_graphs(case)
            if str(graphs.context.identifier) != context.assessment_context_iri:
                raise ValueError("Explanation context IRI mismatch.")
            rules, counts = select_rules(self.kb, case.context) if finding.kind == "RULE_DERIVED" else (Graph(), {})
            schema = self.kb.dataset.graph(self.kb.owl_projection_ref)
            core = slice_owl_for_case(schema, graphs.facts, graphs.context, rules, for_reasoning=True) + graphs.facts + graphs.context
            for prior in prior_findings:
                if prior.support.context != context:
                    raise ValueError("A premise belongs to another reasoning snapshot.")
                stage = prior.support.iteration
                if (prior.kind == "RULE_DERIVED" and stage < support.iteration) or (
                    finding.kind == "RULE_DERIVED" and prior.kind == "OWL_ENTAILED" and stage <= support.iteration
                ):
                    core.add(prior.triple)
            with TemporaryDirectory(prefix="tradegraph-justify-") as temporary:
                directory = Path(temporary)
                job = self._job_payload(directory, context, core, rules, counts, 1, deadline)
                wire = self._run_adapter(directory, "JustificationAdapter", {
                    "job": job, "target": _fact_to_wire(finding), "maxOwlChecks": max_owl_checks,
                }, deadline)
            if wire.get("context") != job["context"]:
                raise ValueError("The justification adapter returned a different context.")
            if wire["availability"] != "AVAILABLE":
                return replace(support, diagnostics=tuple(wire["diagnostics"]))
            axioms = tuple(AxiomWitness(
                tuple(axiom["axiomIds"]), tuple(axiom["sourceClauses"]), axiom["rendering"],
                _fact_from_wire(axiom["fact"]) if axiom["fact"] is not None else None,
            ) for axiom in wire["axioms"])
            witness = wire["rule"]
            rule = RuleWitness(
                witness["ruleId"], tuple(witness["sourceClauses"]),
                tuple((binding["variable"], _term_from_wire(binding["term"])) for binding in witness["bindings"]),
                tuple(_fact_from_wire(fact) for fact in witness["premises"]),
            ) if witness is not None else None
            return replace(support, explanation_status="AVAILABLE", axioms=axioms, rule=rule)
        except (TimeoutError, subprocess.TimeoutExpired):
            return replace(support, diagnostics=("The explanation time budget expired.",))
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
            return replace(support, diagnostics=(f"{type(error).__name__}: {error}",))

    def _execute(self, directory, job, context, core: Graph, rules: Graph, counts, deadline):
        payload = self._job_payload(directory, context, core, rules, counts, job.limits.max_iterations, deadline)
        result = self._run_adapter(directory, "ReasoningMain", payload, deadline)
        if result.get("context") != payload["context"]:
            raise ValueError("The semantic adapter returned a different context.")
        return result

    def _job_payload(self, directory, context, core, rules, counts, max_iterations, deadline):
        core_path = directory / "core.ttl"
        rules_path = directory / "rules.ttl"
        core.serialize(destination=core_path, format="nt", encoding="utf-8")
        if counts:
            rules.serialize(destination=rules_path, format="nt", encoding="utf-8")
        pinned = {
            "kbSha256": context.kb_sha256,
            "engineProfileId": context.engine_profile_id,
            "caseId": context.case_id,
            "caseRevision": context.case_revision,
            "contextIri": context.assessment_context_iri,
        }
        return {
            "context": pinned, "corePath": str(core_path), "rulesPath": str(rules_path), "ruleAtomCounts": counts,
            "maxIterations": max_iterations, "timeoutMs": max(1, int(self._remaining(deadline) * 1000)),
        }

    def _run_adapter(self, directory, entrypoint, payload, deadline):
        request_path = directory / "job.json"
        result_path = directory / "result.json"
        request_path.write_text(json.dumps(payload), encoding="utf-8")
        remaining = self._remaining(deadline)
        if self.persistent_worker:
            worker = _get_worker(self.java, self.classpath)
            worker.execute(entrypoint, request_path, result_path, remaining)
        else:
            process = subprocess.run(
                [self.java, "-cp", self.classpath, f"id.tradegraph.{entrypoint}", str(request_path), str(result_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=remaining, check=False,
            )
            if process.returncode != 0:
                raise RuntimeError(f"Semantic adapter exited with status {process.returncode}. "
                                   f"{process.stderr.decode('utf-8', errors='replace')[-1500:]}")
        return json.loads(result_path.read_text(encoding="utf-8"))

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining
