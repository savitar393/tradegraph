from hashlib import sha256
from pathlib import Path

import pytest
from rdflib import BNode, Literal, Namespace, RDF, OWL, SH

from tradegraph.kb.loader import KnowledgeBaseError, compile_kb
from tradegraph.kb.validation import validate_owl


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = Path(__file__).with_name("fixtures") / "mixed.ttl"
PROFILE = "tradegraph-jdk21-owl2dl-swrl-comparisons-v1"
MIXED = Namespace("urn:tradegraph:test:mixed#")
SWRL = Namespace("http://www.w3.org/2003/11/swrl#")
CATALOG = Namespace("urn:tradegraph:catalog#")


def test_compiles_snapshot_and_keeps_shared_list_in_owl_and_shacl():
    compiled = compile_kb(FIXTURE, PROFILE)
    assert compiled.snapshot.sha256 == sha256(FIXTURE.read_bytes()).hexdigest()
    assert compiled.snapshot.kb_id == "urn:tradegraph:test:mixed"
    assert compiled.snapshot.version == "1"
    assert compiled.source_bytes == FIXTURE.read_bytes()

    owl = compiled.dataset.graph(compiled.owl_projection_ref)
    rules = compiled.dataset.graph(compiled.rule_projection_ref)
    shapes = compiled.dataset.graph(compiled.shapes_graph_ref)
    definitions = compiled.dataset.graph(compiled.definitions_graph_ref)
    reference = compiled.dataset.graph(compiled.reference_graph_ref)

    assert len(list(owl.subjects(RDF.type, OWL.Axiom))) == 1
    shared = owl.value(MIXED.Selection, OWL.oneOf)
    assert isinstance(shared, BNode)
    assert list(owl.items(shared)) == [MIXED.alice, MIXED.bob]
    shape_property = shapes.value(MIXED.selectionShape, SH.property)
    assert shapes.value(shape_property, SH["in"]) == shared
    assert list(shapes.items(shared)) == [MIXED.alice, MIXED.bob]

    rule = next(rules.subjects(RDF.type, SWRL.Imp))
    assert isinstance(rule, BNode)
    assert len(list(rules.items(rules.value(rule, SWRL.body)))) == 1
    assert len(list(rules.items(rules.value(rule, SWRL.head)))) == 1
    assert (MIXED.subject, RDF.type, SWRL.Variable) in rules
    assert (MIXED.Person, RDF.type, OWL.Class) in rules
    assert (MIXED.alice, MIXED.relatedTo, MIXED.bob) not in rules
    assert list(owl.subjects(RDF.type, SWRL.Imp)) == []
    assert (MIXED.selectionShape, RDF.type, SH.NodeShape) not in owl
    assert (MIXED.relatedField, RDF.type, CATALOG.FieldDefinition) not in owl
    assert (MIXED.relatedField, RDF.type, CATALOG.FieldDefinition) in definitions
    assert (MIXED.selectionShape, RDF.type, SH.NodeShape) in reference


def test_catalogs_are_read_from_the_same_document():
    compiled = compile_kb(FIXTURE, PROFILE)
    assert compiled.field_catalog["relatedTo"].property_iri == str(MIXED.relatedTo)
    query = compiled.query_catalog["CQ01"]
    assert query.parameters == ("subject",)
    reference = compiled.dataset.graph(compiled.reference_graph_ref)
    assert [tuple(row) for row in reference.query(query.sparql, initBindings={"subject": MIXED.alice})] == [
        (MIXED.bob,)
    ]
    action = compiled.action_catalog["record-related"]
    definitions = compiled.dataset.graph(compiled.definitions_graph_ref)
    assert definitions.value(action.preconditions[0], CATALOG.mentions) == MIXED.Person
    assert definitions.value(action.effects[0], CATALOG.mentions) == MIXED.relatedTo


def test_keeps_owl_declarations_on_a_class_used_as_a_shacl_shape(tmp_path):
    kb = tmp_path / "dual-role.ttl"
    kb.write_bytes(FIXTURE.read_bytes() + b"\n:Person a sh:NodeShape ; sh:targetClass :Person .\n")
    compiled = compile_kb(kb, PROFILE)
    owl = compiled.dataset.graph(compiled.owl_projection_ref)
    shapes = compiled.dataset.graph(compiled.shapes_graph_ref)

    assert (MIXED.Person, RDF.type, OWL.Class) in owl
    assert (MIXED.Person, RDF.type, SH.NodeShape) not in owl
    assert (MIXED.Person, SH.targetClass, MIXED.Person) in shapes


def test_canonical_kb_has_cq01_fields_actions_and_partial_coverage():
    compiled = compile_kb(ROOT / "knowledge" / "tradegraph.ttl", PROFILE)
    assert compiled.query_catalog["CQ01"].parameters == ("product",)
    assert "transactionAt" in compiled.field_catalog
    assert "prepare-reexport-notice" in compiled.action_catalog
    assert {entry.status for entry in compiled.snapshot.coverage} == {"PARTIAL"}
    assert len(compiled.snapshot.coverage) >= 7


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ('<urn:tradegraph:test:mixed> owl:imports <https://example.invalid/ontology> .', "owl:imports"),
        (
            ':another a catalog:QueryDefinition ; catalog:id "CQ01" ; '
            'rdfs:label "Duplicate" ; catalog:sparql "SELECT * WHERE {}" .',
            "Duplicate query ID",
        ),
        ('[] a swrl:Imp ; tg:ruleId "empty" ; swrl:body rdf:nil ; swrl:head rdf:nil .', "Empty SWRL"),
    ],
)
def test_rejects_invalid_kb_metadata(tmp_path, addition, message):
    kb = tmp_path / "invalid.ttl"
    kb.write_bytes(FIXTURE.read_bytes() + b"\n" + addition.encode())
    with pytest.raises(KnowledgeBaseError, match=message):
        compile_kb(kb, PROFILE)


def test_rejects_a_different_engine_profile():
    with pytest.raises(KnowledgeBaseError, match="Engine profile mismatch"):
        compile_kb(FIXTURE, "some-other-profile")


def test_preserves_rdf_literal_lexical_form(tmp_path):
    kb = tmp_path / "typed.ttl"
    kb.write_bytes(FIXTURE.read_bytes() + b'\n:rank a owl:DatatypeProperty .\n:alice :rank +01 .\n')
    compiled = compile_kb(kb, PROFILE)
    owl = compiled.dataset.graph(compiled.owl_projection_ref)
    assert str(owl.value(MIXED.alice, MIXED.rank)) == "+01"
    assert (MIXED.alice, MIXED.rank, Literal(1)) not in owl


def test_engine_validates_owl_and_reads_the_projected_swrl_body_and_head():
    report = validate_owl(compile_kb(FIXTURE, PROFILE))
    assert report.conforms, report.findings


def test_engine_rejects_owl_dl_violation_after_projection(tmp_path):
    kb = tmp_path / "non-dl.ttl"
    kb.write_bytes(
        FIXTURE.read_bytes()
        + b"""
:derived a owl:ObjectProperty ; owl:propertyChainAxiom (:relatedTo :relatedTo) .
:Person rdfs:subClassOf [ a owl:Restriction ; owl:onProperty :derived ;
    owl:minQualifiedCardinality "2"^^xsd:nonNegativeInteger ; owl:onClass :Person ] .
"""
    )
    report = validate_owl(compile_kb(kb, PROFILE))
    assert not report.conforms
    assert any("OWL 2 DL" in finding for finding in report.findings)
