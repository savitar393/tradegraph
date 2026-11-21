"""Run scoped SHACL checks and exact decimal calculations from the canonical KB."""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from decimal import Context, Decimal, DecimalException, DivisionByZero, Inexact, InvalidOperation, Overflow, localcontext
from hashlib import sha256
from types import MappingProxyType

from pyshacl import validate
from rdflib import Graph, Literal, RDF, SH, URIRef, XSD

from tradegraph.cases.facts import TG, parse_input_facts, require_iri
from tradegraph.contracts import (
    CalculationClosure,
    CalculationFinding,
    CalculationParameter,
    CaseSnapshot,
    CompiledKB,
    EvidenceFinding,
    NamedFact,
    ValidationReport,
)
from tradegraph.graph.store import CaseGraphs, project_case_graphs
from tradegraph.kb.loader import CATALOG
from tradegraph.kb.queries import context_bindings, prepare_local_query
from tradegraph.reasoning.slicing import slice_owl_for_case


def _case_graphs(kb: CompiledKB, case: CaseSnapshot) -> CaseGraphs:
    if (kb.snapshot.sha256, kb.snapshot.engine_profile_id) != (
        case.context.kb_sha256, case.context.engine_profile_id,
    ):
        raise ValueError("Case snapshot requires its pinned KB and engine profile.")
    return project_case_graphs(case)


def _evidence_data(
    kb: CompiledKB, graphs: CaseGraphs, extra: Graph, inferred_facts: Iterable[NamedFact],
) -> Graph:
    owl = kb.dataset.graph(kb.owl_projection_ref)
    protected = {graphs.context.identifier, *graphs.evidence.subjects()}
    for source_graph in (graphs.facts, extra):
        for subject, predicate, value in source_graph:
            if predicate == TG.usesEvidenceSet or subject in protected:
                if (subject, predicate, value) not in graphs.context and (subject, predicate, value) not in graphs.evidence:
                    raise ValueError(f"Case facts cannot supply scoped metadata for {subject} and {predicate}.")
    for subject, predicate, value in owl.triples((None, TG.usesEvidenceSet, None)):
        if (subject, predicate, value) not in graphs.context and (subject, predicate, value) not in graphs.evidence:
            raise ValueError(f"Case facts cannot supply scoped metadata for {subject} and {predicate}.")
    for node in protected:
        for subject, predicate, value in owl.triples((node, None, None)):
            if (subject, predicate, value) not in graphs.context and (subject, predicate, value) not in graphs.evidence:
                raise ValueError(f"Case facts cannot supply scoped metadata for {subject} and {predicate}.")
    case_delta = Graph()
    for source_graph in (graphs.facts, extra, graphs.context, graphs.evidence):
        for triple in source_graph:
            case_delta.add(triple)
    # OWL can entail aliases for pinned context values. Only case records supply scope selectors.
    for subject, predicate, value in inferred_facts:
        if subject not in protected and predicate != TG.usesEvidenceSet:
            case_delta.add((subject, predicate, value))
    sliced_owl = slice_owl_for_case(owl, case_delta, graphs.context, Graph())
    data = Graph()
    for source_graph in (sliced_owl, case_delta):
        for triple in source_graph:
            data.add(triple)
    return data


def validate_evidence(
    kb: CompiledKB, case: CaseSnapshot, facts_turtle: str, *, inferred_facts: Iterable[NamedFact] = (),
) -> ValidationReport:
    """Evaluate KB evidence definitions against the pinned case and supplied fact projection."""
    graphs = _case_graphs(kb, case)
    extra = parse_input_facts(facts_turtle, case.case_id) if facts_turtle != case.facts_turtle else Graph()
    data = _evidence_data(kb, graphs, extra, inferred_facts)
    shapes = kb.dataset.graph(kb.shapes_graph_ref)
    available = context_bindings(graphs.context)
    findings = []
    for definition_id, definition in kb.evidence_catalog.items():
        missing = tuple(name for name in definition.parameters if name not in available)
        if missing:
            findings.append(EvidenceFinding(
                definition_id, definition.shape_iri, None, "UNDETERMINED",
                (f"Missing context parameters {', '.join(missing)}.",),
            ))
            continue
        query = prepare_local_query(definition.sparql, "SelectQuery")
        bindings = {name: available[name] for name in definition.parameters}
        nodes = {row.asdict().get("focus") for row in data.query(query, initBindings=bindings)}
        if any(not isinstance(node, URIRef) for node in nodes):
            raise ValueError(f"Evidence definition {definition_id} requires named ?focus results.")
        if not nodes:
            findings.append(EvidenceFinding(
                definition_id, definition.shape_iri, None, "UNDETERMINED",
                ("No matching evidence in the declared scope.",),
            ))
        for focus in sorted(nodes):
            conforms, result, _ = validate(
                data, shacl_graph=shapes, inference="none", advanced=False, js=False, do_owl_imports=False,
                use_shapes=[definition.shape_iri], focus_nodes=[focus],
            )
            messages = _validation_messages(result)
            findings.append(EvidenceFinding(
                definition_id, definition.shape_iri, str(focus),
                "SATISFIED" if conforms else "UNDETERMINED", messages,
                shacl_report_turtle=result.serialize(format="turtle") if isinstance(result, Graph) else None,
            ))
    diagnostics = tuple(message for finding in findings for message in finding.diagnostics)
    return ValidationReport(
        conforms=all(finding.status == "SATISFIED" for finding in findings),
        findings=diagnostics,
        scope_id=case.context.evidence_scope_id,
        shape_ids=tuple(sorted({finding.shape_id for finding in findings})),
        evidence_findings=tuple(findings),
        kb_sha256=kb.snapshot.sha256,
        case_id=case.case_id,
        case_revision=case.revision,
        evidence_scope_closed=case.evidence_scope_closed,
    )


def _validation_messages(result) -> tuple[str, ...]:
    if isinstance(result, Graph):
        return tuple(sorted({
            str(message)
            for item in result.subjects(RDF.type, SH.ValidationResult)
            for message in result.objects(item, SH.resultMessage)
        }))
    return (str(result),)


class ValidationService:
    def __init__(self, kb: CompiledKB):
        self.kb = kb

    def evaluate_calculation(
        self, query_id: str, parameters: Mapping[str, object], case: CaseSnapshot,
    ) -> CalculationFinding:
        """Run a catalogued calculation only after its scoped closure condition is established."""
        graphs = _case_graphs(self.kb, case)
        definition = self.kb.calculation_catalog.get(query_id)
        if definition is None:
            raise ValueError(f"Unknown calculation {query_id}.")
        bindings = _calculation_bindings(definition.parameters, parameters, context_bindings(graphs.context))
        data = graphs.facts + graphs.context
        guard = prepare_local_query(definition.closure_sparql, "AskQuery")
        with localcontext(exact_decimal_context(data, bindings)):
            closed = data.query(guard, initBindings=bindings).askAnswer is True
            finding = CalculationFinding(
                query_id=query_id,
                kb_sha256=self.kb.snapshot.sha256,
                case_id=case.case_id,
                case_revision=case.revision,
                facts_sha256=sha256(case.facts_turtle.encode("utf-8")).hexdigest(),
                formula=definition.sparql,
                parameters=MappingProxyType(bindings),
                closure=CalculationClosure(bindings[definition.scope_parameter], closed, definition.closure_sparql),
            )
            if not closed:
                return replace(finding, diagnostics=("Closure is unconfirmed for the requested scope.",))
            query = prepare_local_query(definition.sparql, "SelectQuery")
            try:
                rows = [row.asdict() for row in data.query(query, initBindings=bindings)]
            except DecimalException:
                return replace(finding, diagnostics=("The calculation requires exact decimal arithmetic.",))
        if len(rows) != 1:
            return replace(finding, diagnostics=("Expected one calculation result with complete inputs.",))
        value, unit, currency = (rows[0].get(name) for name in ("value", "unit", "currency"))
        amount = _decimal_value(value)
        if amount is None or not isinstance(unit, URIRef) or (currency is not None and not isinstance(currency, URIRef)):
            return replace(finding, diagnostics=("Expected a decimal value, an IRI unit, and an optional IRI currency.",))
        return replace(
            finding, value=amount, unit_iri=str(unit), currency_iri=str(currency) if currency is not None else None,
        )


def _calculation_bindings(
    definitions: tuple[CalculationParameter, ...], supplied: Mapping[str, object], context: Mapping[str, object],
) -> dict[str, URIRef | Literal]:
    expected = {parameter.name for parameter in definitions if parameter.context_binding is None}
    if set(supplied) != expected:
        raise ValueError(f"Expected calculation parameters {', '.join(sorted(expected))}.")
    bindings = {}
    for parameter in definitions:
        value = supplied[parameter.name] if parameter.context_binding is None else context.get(parameter.context_binding)
        if value is None:
            raise ValueError(f"Missing calculation context {parameter.context_binding}.")
        bindings[parameter.name] = _bind_term(parameter, value)
    return bindings


def _bind_term(parameter: CalculationParameter, value: object) -> URIRef | Literal:
    datatype = URIRef(parameter.datatype_iri)
    if datatype == CATALOG.IRI:
        if isinstance(value, (URIRef, str)) and not isinstance(value, Literal):
            return require_iri(str(value))
    else:
        python_types = {
            XSD.string: str, XSD.boolean: bool, XSD.integer: int, XSD.decimal: Decimal, XSD.dateTime: datetime,
        }
        if type(value) is python_types[datatype]:
            value = Literal(format(value, "f") if isinstance(value, Decimal) else value, datatype=datatype)
        if isinstance(value, Literal) and value.datatype == datatype and not value.ill_typed:
            parsed = value.toPython()
            if datatype == XSD.decimal and _decimal_value(value) is None:
                raise ValueError(f"Calculation parameter {parameter.name} requires a finite decimal.")
            if datatype == XSD.dateTime and (not isinstance(parsed, datetime) or parsed.utcoffset() is None):
                raise ValueError(f"Calculation parameter {parameter.name} requires a timezone.")
            return value
    raise ValueError(f"Calculation parameter {parameter.name} requires {datatype}.")


def _decimal_value(term) -> Decimal | None:
    if isinstance(term, Literal) and term.datatype == XSD.decimal:
        value = term.toPython()
        if isinstance(value, Decimal) and value.is_finite():
            return value
    return None


def exact_decimal_context(data: Graph, bindings: Mapping[str, URIRef | Literal]) -> Context:
    """Size decimal arithmetic for input precision and trap inexact results."""
    values = [value for term in data.objects() if (value := _decimal_value(term)) is not None]
    values.extend(value for term in bindings.values() if (value := _decimal_value(term)) is not None)
    whole = max((value.adjusted() + 1 for value in values), default=0)
    fraction = max((-value.as_tuple().exponent for value in values), default=0)
    precision = max(1, whole) + max(0, fraction) + max(32, len(str(len(data))))
    return Context(prec=precision, traps=[Inexact, InvalidOperation, DivisionByZero, Overflow])
