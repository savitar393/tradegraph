"""Evaluate what-if branches against their registered baseline run."""

from dataclasses import fields

from tradegraph.assessment.service import AssessmentService
from tradegraph.cases.facts import parse_persisted_facts, rebase_triple, revision_ref
from tradegraph.contracts import AssessmentRequest, FactChanges, FactPatch, FindingChange, ScenarioDiff


class ScenarioService:
    def __init__(self, assessment: AssessmentService):
        self.assessment = assessment

    def evaluate_scenario(self, base_run_id: str, patch: FactPatch) -> ScenarioDiff:
        report, case = self.assessment.provenance.load_run(base_run_id)
        if patch.context is not None and (
            patch.context.kb_sha256, patch.context.engine_profile_id,
        ) != (case.context.kb_sha256, case.context.engine_profile_id):
            raise ValueError("Scenario revisions require the baseline KB and engine profile.")

        branch = self.assessment.repository.revise_case(case.case_id, case.revision, patch, branch=True)
        scenario = self.assessment.assess(AssessmentRequest(
            report.kb_sha256, branch.case_id, branch.revision, tuple(report.query_results),
        ))
        original = revision_ref(case.case_id, case.revision)
        fork = revision_ref(branch.case_id, branch.revision)
        before = set(parse_persisted_facts(case.facts_turtle, case.case_id))
        after = {
            rebase_triple(triple, fork, original)
            for triple in parse_persisted_facts(branch.facts_turtle, branch.case_id)
        }
        changes = () if scenario.execution_status != "CONVERGED" else _findings(report, scenario, fork, original)
        context_changes = tuple(
            (field.name, before_value, after_value)
            for field in fields(case.context)
            if (before_value := getattr(case.context, field.name))
            != (after_value := getattr(branch.context, field.name))
        )
        return ScenarioDiff(
            base_run_id, scenario.run_id, FactChanges(tuple(sorted(after - before)), tuple(sorted(before - after))),
            changes, scenario, context_changes,
        )


def _findings(before, after, fork, original):
    def inferences(report, source, target):
        return {
            " ".join(term.n3() for term in rebase_triple(finding.triple, source, target)): finding
            for finding in report.findings
        }

    def evidence(report):
        return {
            f"{finding.definition_id}|{finding.focus_iri or ''}": finding
            for finding in (report.validation_report.evidence_findings if report.validation_report else ())
        }

    def compare(kind, left, right, signature, status):
        for key in sorted(left.keys() | right.keys()):
            old, new = left.get(key), right.get(key)
            if (signature(old) if old else None) != (signature(new) if new else None):
                yield FindingChange(
                    kind, key, status(old) if old else None, status(new) if new else None,
                    old.finding_id if old else None, new.finding_id if new else None,
                )

    return (
        *compare("INFERENCE", inferences(before, original, original), inferences(after, fork, original),
                 lambda finding: finding.kind, lambda finding: finding.kind),
        *compare("POLICY", {f.policy_id: f for f in before.domain_findings},
                 {f.policy_id: f for f in after.domain_findings}, lambda finding: finding, lambda finding: finding.status),
        *compare("VALIDATION", evidence(before), evidence(after),
                 lambda finding: finding, lambda finding: finding.status),
    )
