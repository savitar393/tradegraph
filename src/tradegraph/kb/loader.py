"""Compile one canonical Turtle artifact into RDF graphs and definition catalogs."""

from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import re
from types import MappingProxyType

from pyparsing import ParseBaseException
from rdflib import BNode, Dataset, Graph, Literal, Namespace, OWL, RDF, RDFS, SH, URIRef, XSD
from rdflib.compare import graph_diff, isomorphic
from rdflib.exceptions import ParserError
from rdflib.plugins.sparql.parser import parseQuery

from tradegraph.contracts import (
    ActionDefinition,
    ActionEffect,
    AssessmentContext,
    CalculationDefinition,
    CalculationParameter,
    CompiledKB,
    CoverageEntry,
    EvidenceDefinition,
    FieldDefinition,
    KBSnapshot,
    ObjectiveDefinition,
    PlanningAction,
    PolicyDefinition,
    Quantity,
    QueryDefinition,
    ValidationReport,
)
from tradegraph.kb.queries import UnsupportedQuery, prepare_local_query
from tradegraph.kb.rdf import parse_turtle


TG = Namespace("http://www.semanticweb.org/tradegraph/kb#")
CATALOG = Namespace("http://www.semanticweb.org/tradegraph/catalog#")
SWRL = Namespace("http://www.w3.org/2003/11/swrl#")
SWRLA = Namespace("http://swrl.stanford.edu/ontologies/3.3/swrla.owl#")
ENGINE_PROFILE_ID = "tradegraph-jdk21-owl2dl-swrl-comparisons-v1"
COVERAGE_STATUSES = {"SUPPORTED", "PARTIAL", "OUT_OF_COVERAGE"}
RESOURCE = (URIRef, BNode)
LIST_LINKS = {
    RDF.rest, OWL.intersectionOf, OWL.unionOf, OWL.oneOf, OWL.members, OWL.distinctMembers,
    OWL.propertyChainAxiom, OWL.hasKey, OWL.withRestrictions,
    SWRL.body, SWRL.head, SWRL.arguments,
    CATALOG.parameters, CATALOG.preconditions, CATALOG.effects, CATALOG.typedParameters,
    CATALOG.goals, CATALOG.dependencies,
}
OWL_DECLARATIONS = {
    OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty,
    OWL.NamedIndividual, RDFS.Datatype,
}
SHAPE_TYPES = {SH.NodeShape, SH.PropertyShape}


class KnowledgeBaseError(ValueError):
    """Invalid canonical KB structure or incompatible engine profile."""


def _parse(path: Path, content: bytes) -> Graph:
    try:
        return parse_turtle(content, path.resolve().as_uri())
    except (SyntaxError, ValueError, ParserError) as error:
        raise KnowledgeBaseError(f"Invalid Turtle in {path}\n{error}") from error


def _term(graph, subject, predicate, types, *, optional=False):
    values = list(graph.objects(subject, predicate))
    if optional and not values:
        return None
    if len(values) != 1 or not isinstance(values[0], types):
        raise KnowledgeBaseError(f"Expected one value for {predicate} on {subject}")
    return values[0]


def _text(graph, subject, predicate):
    result = str(_term(graph, subject, predicate, Literal))
    if not result.strip():
        raise KnowledgeBaseError(f"Empty text for {predicate} on {subject}")
    return result


def _items(graph, node, types):
    values = []
    visited = set()
    while node != RDF.nil:
        if node in visited:
            raise KnowledgeBaseError("Cyclic RDF list")
        visited.add(node)
        values.append(_term(graph, node, RDF.first, types))
        node = _term(graph, node, RDF.rest, RESOURCE)
    return tuple(values)


def _list_values(graph, subject, predicate, types, *, optional=False):
    head = _term(graph, subject, predicate, RESOURCE, optional=optional)
    return () if head is None else _items(graph, head, types)


def _follows(graph, predicate, obj):
    if isinstance(obj, BNode):
        return True
    if not isinstance(obj, URIRef) or obj == RDF.nil:
        return False
    if predicate in LIST_LINKS or str(predicate).startswith(str(SH)):
        return (obj, RDF.first, None) in graph or (
            str(predicate).startswith(str(CATALOG))
            and any(str(p).startswith(str(CATALOG)) for p in graph.predicates(obj))
        )
    if predicate == RDF.first:
        return any(str(t).startswith(str(SWRL)) for t in graph.objects(obj, RDF.type)) or any(
            str(p).startswith(str(CATALOG)) for p in graph.predicates(obj)
        )
    return False


def _description(graph, roots):
    result = set()
    pending = list(roots)
    visited = set()
    while pending:
        subject = pending.pop()
        if subject in visited:
            continue
        visited.add(subject)
        for triple in graph.triples((subject, None, None)):
            _, predicate, obj = triple
            result.add(triple)
            if _follows(graph, predicate, obj):
                pending.append(obj)
    return result


def _catalog_nodes(graph, kind):
    return set(graph.subjects(RDF.type, CATALOG[kind]))


def _is_shape_statement(triple):
    _, predicate, obj = triple
    return str(predicate).startswith(str(SH)) or (predicate == RDF.type and obj in SHAPE_TYPES)


def _project(graph, ontology, rule_nodes, definition_nodes):
    rule_content = _description(graph, rule_nodes | set(graph.subjects(RDF.type, SWRL.Variable)))
    shape_roots = {triple[0] for triple in graph if _is_shape_statement(triple)}
    shapes = _description(graph, shape_roots)
    definitions = _description(graph, definition_nodes)

    core = set(graph) - rule_content - shapes - definitions
    for subject in shape_roots:
        if set(graph.objects(subject, RDF.type)) & (OWL_DECLARATIONS | {RDFS.Class}):
            core.update(triple for triple in graph.triples((subject, None, None)) if not _is_shape_statement(triple))
    # Restore structural nodes that the core still references, such as a shared owl:oneOf/sh:in list.
    core_roots = {obj for _, predicate, obj in core if _follows(graph, predicate, obj)}
    core.update(triple for triple in _description(graph, core_roots) if not _is_shape_statement(triple))

    if rule_nodes:
        terms = {term for triple in rule_content for term in triple}
        rule_content.update(
            triple for triple in core
            if triple[0] in terms and triple[1] == RDF.type and triple[2] in OWL_DECLARATIONS
        )
        rule_content.add((ontology, RDF.type, OWL.Ontology))
    return core, rule_content, shapes, definitions


def _coverage(graph, revision):
    entries = []
    for resource in sorted(graph.objects(revision, TG.declaresCoverage)):
        if not isinstance(resource, URIRef) or (resource, RDF.type, TG.CoverageDeclaration) not in graph:
            raise KnowledgeBaseError("Invalid coverage declaration")
        status = _term(graph, resource, TG.hasCoverageStatus, URIRef)
        label = _text(graph, status, RDFS.label)
        if label not in COVERAGE_STATUSES:
            raise KnowledgeBaseError(f"Invalid coverage status {label}")
        entries.append(CoverageEntry(str(resource), label))
    return tuple(entries)


def _rule_counts(graph, rules):
    counts = {}
    for rule in rules:
        if not isinstance(rule, BNode):
            raise KnowledgeBaseError("SWRL rules require a blank node with a literal ruleId")
        rule_id = _text(graph, rule, TG.ruleId)
        body = _list_values(graph, rule, SWRL.body, RESOURCE)
        head = _list_values(graph, rule, SWRL.head, RESOURCE)
        if not body or not head:
            raise KnowledgeBaseError(f"Empty SWRL body or head in rule {rule_id}")
        if rule_id in counts:
            raise KnowledgeBaseError(f"Duplicate SWRL rule ID {rule_id}")
        counts[rule_id] = (len(body), len(head))
    return MappingProxyType(counts)


def _field(graph, node, label):
    return FieldDefinition(
        str(node), label,
        str(_term(graph, node, CATALOG.property, URIRef)),
        str(_term(graph, node, CATALOG.valueType, URIRef)),
    )


def _query(graph, node, label):
    sparql = _sparql(graph, node, CATALOG.sparql)
    parameters = tuple(map(str, _list_values(graph, node, CATALOG.parameters, Literal, optional=True)))
    exact = _term(graph, node, CATALOG.exactDecimal, Literal, optional=True)
    if exact is not None and (exact.datatype != XSD.boolean or exact.ill_typed):
        raise KnowledgeBaseError(f"Exact decimal query mode requires an xsd:boolean on {node}")
    return QueryDefinition(str(node), label, sparql, parameters, exact is not None and exact.toPython() is True)


def _sparql(graph, node, predicate):
    sparql = _text(graph, node, predicate)
    try:
        parseQuery(sparql)
    except ParseBaseException as error:
        raise KnowledgeBaseError(f"Invalid SPARQL on {node}\n{error}") from error
    return sparql


def _evidence(graph, node, label):
    query = _query(graph, node, label)
    shape = _term(graph, node, CATALOG.shape, URIRef)
    if (shape, RDF.type, SH.NodeShape) not in graph:
        raise KnowledgeBaseError(f"Expected a named SHACL node shape on {node}")
    admit = _term(graph, node, CATALOG.admitEvidence, Literal, optional=True)
    if admit is not None and (admit.datatype != XSD.boolean or admit.ill_typed):
        raise KnowledgeBaseError(f"Evidence admission requires an xsd:boolean on {node}")
    return EvidenceDefinition(query.iri, label, query.sparql, query.parameters, str(shape),
                              admit is not None and admit.toPython() is True)


def _policy(graph, node, label):
    scope = _term(graph, node, CATALOG.coverageScope, URIRef)
    if (scope, RDF.type, TG.CoverageDeclaration) not in graph:
        raise KnowledgeBaseError(f"Unknown coverage declaration on {node}")
    sources = _source_clauses(graph, node)
    kind = _text(graph, node, CATALOG.policyKind)
    if kind not in {"CRITERION", "OBLIGATION"}:
        raise KnowledgeBaseError(f"Unsupported policy kind {kind}")

    def query(predicate, *, optional=False):
        value = _term(graph, node, predicate, URIRef, optional=optional)
        if value is None:
            return None
        if (value, RDF.type, CATALOG.DecisionQuery) not in graph:
            raise KnowledgeBaseError(f"Unknown decision query {value} on {node}")
        return _text(graph, value, CATALOG.id)

    jurisdiction = _term(graph, node, CATALOG.policyJurisdiction, URIRef, optional=True)
    role = _term(graph, node, CATALOG.responsibleRole, URIRef, optional=True)
    return PolicyDefinition(
        str(node), label, str(scope), kind, sources, query(CATALOG.satisfiedQuery),
        query(CATALOG.failedQuery, optional=True), query(CATALOG.exceptionQuery, optional=True),
        str(jurisdiction) if jurisdiction is not None else None, str(role) if role is not None else None,
    )


def _source_clauses(graph, node):
    sources = tuple(sorted(set(graph.objects(node, TG.hasSourceClause))))
    if not sources or any(not isinstance(source, URIRef) or (source, RDF.type, TG.SourceClause) not in graph
                          for source in sources):
        raise KnowledgeBaseError(f"{node} requires named source clauses")
    return tuple(map(str, sources))


def _typed_parameters(graph, node, *, optional=False):
    parameters = []
    names = set()
    for item in _list_values(graph, node, CATALOG.typedParameters, RESOURCE, optional=optional):
        name = _text(graph, item, CATALOG.name)
        kind = _term(graph, item, CATALOG.valueType, URIRef)
        binding = _term(graph, item, CATALOG.fromContext, Literal, optional=True)
        if name in names or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name):
            raise KnowledgeBaseError(f"Invalid or duplicate parameter {name}")
        if kind not in {CATALOG.IRI, XSD.string, XSD.decimal, XSD.integer, XSD.boolean, XSD.dateTime}:
            raise KnowledgeBaseError(f"Unsupported parameter type {kind}")
        names.add(name)
        parameters.append(CalculationParameter(name, str(kind), str(binding) if binding is not None else None))
    return tuple(parameters)


def _calculation(graph, node, label):
    parameters = _typed_parameters(graph, node)
    scope = _text(graph, node, CATALOG.scopeParameter)
    if not any(parameter.name == scope and parameter.datatype_iri == str(CATALOG.IRI) for parameter in parameters):
        raise KnowledgeBaseError(f"Calculation scope parameter {scope} requires an IRI")
    return CalculationDefinition(
        str(node), label, _sparql(graph, node, CATALOG.sparql), parameters,
        _sparql(graph, node, CATALOG.closureSparql), scope,
    )


def _action(graph, node, label):
    conditions = _list_values(graph, node, CATALOG.preconditions, RESOURCE, optional=True)
    effects = _list_values(graph, node, CATALOG.effects, RESOURCE)
    if (node, CATALOG.goals, None) not in graph:
        return ActionDefinition(str(node), label, conditions, effects)
    goals = _list_values(graph, node, CATALOG.goals, URIRef)
    if not goals or any((goal, RDF.type, CATALOG.GoalDefinition) not in graph for goal in goals):
        raise KnowledgeBaseError(f"Action {node} requires named planning goals")
    parameters = _typed_parameters(graph, node, optional=True)
    grounding = _planning_query(graph, node, CATALOG.groundingSparql, "SelectQuery", optional=True)
    if grounding is None and any(parameter.context_binding is None for parameter in parameters):
        raise KnowledgeBaseError(f"Unbound parameters require a grounding query on {node}")
    applicable = _planning_query(graph, node, CATALOG.applicabilitySparql, "AskQuery", optional=True)
    predicates = tuple(_planning_query(graph, item, CATALOG.sparql, "AskQuery") for item in conditions)
    changes = []
    for effect in effects:
        operation = _text(graph, effect, CATALOG.operation)
        if operation not in {"ADD", "DELETE"}:
            raise KnowledgeBaseError(f"Unknown planning effect operation {operation}")
        changes.append(ActionEffect(operation, _planning_query(graph, effect, CATALOG.sparql, "ConstructQuery")))
    if not changes:
        raise KnowledgeBaseError(f"Executable action {node} requires effects")
    planning = PlanningAction(
        parameters, grounding, ((applicable,) if applicable else ()) + predicates, tuple(changes),
        tuple(map(str, _list_values(graph, node, CATALOG.dependencies, Literal, optional=True))),
        tuple(map(str, goals)), _source_clauses(graph, node),
        _quantity(graph, node, CATALOG.cost), _quantity(graph, node, CATALOG.duration),
    )
    return ActionDefinition(str(node), label, conditions, effects, planning)


def _planning_query(graph, node, predicate, kind, *, optional=False):
    value = _term(graph, node, predicate, Literal, optional=optional)
    if value is None:
        return None
    sparql = str(value)
    try:
        prepare_local_query(sparql, kind)
    except (ParseBaseException, UnsupportedQuery) as error:
        raise KnowledgeBaseError(f"Invalid planning query on {node}: {error}") from error
    return sparql


def _quantity(graph, node, predicate):
    item = _term(graph, node, predicate, RESOURCE, optional=True)
    if item is None:
        return None
    value = _term(graph, item, CATALOG.amount, Literal)
    if value.datatype not in {XSD.decimal, XSD.integer} or value.ill_typed:
        raise KnowledgeBaseError(f"Expected an exact decimal amount on {item}")
    number = Decimal(value.toPython())
    if not number.is_finite() or number < 0:
        raise KnowledgeBaseError(f"Expected a finite nonnegative amount on {item}")
    unit = _term(graph, item, CATALOG.unit, URIRef)
    return Quantity(number, str(unit), _source_clauses(graph, item))


def _goal(graph, node, label):
    return QueryDefinition(str(node), label, _planning_query(graph, node, CATALOG.sparql, "AskQuery"), ())


def _objective(graph, node, label):
    metric = _text(graph, node, CATALOG.metric)
    unit = _term(graph, node, CATALOG.unit, URIRef, optional=True)
    if metric not in {"STEPS", "COST", "DURATION"} or (metric == "STEPS") != (unit is None):
        raise KnowledgeBaseError(f"Objective {node} requires a supported metric and the corresponding unit")
    return ObjectiveDefinition(metric, str(unit) if unit is not None else None)


def _catalog(graph, nodes, name, build):
    entries = {}
    for node in sorted(nodes):
        if not isinstance(node, URIRef):
            raise KnowledgeBaseError(f"Expected a named {name} definition")
        key = _text(graph, node, CATALOG.id)
        if key in entries:
            raise KnowledgeBaseError(f"Duplicate {name} ID {key}")
        entries[key] = build(graph, node, _text(graph, node, RDFS.label))
    return MappingProxyType(entries)


def compile_kb(path: Path, profile_id: str) -> CompiledKB:
    """Pin exact source bytes and compile structurally checked RDF projections."""
    path = path.resolve()
    content = path.read_bytes()
    source = _parse(path, content)
    if (None, OWL.imports, None) in source:
        raise KnowledgeBaseError("owl:imports is outside the self-contained KB contract")
    ontologies = set(source.subjects(RDF.type, OWL.Ontology))
    if len(ontologies) != 1 or not isinstance(next(iter(ontologies)), URIRef):
        raise KnowledgeBaseError("Expected one named owl:Ontology")
    ontology = ontologies.pop()
    profile = _text(source, ontology, TG.compatibleEngineProfile)
    if profile != profile_id:
        raise KnowledgeBaseError(f"Engine profile mismatch. Expected {profile_id}, found {profile}")
    version = _text(source, ontology, OWL.versionInfo)
    revision = _term(source, ontology, TG.currentRevision, URIRef)
    if (revision, RDF.type, TG.KnowledgeRevision) not in source or _text(
        source, revision, TG.knowledgeVersion
    ) != version:
        raise KnowledgeBaseError("Current revision does not match ontology versionInfo")

    fields = _catalog_nodes(source, "FieldDefinition")
    queries = _catalog_nodes(source, "QueryDefinition")
    actions = _catalog_nodes(source, "ActionDefinition")
    evidence = _catalog_nodes(source, "EvidenceDefinition")
    calculations = _catalog_nodes(source, "CalculationDefinition")
    policies = _catalog_nodes(source, "PolicyDefinition")
    decisions = _catalog_nodes(source, "DecisionQuery")
    scopes = _catalog_nodes(source, "CoverageQuery")
    goals = _catalog_nodes(source, "GoalDefinition")
    objectives = _catalog_nodes(source, "ObjectiveDefinition")
    action_catalog = _catalog(source, actions, "action", _action)
    executable = {key for key, action in action_catalog.items() if action.planning is not None}
    for key in executable:
        missing = set(action_catalog[key].planning.dependencies) - executable
        if missing:
            raise KnowledgeBaseError(f"Action {key} has unknown executable dependencies {sorted(missing)}")
    coverage_queries = _catalog(source, scopes, "coverage query", _query)
    if len(coverage_queries) > 1 or (policies and not coverage_queries):
        raise KnowledgeBaseError("Assessment policies require exactly one coverage query")
    rules = set(source.subjects(RDF.type, SWRL.Imp))
    rule_counts = _rule_counts(source, rules)
    core, rule_content, shapes, definitions = _project(
        source, ontology, rules,
        fields | queries | actions | evidence | calculations | policies | decisions | scopes | goals | objectives,
    )

    digest = sha256(content).hexdigest()
    snapshot = KBSnapshot(str(ontology), version, digest, profile, _coverage(source, revision), str(path))
    dataset = Dataset()
    for prefix, namespace in source.namespaces():
        dataset.bind(prefix, namespace, replace=True)
    graph_refs = []
    for label, triples in (
        ("owl", core), ("rules", rule_content), ("shapes", shapes),
        ("definitions", definitions), ("reference", source),
    ):
        named_graph = dataset.graph(URIRef(f"http://www.semanticweb.org/tradegraph/projection/{digest}/{label}"))
        for triple in triples:
            named_graph.add(triple)
        graph_refs.append(named_graph.identifier)
    return CompiledKB(
        snapshot, content, dataset, *graph_refs,
        _catalog(source, fields, "field", _field),
        _catalog(source, queries, "query", _query),
        action_catalog,
        rule_counts,
        _catalog(source, evidence, "evidence", _evidence),
        _catalog(source, calculations, "calculation", _calculation),
        _catalog(source, policies, "policy", _policy),
        _catalog(source, decisions, "decision query", _query),
        next(iter(coverage_queries.values()), None),
        MappingProxyType({goal.iri: goal for goal in _catalog(source, goals, "goal", _goal).values()}),
        _catalog(source, objectives, "objective", _objective),
    )


def _rule_time(graph, rule, predicate):
    value = _term(graph, rule, predicate, Literal, optional=True)
    if value is None:
        return None
    timestamp = value.toPython()
    if value.datatype != XSD.dateTime or not isinstance(timestamp, datetime) or timestamp.utcoffset() != timedelta(0):
        raise KnowledgeBaseError(f"{predicate} requires an xsd:dateTime in UTC")
    return timestamp


def _rule_applies(graph, rule, context):
    enabled = _term(graph, rule, SWRLA.isRuleEnabled, Literal, optional=True)
    if enabled is not None:
        if enabled.datatype != XSD.boolean or str(enabled) not in {"true", "false", "1", "0"}:
            raise KnowledgeBaseError("swrla:isRuleEnabled requires an xsd:boolean")
        if not enabled.toPython():
            return False

    selectors = [
        (_term(graph, rule, predicate, URIRef, optional=True), actual)
        for predicate, actual in (
            (TG.ruleJurisdiction, context.jurisdiction_iri),
            (TG.ruleProcedure, context.procedure_iri),
            (TG.ruleAgreement, context.agreement_iri),
        )
    ]
    start = _rule_time(graph, rule, TG.ruleEffectiveFrom)
    end = _rule_time(graph, rule, TG.ruleEffectiveUntil)
    if start is not None and end is not None and start >= end:
        raise KnowledgeBaseError("The rule effective interval must have a start before its end")
    if any(expected is not None and actual is not None and str(expected) != actual for expected, actual in selectors):
        return False
    transaction = context.transaction_at
    if transaction is not None and ((start is not None and transaction < start)
                                    or (end is not None and transaction >= end)):
        return False
    if any(expected is not None and actual is None for expected, actual in selectors):
        raise KnowledgeBaseError("A context selector is missing")
    if transaction is None and (start is not None or end is not None):
        raise KnowledgeBaseError("Transaction time is required for a dated rule")
    return True


def select_rules(kb: CompiledKB, context: AssessmentContext) -> tuple[Graph, dict[str, tuple[int, int]]]:
    """Keep rules applicable to the pinned context and preserve shared RDF list nodes."""
    source = kb.dataset.graph(kb.rule_projection_ref)
    rules = set(source.subjects(RDF.type, SWRL.Imp))
    active = set()
    counts = {}
    for rule in rules:
        rule_id = _text(source, rule, TG.ruleId)
        try:
            if _rule_applies(source, rule, context):
                active.add(rule)
                counts[rule_id] = kb.rule_atom_counts[rule_id]
        except KnowledgeBaseError as error:
            raise KnowledgeBaseError(f"Rule {rule_id}. {error}") from error
    selected = Graph()
    removed = _description(source, rules - active) - _description(source, active)
    for triple in set(source) - removed:
        selected.add(triple)
    return selected, counts


def verify_roundtrip(before: Path, after: Path) -> ValidationReport:
    """Compare RDF graphs, including lexical forms and blank-node relationships."""
    try:
        original = _parse(before, before.read_bytes())
        saved = _parse(after, after.read_bytes())
    except (KnowledgeBaseError, OSError) as error:
        return ValidationReport(False, (str(error),))
    if isomorphic(original, saved):
        return ValidationReport(True, ())
    _, removed, added = graph_diff(original, saved)
    return ValidationReport(
        False,
        (f"RDF graphs differ modulo blank-node IDs. Removed {len(removed)}, added {len(added)} triples.",),
    )
