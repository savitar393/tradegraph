"""Recover RDF lost by a verified no-edit OWLAPI Turtle save."""

from pathlib import Path
from tempfile import TemporaryDirectory

from rdflib.compare import isomorphic

from tradegraph.contracts import ValidationReport
from tradegraph.kb.loader import ENGINE_PROFILE_ID, _parse, compile_kb
from tradegraph.kb.validation import replay_owlapi_save, validate_owl


def restore_roundtrip(
    before: Path, saved: Path, destination: Path, profile_id: str = ENGINE_PROFILE_ID
) -> ValidationReport:
    """Restore the complete graph when the save matches a no-edit replay."""
    if destination.exists():
        raise FileExistsError(destination)
    compiled = compile_kb(before, profile_id)
    original = compiled.dataset.graph(compiled.reference_graph_ref)
    saved_graph = _parse(saved, saved.read_bytes())

    if not isomorphic(original, saved_graph):
        with TemporaryDirectory(prefix="tradegraph-roundtrip-") as workdir:
            baseline = Path(workdir) / "original.ttl"
            replay = Path(workdir) / "replay.ttl"
            baseline.write_bytes(_portable_turtle(compiled.source_bytes, original, baseline))
            replay_owlapi_save(baseline, replay)
            if not isomorphic(_parse(replay, replay.read_bytes()), saved_graph):
                return ValidationReport(False, ("Saved RDF differs from the no-edit OWLAPI replay.",))

    validation = validate_owl(compiled)
    if not validation.conforms:
        return validation
    restored = _portable_turtle(compiled.source_bytes, original, destination)
    with destination.open("xb") as output:
        output.write(restored)
    return ValidationReport(True, ())


def _portable_turtle(content, graph, location):
    # A raw copy with relative IRIs might change meaning at a new destination.
    if isomorphic(_parse(location, content), graph):
        return content
    return graph.serialize(format="nt", encoding="utf-8")
