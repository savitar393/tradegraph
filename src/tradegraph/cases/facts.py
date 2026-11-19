"""Read and write the RDF asserted inside one case revision."""

import re
from urllib.parse import urlsplit

from rdflib import BNode, Graph, Literal, Namespace, OWL, RDF, RDFS, SH, URIRef, XSD
from rdflib.exceptions import ParserError

from tradegraph.contracts import FactPatch, RDFTriple
from tradegraph.kb.rdf import parse_turtle


TG = Namespace("urn:tradegraph:kb#")
CASE_DATA = Namespace("urn:tradegraph:case-data#")
SWRL = "http://www.w3.org/2003/11/swrl#"
CASE_ASSERTION_TYPES = {
    OWL.NamedIndividual, OWL.AllDifferent, OWL.NegativePropertyAssertion, OWL.Thing, OWL.Nothing,
}
CASE_OWL_PROPERTIES = {
    OWL.sameAs, OWL.differentFrom, OWL.distinctMembers, OWL.members, OWL.sourceIndividual,
    OWL.assertionProperty, OWL.targetIndividual, OWL.targetValue,
}
CASE_CONTROLLED_PROPERTIES = {
    TG.evidenceScopeClosed, TG.usesKnowledgeRevision, TG.engineProfileId, TG.assessesRevision,
}
RDFS_SCHEMA_PROPERTIES = {RDFS.subClassOf, RDFS.subPropertyOf, RDFS.domain, RDFS.range}


def require_iri(value: str) -> URIRef:
    if (
        not isinstance(value, str) or not urlsplit(value).scheme
        or any(char.isspace() or char in '<>"{}|\\^`' for char in value)
    ):
        raise ValueError(f"Expected an absolute RDF IRI, got {value!r}")
    return URIRef(value)


def _case_base(case_id: str) -> str:
    return f"https://tradegraph.invalid/cases/{case_id}/"


def parse_input_facts(content: str, case_id: str) -> Graph:
    try:
        graph = parse_turtle(content.encode("utf-8"), _case_base(case_id))
    except (SyntaxError, ValueError, ParserError) as error:
        raise ValueError(f"Invalid Turtle for case facts\n{error}") from error
    for triple in graph:
        _validate_assertion(triple)
    return graph


def parse_persisted_facts(content: str, case_id: str) -> Graph:
    return parse_turtle(content.encode("utf-8"), _case_base(case_id), preserve_bnode_labels=True)


def serialize_assertions(graph: Graph) -> str:
    # N-Triples is a Turtle subset. Retain each serialized blank node label and literal.
    lines = graph.serialize(format="nt", encoding="utf-8").decode("utf-8").split("\n")
    return "".join(f"{line}\n" for line in sorted(lines) if line)


def apply_fact_patch(graph: Graph, patch: FactPatch) -> None:
    removals = tuple(canonical_assertion(triple) for triple in patch.remove_triples)
    additions = tuple(canonical_assertion(triple) for triple in patch.add_triples)
    for triple in removals:
        if triple not in graph:
            raise ValueError(f"Cannot retract a triple absent from the base revision: {triple!r}")
    for triple in removals:
        graph.remove(triple)
    for triple in additions:
        graph.add(triple)


def revision_ref(case_id: str, revision: int) -> URIRef:
    return URIRef(f"urn:tradegraph:case:{case_id}:revision:{revision}")


def rebase_triple(triple: RDFTriple, source: URIRef, target: URIRef) -> RDFTriple:
    """Move identifiers in the owned revision namespace while preserving RDF term types."""
    def rebase(term):
        if isinstance(term, URIRef) and (term == source or term.startswith(f"{source}:")):
            return URIRef(f"{target}{term[len(source):]}")
        return term

    subject, predicate, value = triple
    return rebase(subject), predicate, rebase(value)


def canonical_assertion(triple: RDFTriple) -> RDFTriple:
    _validate_assertion(triple)
    subject, predicate, value = triple
    if isinstance(value, Literal) and value.language is None and value.datatype is None:
        value = Literal(str(value), datatype=XSD.string, normalize=False)
    return subject, predicate, value


def _validate_assertion(triple: RDFTriple) -> None:
    subject, predicate, value = triple
    if not (
        isinstance(subject, (URIRef, BNode))
        and isinstance(predicate, URIRef)
        and isinstance(value, (URIRef, BNode, Literal))
    ):
        raise ValueError("Case assertions require RDF subjects, IRI predicates, and RDF terms")
    for term in triple:
        if isinstance(term, URIRef):
            require_iri(term)
        if isinstance(term, BNode) and not re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?", term):
            raise ValueError("Use a generated or persisted blank-node label for case assertions")
        if isinstance(term, Literal) and term.datatype is not None:
            require_iri(term.datatype)
    if (
        predicate in RDFS_SCHEMA_PROPERTIES or predicate in CASE_CONTROLLED_PROPERTIES
        or str(predicate).startswith((SWRL, str(SH), str(CASE_DATA)))
        or (str(predicate).startswith(str(OWL)) and predicate not in CASE_OWL_PROPERTIES)
        or (
            predicate == RDF.type and isinstance(value, URIRef)
            and (
                str(value).startswith((SWRL, str(SH)))
                or (str(value).startswith(str(OWL)) and value not in CASE_ASSERTION_TYPES)
                or value in {RDFS.Class, RDFS.Datatype, RDF.Property}
            )
        )
    ):
        raise ValueError("Case facts accept assertions only. Store schema and rules in the canonical KB.")
