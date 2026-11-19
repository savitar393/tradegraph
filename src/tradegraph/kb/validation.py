"""Check projected OWL and SWRL using the pinned Java dependencies."""

from base64 import b64decode
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from tradegraph.contracts import CompiledKB, ValidationReport


def _pom_path() -> Path:
    cwd_pom = Path.cwd() / "semantic" / "pom.xml"
    if cwd_pom.is_file():
        return cwd_pom
    return Path(__file__).resolve().parents[3] / "semantic" / "pom.xml"


def _run_owlapi(core: Path, rules: Path | None = None, saved: Path | None = None) -> list[str]:
    maven = shutil.which("mvn")
    pom = _pom_path()
    if not maven or not pom.is_file():
        raise OSError("Maven and semantic/pom.xml are required for OWL validation.")
    try:
        result = subprocess.run(
            [
                maven, "-q", "-B", "-ntp", "-f", str(pom), "compile", "exec:java",
                f"-Dtradegraph.owl.input={core.resolve()}",
                f"-Dtradegraph.swrl.input={rules.resolve() if rules is not None else ''}",
                f"-Dtradegraph.roundtrip.output={saved.resolve() if saved is not None else ''}",
            ],
            capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError("OWLAPI tool timed out.") from error
    if result.returncode:
        diagnostics = (result.stdout + "\n" + result.stderr).strip()
        raise OSError(diagnostics or "OWLAPI tool did not complete.")
    return result.stdout.splitlines()


def replay_owlapi_save(original: Path, saved: Path) -> None:
    """Replay a Turtle save using the same OWLAPI version bundled by Protégé 5.6.9."""
    if saved.exists():
        raise FileExistsError(saved)
    _run_owlapi(original, saved=saved)
    if not saved.is_file():
        raise OSError("OWLAPI did not produce the expected saved Turtle file.")


def validate_owl(compiled: CompiledKB) -> ValidationReport:
    try:
        with TemporaryDirectory(prefix="tradegraph-owl-") as workspace:
            core = Path(workspace) / "core.ttl"
            rules = Path(workspace) / "rules.ttl"
            compiled.dataset.graph(compiled.owl_projection_ref).serialize(core, format="nt", encoding="utf-8")
            if compiled.rule_atom_counts:
                compiled.dataset.graph(compiled.rule_projection_ref).serialize(rules, format="nt", encoding="utf-8")
            lines = _run_owlapi(core, rules if compiled.rule_atom_counts else None)
    except OSError as error:
        return ValidationReport(False, (str(error),))

    if "TRADEGRAPH_OWL_VALID" not in lines:
        return ValidationReport(False, ("OWL validator produced no result.",))

    counts = {}
    for line in lines:
        if line.startswith("TRADEGRAPH_SWRL_RULE\t"):
            _, encoded_id, body, head = line.split("\t")
            counts[b64decode(encoded_id).decode()] = (int(body), int(head))
    if counts != compiled.rule_atom_counts:
        return ValidationReport(False, ("OWLAPI changed the rule IDs or SWRL body/head atom counts.",))
    return ValidationReport(True, ())
