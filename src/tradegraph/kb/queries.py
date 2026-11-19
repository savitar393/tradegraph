"""Prepare declarative queries against local RDF graphs."""

from rdflib import Graph, Literal, URIRef
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.parserutils import CompValue

from tradegraph.cases.facts import TG


class UnsupportedQuery(ValueError):
    pass


def _local_pattern(node):
    if isinstance(node, CompValue):
        if node.name == "ServiceGraphPattern" or ("datasetClause" in node and node["datasetClause"]):
            return False
        return all(map(_local_pattern, node.values()))
    if isinstance(node, (list, tuple)):
        return all(map(_local_pattern, node))
    return True


def prepare_local_query(sparql: str, *kinds: str):
    query = prepareQuery(sparql)
    if query.algebra.name not in kinds or not _local_pattern(query.algebra):
        raise UnsupportedQuery(f"Expected a local {' or '.join(kinds)}.")
    return query


def context_bindings(graph: Graph) -> dict[str, URIRef | Literal]:
    predicates = {
        "product": TG.assessesProduct,
        "batch": TG.assessesBatch,
        "jurisdiction": TG.appliesInJurisdiction,
        "procedure": TG.selectedProcedure,
        "agreement": TG.selectedAgreement,
        "revision": TG.assessesRevision,
        "transactionAt": TG.transactionAt,
        "evaluationAt": TG.evaluationAt,
        "scope": TG.usesEvidenceSet,
    }
    bindings = {"context": graph.identifier}
    bindings.update({
        name: value for name, predicate in predicates.items()
        if (value := graph.value(graph.identifier, predicate)) is not None
    })
    return bindings
