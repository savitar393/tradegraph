"""Run the pinned Java engine and query the resulting named facts locally."""

from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic

from rdflib import Graph, Literal, OWL, URIRef

from tradegraph.contracts import (
    AxiomWitness, CaseSnapshot, CompiledKB, FactSupport, Finding, InferenceResult, ReasoningContext,
    ReasoningJob, RuleWitness,
)
from tradegraph.graph.store import CaseGraphs, project_case_graphs
from tradegraph.kb.loader import ENGINE_PROFILE_ID, KnowledgeBaseError, select_rules
from tradegraph.kb.queries import UnsupportedQuery, context_bindings, prepare_local_query


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


class ReasoningClient:
    def __init__(self, kb: CompiledKB, classpath: str, *, java: str = "java"):
        self.kb = kb
        self.classpath = classpath
        self.java = java

    @staticmethod
    def build_classpath(pom: Path, output: Path, *, maven: str | None = None) -> str:
        """Compile the adapter and resolve the pinned Maven runtime classpath."""
        executable = maven or shutil.which("mvn")
        if executable is None:
            raise RuntimeError("Put Maven on PATH before building the semantic adapter.")
        pom = pom.resolve()
        output = output.resolve()
        process = subprocess.run(
            [
                executable, "-B", "-ntp", "-f", str(pom), "compile",
                "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:build-classpath",
                "-DincludeScope=runtime", f"-Dmdep.outputFile={output}",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", timeout=180, check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"Cannot build the semantic adapter.\n{process.stdout}")
        dependencies = output.read_text(encoding="utf-8").strip()
        return os.pathsep.join((str(pom.parent / "target" / "classes"), dependencies))

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
                definitions = self.kb.dataset.graph(self.kb.definitions_graph_ref)
                fixed = {node for graph in (schema, definitions) for node in graph.subjects() if isinstance(node, URIRef)}
                for subject, predicate, value in graphs.facts:
                    if (subject in fixed and (subject, predicate, value) not in schema) or (
                        predicate == OWL.sameAs and (subject in fixed or value in fixed)
                    ):
                        raise KnowledgeBaseError(f"Case assertion changes a fixed KB resource at {subject}.")
            core = schema + graphs.facts + graphs.context
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
            if queries:
                for fact in inferred:
                    core.add(fact)
            query_results = {}
            for query_id, (query, bindings) in queries.items():
                result = core.query(query, initBindings=bindings)
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
            core = self.kb.dataset.graph(self.kb.owl_projection_ref) + graphs.facts + graphs.context
            for prior in prior_findings:
                if prior.support.context != context:
                    raise ValueError("A premise belongs to another reasoning snapshot.")
                stage = prior.support.iteration
                if (prior.kind == "RULE_DERIVED" and stage < support.iteration) or (
                    finding.kind == "RULE_DERIVED" and prior.kind == "OWL_ENTAILED" and stage <= support.iteration
                ):
                    core.add(prior.triple)
            rules, counts = select_rules(self.kb, case.context) if finding.kind == "RULE_DERIVED" else (Graph(), {})
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
        process = subprocess.run(
            [self.java, "-cp", self.classpath, f"id.tradegraph.{entrypoint}", str(request_path), str(result_path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self._remaining(deadline), check=False,
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
