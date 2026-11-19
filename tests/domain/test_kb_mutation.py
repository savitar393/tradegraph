from hashlib import sha256

from tradegraph.kb.loader import ENGINE_PROFILE_ID, compile_kb

from .conftest import CASES, KB, ROOT


def test_lab_threshold_change_reassesses_only_its_policy_context(tmp_path, run_case, compiled):
    source_files = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "semantic/src/main").rglob("*.java"))
    before_code = tuple(sha256(path.read_bytes()).hexdigest() for path in source_files)
    text = KB.read_text(encoding="utf-8")
    assert text.count(":minimumRvcPercent 40.00") == 1
    lab_file = tmp_path / "lab.ttl"
    lab_file.write_text(
        "# LAB scenario only. Synthetic threshold applied to a temporary copy.\n"
        + text.replace(":minimumRvcPercent 40.00", ":minimumRvcPercent 45.00"),
        encoding="utf-8",
    )
    lab = compile_kb(lab_file, ENGINE_PROFILE_ID)
    assert lab.snapshot.sha256 != compiled.snapshot.sha256

    affected = next(case for case in CASES if case["id"] == "pump-rvc-boundary")
    unaffected = next(case for case in CASES if case["id"] == "coffee-destination-permit")
    before = run_case(affected)
    after = run_case(affected, kb=lab)
    before_status = {finding.policy_id: finding.status for finding in before.domain_findings}
    after_status = {finding.policy_id: finding.status for finding in after.domain_findings}
    assert before_status["atiga-rvc"] == "SATISFIED"
    assert after_status["atiga-rvc"] == "NOT_SATISFIED"
    assert before_status | {"atiga-rvc": "NOT_SATISFIED"} == after_status
    assert run_case(unaffected).domain_findings == run_case(unaffected, kb=lab).domain_findings
    assert tuple(sha256(path.read_bytes()).hexdigest() for path in source_files) == before_code
