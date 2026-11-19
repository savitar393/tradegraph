from decimal import Decimal, localcontext

import pytest
from rdflib import Namespace, RDF, SH, URIRef

from tradegraph.kb.loader import CATALOG, TG
from tradegraph.validation.service import ValidationService

EX = Namespace("urn:tradegraph:test:validation#")
QUERY = "total-component-value"
AMOUNTS = """
ex:bom a tg:ComponentList ; tg:componentListForBatch ex:batch ;
    tg:componentListClosed true ; tg:componentEntry ex:first, ex:second .
ex:first tg:componentAmount ex:firstAmount .
ex:second tg:componentAmount ex:secondAmount .
ex:firstAmount tg:amountValue 12000000000000000000000000001.0000 ;
    tg:amountUnit ex:pieces ; tg:amountCurrency ex:idr .
ex:secondAmount tg:amountValue 0.000000000000000000000000001 ;
    tg:amountUnit ex:pieces ; tg:amountCurrency ex:idr .
"""


def evaluate(kb, case, params=None):
    return ValidationService(kb).evaluate_calculation(
        QUERY, {"componentList": EX.bom} if params is None else params, case,
    )


def test_decimal_total_records_formula_closure_and_source_revision(kb, case_factory):
    case = case_factory(AMOUNTS)
    with localcontext() as caller:
        caller.prec = 7
        finding = evaluate(kb, case)
    assert finding.kind == "CALCULATED"
    assert finding.calculation_available, finding.diagnostics
    assert finding.value == Decimal("12000000000000000000000000001.000000000000000000000000001")
    assert finding.unit_iri == str(EX.pieces)
    assert finding.currency_iri == str(EX.idr)
    assert finding.closure.confirmed is True
    assert finding.closure.scope == EX.bom
    assert finding.kb_sha256 == kb.snapshot.sha256
    assert finding.case_revision == case.revision
    assert finding.parameters == {"componentList": EX.bom, "batch": EX.batch}
    assert len(finding.facts_sha256) == 64
    assert "SUM(" in finding.formula


@pytest.mark.parametrize("scope_change", [
    ("tg:componentListClosed true ;", "tg:componentListClosed false ;"),
    ("tg:componentListClosed true ;", ""),
    ("tg:componentListClosed true ;", "tg:componentListClosed true, false ;"),
    ("tg:componentListForBatch ex:batch", "tg:componentListForBatch ex:otherBatch"),
])
def test_partial_open_or_wrong_batch_does_not_produce_total(kb, case_factory, scope_change):
    case = case_factory(AMOUNTS.replace(*scope_change), closed=True)
    finding = evaluate(kb, case)
    assert finding.calculation_available is False
    assert finding.closure.confirmed is False
    assert finding.value is None
    assert finding.diagnostics


@pytest.mark.parametrize("data_change", [
    ("tg:amountCurrency ex:idr .\n", "tg:amountCurrency ex:usd .\n", True),
    ("tg:amountUnit ex:pieces", "tg:amountUnit ex:weight", True),
    ("tg:amountValue 0.000000000000000000000000001 ;", "", False),
    ("tg:amountValue 0.000000000000000000000000001 ;", 'tg:amountValue "2.00" ;', False),
    ("tg:amountValue 0.000000000000000000000000001 ;", "tg:amountValue 0.1, 0.2 ;", False),
    ("ex:second tg:componentAmount ex:secondAmount .", "", False),
])
def test_closed_scope_with_incomplete_or_incompatible_values_has_no_total(kb, case_factory, data_change):
    old, new, only_last = data_change
    facts = new.join(AMOUNTS.rsplit(old, 1)) if only_last else AMOUNTS.replace(old, new)
    finding = evaluate(kb, case_factory(facts))
    assert finding.closure.confirmed is True
    assert finding.calculation_available is False
    assert finding.value is None


@pytest.mark.parametrize("params", [{}, {"componentList": "relative"}, {"componentList": EX.bom, "batch": EX.other}])
def test_requires_typed_parameters_and_protects_context(kb, case_factory, params):
    with pytest.raises(ValueError):
        evaluate(kb, case_factory(AMOUNTS), params)


def test_definitions_are_projected_outside_owl(kb):
    definitions = kb.dataset.graph(kb.definitions_graph_ref)
    shapes = kb.dataset.graph(kb.shapes_graph_ref)
    owl = kb.dataset.graph(kb.owl_projection_ref)
    calculation = URIRef(kb.calculation_catalog[QUERY].iri)
    assert (calculation, RDF.type, CATALOG.CalculationDefinition) in definitions
    assert (calculation, RDF.type, CATALOG.CalculationDefinition) not in owl
    assert any(shapes.triples((TG["document-evidence-shape"], None, None)))
    assert not any(str(predicate).startswith(str(SH)) for predicate in owl.predicates())
    assert (TG["document-evidence-shape"], RDF.type, SH.NodeShape) not in owl
