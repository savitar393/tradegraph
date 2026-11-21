"""Check projected OWL and SWRL using the pinned Java dependencies."""

from base64 import b64decode
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

from tradegraph.contracts import CompiledKB, ValidationReport


_DEP_RE = re.compile(r"([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+):jar:([a-zA-Z0-9_.-]+):(compile|runtime)")


def _pom_path() -> Path:
    cwd_pom = Path.cwd() / "semantic" / "pom.xml"
    if cwd_pom.is_file():
        return cwd_pom
    return Path(__file__).resolve().parents[3] / "semantic" / "pom.xml"


def _resolve_m2_classpath(pom: Path, *, output: Path | None = None) -> str:
    semantic_dir = pom.resolve().parent
    repo_root = semantic_dir.parent
    classes_dir = semantic_dir / "target" / "classes"
    tree_file = repo_root / "docs" / "engine-dependency-tree.txt"
    m2_root = Path(os.environ.get("MAVEN_REPO_LOCAL") or (Path.home() / ".m2" / "repository"))
    if not tree_file.is_file() or not m2_root.is_dir():
        raise OSError("Maven and semantic/pom.xml are required for OWL validation.")

    jars: list[str] = []
    for line in tree_file.read_text(encoding="utf-8").splitlines():
        match = _DEP_RE.search(line)
        if not match:
            continue
        group, artifact, version, _ = match.groups()
        jar = m2_root / Path(*group.split(".")) / artifact / version / f"{artifact}-{version}.jar"
        if not jar.is_file():
            raise OSError(f"Missing pinned Maven artifact {jar}; put Maven on PATH to build.")
        jars.append(str(jar))

    src_root = semantic_dir / "src" / "main" / "java"
    java_sources = sorted(src_root.rglob("*.java")) if src_root.is_dir() else []
    needs_compile = not classes_dir.is_dir()
    if not needs_compile and java_sources:
        for src in java_sources:
            rel = src.relative_to(src_root).with_suffix(".class")
            cls = classes_dir / rel
            if not cls.is_file() or src.stat().st_mtime > cls.stat().st_mtime:
                needs_compile = True
                break
    if needs_compile:
        javac = shutil.which("javac")
        if not javac or not java_sources:
            raise OSError("Compiled semantic/target/classes or javac/Maven is required.")
        classes_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [javac, "-d", str(classes_dir), "-cp", os.pathsep.join(jars), *(str(p) for p in java_sources)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=120, check=False,
        )
        if proc.returncode != 0:
            raise OSError(f"Cannot compile semantic Java sources with javac.\n{proc.stdout}")

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(os.pathsep.join(jars), encoding="utf-8")
    return os.pathsep.join((str(classes_dir), *jars))


def _resolve_bundled_lib_classpath(pom: Path) -> str | None:
    """Resolve pre-bundled semantic/target/classes + semantic/target/lib/*.jar (used in container images)."""
    semantic_dir = pom.resolve().parent
    classes_dir = semantic_dir / "target" / "classes"
    lib_dir = semantic_dir / "target" / "lib"
    if not classes_dir.is_dir() or not lib_dir.is_dir():
        return None
    jars = sorted(str(jar) for jar in lib_dir.glob("*.jar"))
    if not jars:
        return None
    return os.pathsep.join((str(classes_dir), *jars))


def resolve_runtime_classpath(pom: Path | None = None, *, output: Path | None = None, maven: str | None = None) -> str:
    """Resolve the pinned semantic runtime classpath via env override, bundled container libs, Maven, or .m2 cache."""
    env_cp = os.environ.get("TRADEGRAPH_CLASSPATH", "").strip()
    if env_cp and maven is None:
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(env_cp, encoding="utf-8")
        return env_cp

    pom_file = (pom or _pom_path()).resolve()
    if maven is None:
        bundled = _resolve_bundled_lib_classpath(pom_file)
        if bundled is not None:
            if output is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(bundled, encoding="utf-8")
            return bundled

    if not pom_file.is_file():
        raise OSError("Maven and semantic/pom.xml are required for OWL validation.")

    executable = maven or shutil.which("mvn")
    if executable is not None and output is not None:
        out_path = output.resolve()
        process = subprocess.run(
            [
                executable, "-B", "-ntp", "-f", str(pom_file), "compile",
                "org.apache.maven.plugins:maven-dependency-plugin:3.8.1:build-classpath",
                "-DincludeScope=runtime", f"-Dmdep.outputFile={out_path}",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", timeout=180, check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(f"Cannot build the semantic adapter.\n{process.stdout}")
        dependencies = out_path.read_text(encoding="utf-8").strip()
        return os.pathsep.join((str(pom_file.parent / "target" / "classes"), dependencies))

    if maven is not None:
        raise RuntimeError("Put Maven on PATH before building the semantic adapter.")
    return _resolve_m2_classpath(pom_file, output=output)


def _run_owlapi(core: Path, rules: Path | None = None, saved: Path | None = None) -> list[str]:
    pom = _pom_path()
    maven = shutil.which("mvn")
    env_cp = os.environ.get("TRADEGRAPH_CLASSPATH", "").strip()
    bundled_cp = _resolve_bundled_lib_classpath(pom) if pom.is_file() else None
    if env_cp or bundled_cp or not maven:
        java = shutil.which("java")
        if not java:
            raise OSError("Java 21 is required for OWL validation.")
        classpath = env_cp or bundled_cp or _resolve_m2_classpath(pom)
        cmd = [
            java, "-cp", classpath, "id.tradegraph.KbOwlValidator",
            str(core.resolve()),
            str(rules.resolve()) if rules is not None else "",
            str(saved.resolve()) if saved is not None else "",
        ]
    else:
        if not pom.is_file():
            raise OSError("Maven and semantic/pom.xml are required for OWL validation.")
        cmd = [
            maven, "-q", "-B", "-ntp", "-f", str(pom), "compile", "exec:java",
            f"-Dtradegraph.owl.input={core.resolve()}",
            f"-Dtradegraph.swrl.input={rules.resolve() if rules is not None else ''}",
            f"-Dtradegraph.roundtrip.output={saved.resolve() if saved is not None else ''}",
        ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120, check=False,
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
