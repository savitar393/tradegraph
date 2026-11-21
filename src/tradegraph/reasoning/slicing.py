"""Slice bulk reference ABox entries before invoking the OWL 2 DL reasoner."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from rdflib import BNode, Graph, OWL, RDF, URIRef

from tradegraph.kb.loader import TG


BULK_REFERENCE_CLASSES: frozenset[URIRef] = frozenset({
    TG.HSCode,
    TG.HSSubheading,
    TG.HSHeading,
    TG.HSChapter,
    TG.ProductSpecificRule,
    TG.OriginRuleAlternative,
    TG.TariffRateEntry,
    TG.ImportRestrictionRule,
    TG.ExportRestrictionRule,
    TG.ExportDutyRule,
    TG.SourceClause,
    TG.SourceDocument,
    TG.LegalRelationEvent,
})

REVERSE_REFERENCE_PREDICATES: frozenset[URIRef] = frozenset({
    TG.psrForSubheading,
    TG.psrForHeading,
    TG.psrForChapter,
    TG.lartasTargetCode,
    OWL.sameAs,
    OWL.differentFrom,
})

REVERSE_REASONING_PREDICATES: frozenset[URIRef] = frozenset({
    TG.psrForSubheading,
    TG.psrForHeading,
    TG.psrForChapter,
    OWL.sameAs,
    OWL.differentFrom,
})

TBOX_PIN_PREDICATES: frozenset[URIRef] = frozenset({
    OWL.hasValue,
    OWL.oneOf,
    OWL.distinctMembers,
    OWL.members,
})

_NON_REASONING_BULK_CLASSES: frozenset[URIRef] = frozenset({
    TG.TariffRateEntry,
    TG.ImportRestrictionRule,
    TG.ExportRestrictionRule,
    TG.ExportDutyRule,
    TG.SourceClause,
    TG.SourceDocument,
    TG.LegalRelationEvent,
})


@dataclass(frozen=True, slots=True)
class _SchemaSliceIndex:
    triple_count: int
    candidates: frozenset[URIRef]
    tbox_pinned: frozenset[URIRef]
    axiom_bnodes: frozenset[BNode]
    non_bulk_seeds: frozenset[URIRef]
    non_bulk_seeds_reasoning: frozenset[URIRef]


_INDEX_CACHE: WeakKeyDictionary[Graph, _SchemaSliceIndex] = WeakKeyDictionary()


def _collect_list_members(graph: Graph, head) -> set[URIRef]:
    members: set[URIRef] = set()
    seen: set = set()
    node = head
    while node is not None and node != RDF.nil and node not in seen:
        seen.add(node)
        first = graph.value(node, RDF.first)
        if isinstance(first, URIRef):
            members.add(first)
        node = graph.value(node, RDF.rest)
    return members


def _bulk_candidates(schema: Graph) -> set[URIRef]:
    """Return named individuals whose asserted types fall strictly inside bulk reference classes."""
    typed_bulk: set[URIRef] = set()
    for cls in BULK_REFERENCE_CLASSES:
        for subj in schema.subjects(RDF.type, cls):
            if isinstance(subj, URIRef):
                typed_bulk.add(subj)

    candidates: set[URIRef] = set()
    for node in typed_bulk:
        types = {t for t in schema.objects(node, RDF.type) if t != OWL.NamedIndividual}
        if types and types <= BULK_REFERENCE_CLASSES:
            candidates.add(node)
    return candidates


def _tbox_pinned_individuals(schema: Graph, candidates: set[URIRef]) -> set[URIRef]:
    """Never prune individuals referenced inside OWL nominals, hasValue, or AllDifferent lists."""
    pinned: set[URIRef] = set()
    for pred in TBOX_PIN_PREDICATES:
        for _, obj in schema.subject_objects(pred):
            if isinstance(obj, URIRef) and obj in candidates:
                pinned.add(obj)
            elif isinstance(obj, BNode):
                pinned.update(_collect_list_members(schema, obj) & candidates)
    return pinned


def _get_schema_index(schema: Graph) -> _SchemaSliceIndex:
    count = len(schema)
    cached = _INDEX_CACHE.get(schema)
    if cached is not None and cached.triple_count == count:
        return cached

    candidates = _bulk_candidates(schema)
    pinned = _tbox_pinned_individuals(schema, candidates)
    axiom_bnodes = {b for b in schema.subjects(RDF.type, OWL.Axiom) if isinstance(b, BNode)}

    non_reasoning_bulk: set[URIRef] = set()
    for cls in _NON_REASONING_BULK_CLASSES:
        for subj in schema.subjects(RDF.type, cls):
            if isinstance(subj, URIRef) and subj in candidates:
                non_reasoning_bulk.add(subj)

    non_bulk_seeds: set[URIRef] = set()
    non_bulk_seeds_reasoning: set[URIRef] = set()
    for s, p, o in schema:
        if isinstance(o, URIRef) and o in candidates:
            if isinstance(s, URIRef) and s not in candidates:
                non_bulk_seeds.add(o)
                if p != TG.hasSourceClause and o not in non_reasoning_bulk:
                    non_bulk_seeds_reasoning.add(o)
            elif s in axiom_bnodes:
                annotated_source = schema.value(s, OWL.annotatedSource)
                if annotated_source not in candidates:
                    non_bulk_seeds.add(o)
                    non_bulk_seeds_reasoning.add(o)

    index = _SchemaSliceIndex(
        triple_count=count,
        candidates=frozenset(candidates),
        tbox_pinned=frozenset(pinned),
        axiom_bnodes=frozenset(axiom_bnodes),
        non_bulk_seeds=frozenset(non_bulk_seeds),
        non_bulk_seeds_reasoning=frozenset(non_bulk_seeds_reasoning),
    )
    _INDEX_CACHE[schema] = index
    return index


def slice_owl_for_case(
    schema: Graph,
    facts: Graph,
    context: Graph,
    rules: Graph,
    *,
    for_reasoning: bool = False,
) -> Graph:
    """Return a sound OWL projection pruning bulk reference individuals unreachable from the case."""
    index = _get_schema_index(schema)
    candidates = index.candidates
    if not candidates:
        return schema

    seeds: set[URIRef] = set(index.tbox_pinned)
    seeds.update(index.non_bulk_seeds_reasoning if for_reasoning else index.non_bulk_seeds)

    for graph in (facts, context, rules):
        for s, p, o in graph:
            if isinstance(s, URIRef) and s in candidates:
                seeds.add(s)
            if isinstance(o, URIRef) and o in candidates:
                seeds.add(o)

    rev_preds = REVERSE_REASONING_PREDICATES if for_reasoning else REVERSE_REFERENCE_PREDICATES

    # Expand closure from kept bulk seeds forward and reverse (HS -> PSR / Lartas, equality).
    kept: set[URIRef] = set(seeds)
    queue: deque[URIRef] = deque(seeds)
    while queue:
        node = queue.popleft()
        for pred, obj in schema.predicate_objects(node):
            if for_reasoning and pred == TG.hasSourceClause:
                continue
            if isinstance(obj, URIRef) and obj in candidates and obj not in kept:
                kept.add(obj)
                queue.append(obj)
        for pred in rev_preds:
            for subj in schema.subjects(pred, node):
                if isinstance(subj, URIRef) and subj in candidates and subj not in kept:
                    kept.add(subj)
                    queue.append(subj)

    pruned = candidates - kept
    if not pruned:
        return schema

    pruned_axioms = {
        b for b in index.axiom_bnodes
        if schema.value(b, OWL.annotatedSource) in pruned or schema.value(b, OWL.annotatedTarget) in pruned
    }

    sliced = Graph()
    for prefix, namespace in schema.namespaces():
        sliced.bind(prefix, namespace)
    for s, p, o in schema:
        if s in pruned or o in pruned or s in pruned_axioms or o in pruned_axioms:
            continue
        sliced.add((s, p, o))
    return sliced
