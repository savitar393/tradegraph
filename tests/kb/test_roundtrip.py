from pathlib import Path

import pytest
from rdflib import Graph, Namespace, RDF, SH

from tradegraph.kb.loader import verify_roundtrip
from tradegraph.kb.roundtrip import restore_roundtrip
from tradegraph.kb.validation import replay_owlapi_save


FIXTURE = Path(__file__).with_name("fixtures") / "mixed.ttl"
MIXED = Namespace("http://www.semanticweb.org/tradegraph/test/mixed#")
SWRL = Namespace("http://www.w3.org/2003/11/swrl#")
CATALOG = Namespace("http://www.semanticweb.org/tradegraph/catalog#")


def test_roundtrip_accepts_different_blank_node_ids_and_triple_order(tmp_path):
    after = tmp_path / "saved.ttl"
    Graph().parse(FIXTURE, format="turtle").serialize(after, format="nt")
    assert after.read_bytes() != FIXTURE.read_bytes()
    report = verify_roundtrip(FIXTURE, after)
    assert report.conforms is True
    assert report.findings == ()


def test_roundtrip_reports_dropped_shapes_actions_and_rules(tmp_path):
    after = tmp_path / "incomplete.ttl"
    graph = Graph().parse(FIXTURE, format="turtle")
    graph.remove((MIXED.selectionShape, RDF.type, SH.NodeShape))
    graph.remove((MIXED.recordAction, RDF.type, CATALOG.ActionDefinition))
    graph.remove((None, RDF.type, SWRL.Imp))
    graph.serialize(after, format="turtle")
    report = verify_roundtrip(FIXTURE, after)
    assert report.conforms is False
    assert report.findings


def test_roundtrip_rejects_a_changed_literal_lexical_form(tmp_path):
    prefix = '@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n'
    before = tmp_path / "before.ttl"
    after = tmp_path / "after.ttl"
    before.write_text(prefix + '<http://example.org/a> <http://example.org/p> "01"^^xsd:integer .', encoding="utf-8")
    after.write_text(prefix + '<http://example.org/a> <http://example.org/p> "1"^^xsd:integer .', encoding="utf-8")
    assert verify_roundtrip(before, after).conforms is False


def test_restores_extension_rdf_after_a_verified_owlapi_no_edit_save(tmp_path):
    saved = tmp_path / "saved.ttl"
    restored = tmp_path / "restored.ttl"
    replay_owlapi_save(FIXTURE, saved)
    assert not verify_roundtrip(FIXTURE, saved).conforms

    report = restore_roundtrip(FIXTURE, saved, restored)

    assert report.conforms, report.findings
    assert verify_roundtrip(FIXTURE, restored).conforms
    assert restored.read_bytes() == FIXTURE.read_bytes()


def test_refuses_to_discard_an_observable_owl_edit(tmp_path):
    saved = tmp_path / "edited.ttl"
    restored = tmp_path / "restored.ttl"
    replay_owlapi_save(FIXTURE, saved)
    with saved.open("ab") as output:
        output.write(b'\n<http://www.semanticweb.org/tradegraph/test/mixed#bob> a <http://www.semanticweb.org/tradegraph/test/mixed#Checked> .\n')

    report = restore_roundtrip(FIXTURE, saved, restored)

    assert not report.conforms
    assert not restored.exists()


def test_restore_writes_only_to_a_new_path(tmp_path):
    destination = tmp_path / "occupied.ttl"
    destination.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        restore_roundtrip(FIXTURE, FIXTURE, destination)
    assert destination.read_text(encoding="utf-8") == "keep"
