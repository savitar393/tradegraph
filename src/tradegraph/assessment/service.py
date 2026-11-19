"""Assess a specific stored revision using the registered KB snapshot."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from tradegraph.assessment.policies import evaluate_policies
from tradegraph.cases.repository import CaseRepository
from tradegraph.contracts import AssessmentReport, AssessmentRequest, Finding, ReasoningJob, ReasoningLimits
from tradegraph.provenance.service import ProvenanceService
from tradegraph.reasoning.client import ReasoningClient
from tradegraph.validation.service import validate_evidence


class AssessmentService:
    def __init__(self, repository: CaseRepository, client: ReasoningClient, *, limits=ReasoningLimits()):
        self.repository = repository
        self.client = client
        self.limits = limits
        self.provenance = ProvenanceService(client)

    def assess(self, request: AssessmentRequest, *, timeout_ms: int | None = None) -> AssessmentReport:
        run_id = str(uuid4())
        run_at = datetime.now(UTC)
        snapshot = self.repository.load_case(request.case_id, request.revision)
        limits = self.limits if timeout_ms is None else replace(
            self.limits, timeout_ms=min(self.limits.timeout_ms, timeout_ms),
        )
        result = self.client.reason(ReasoningJob(request.kb_sha256, snapshot, request.query_ids, limits))
        validation_report = None
        coverage_status = None
        domain_findings = ()
        kb = self.client.kb
        coverage = kb.snapshot.coverage if request.kb_sha256 == kb.snapshot.sha256 else ()
        if result.execution_status == "CONVERGED" and result.consistency == "CONSISTENT":
            if kb.evidence_catalog:
                validation_report = validate_evidence(
                    kb, snapshot, snapshot.facts_turtle, inferred_facts=result.inferred_facts,
                )
            if kb.coverage_query is not None:
                coverage, domain_findings, coverage_status = evaluate_policies(kb, snapshot, result, validation_report)
        elif kb.coverage_query is not None:
            coverage = ()
        if validation_report is not None:
            validation_report = replace(
                validation_report, report_id=f"{run_id}:validation",
                evidence_findings=tuple(
                    replace(finding, finding_id=f"{run_id}:evidence:{index}")
                    for index, finding in enumerate(validation_report.evidence_findings)
                ),
            )
        report = AssessmentReport(
            run_id=run_id,
            context=result.context,
            execution_status=result.execution_status,
            consistency=result.consistency,
            coverage=coverage,
            findings=tuple(
                Finding(fact, support, f"{run_id}:inference:{index}")
                for index, (fact, support) in enumerate(zip(result.inferred_facts, result.supports, strict=True))
            ),
            query_results=result.query_results,
            completeness_claim=result.completeness_claim,
            diagnostics=result.diagnostics,
            validation_report=validation_report,
            domain_findings=tuple(
                replace(finding, finding_id=f"{run_id}:policy:{index}")
                for index, finding in enumerate(domain_findings)
            ),
            coverage_status=coverage_status,
            run_at=run_at,
        )
        if result.execution_status == "CONVERGED":
            self.provenance.register(report, snapshot)
        return report
