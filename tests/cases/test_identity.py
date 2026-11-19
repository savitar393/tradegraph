from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
from threading import Barrier

import pytest
from rdflib import Namespace, OWL, RDF
from rdflib.compare import isomorphic

from tradegraph.cases.repository import CaseRepository, RevisionConflict
from tradegraph.contracts import AssessmentContext, EvidenceClaim, FactPatch
from tradegraph.graph.store import FusekiGraphStore, GraphRevisionConflict, TG, project_case_graphs


EX = Namespace("urn:example:identity#")
CONTEXT = AssessmentContext(kb_sha256="c" * 64, engine_profile_id="tradegraph-jdk21-owl2dl-swrl-comparisons-v1")


def test_distinct_iris_remain_open_until_identity_is_asserted(tmp_path):
    repository = CaseRepository(tmp_path / "cases.db")
    facts = "@prefix ex: <urn:example:identity#> . @prefix tg: <urn:tradegraph:kb#> . "
    first = repository.create_case(facts + "ex:batch tg:usesMaterial ex:material, ex:alias, ex:other .", CONTEXT)
    same = (EX.material, OWL.sameAs, EX.alias)
    different = (EX.material, OWL.differentFrom, EX.other)
    second = repository.revise_case(first.case_id, 1, FactPatch(
        reason="Record evidence about identities", add_triples=(same, different),
    ))
    base_graph = project_case_graphs(first).facts
    changed = project_case_graphs(repository.load_case(first.case_id, second.revision)).facts
    assert not list(base_graph.triples((None, OWL.sameAs, None)))
    assert not list(base_graph.triples((None, OWL.differentFrom, None)))
    assert set(changed.subject_objects(OWL.sameAs)) == {(EX.material, EX.alias)}
    assert set(changed.subject_objects(OWL.differentFrom)) == {(EX.material, EX.other)}


def test_concurrent_mainline_updates_one_winner(tmp_path):
    path = tmp_path / "cases.db"
    repository = CaseRepository(path)
    first = repository.create_case("", CONTEXT)
    barrier = Barrier(2)

    def change(label):
        patch = FactPatch(reason=label, add_triples=((EX.product, TG.hasBatch, EX[label]),))
        barrier.wait(timeout=10)
        try:
            return CaseRepository(path).revise_case(first.case_id, first.revision, patch)
        except RevisionConflict as conflict:
            return conflict

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, ("left", "right")))
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert repository.load_head(first.case_id).revision == 2
    assert len(project_case_graphs(repository.load_head(first.case_id)).facts) == 1


@pytest.mark.skipif(not os.environ.get("TRADEGRAPH_FUSEKI_URL"), reason="Requires a local Fuseki test dataset")
def test_fuseki_tdb2_publish_is_atomic_idempotent_and_revision_scoped(tmp_path, monkeypatch):
    store = FusekiGraphStore(os.environ["TRADEGRAPH_FUSEKI_URL"])
    repository = CaseRepository(tmp_path / "cases.db")
    claim = EvidenceClaim(str(EX.claim), str(EX.product), str(EX.origin), str(EX.source), value=EX.country)
    context = replace(CONTEXT, jurisdiction_iri=str(EX.originJurisdiction), evidence_scope_id="scope")
    root = repository.create_case(
        "@prefix ex: <urn:example:identity#> . ex:product ex:pack [ ex:count 1 ] .",
        context, evidence_claims=(claim,), evidence_scope_closed=True,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, duplicate = list(pool.map(store.publish, (root, root)))
    assert first.facts.identifier == duplicate.facts.identifier
    assert isomorphic(store.load_graph(first.facts.identifier), first.facts)
    assert (EX.claim, RDF.type, TG.EvidenceClaim) in store.load_graph(first.evidence.identifier)
    assert (EX.product, EX.origin, EX.country) not in store.load_graph(first.facts.identifier)

    incompatible = replace(root, context=replace(context, jurisdiction_iri=str(EX.otherJurisdiction)))
    with pytest.raises(GraphRevisionConflict):
        store.publish(incompatible)
    assert (None, TG.appliesInJurisdiction, EX.otherJurisdiction) not in store.load_graph(first.context.identifier)

    branch = repository.revise_case(root.case_id, 1, FactPatch(
        reason="Alternative destination", context=replace(context, jurisdiction_iri=str(EX.otherJurisdiction)),
    ), branch=True)
    request = store._request

    def fail_to_confirm(operation, **kwargs):
        if operation == "query":
            raise ConnectionError("Simulated lost confirmation")
        return request(operation, **kwargs)

    with monkeypatch.context() as failed_confirmation:
        failed_confirmation.setattr(store, "_request", fail_to_confirm)
        with pytest.raises(ConnectionError):
            store.publish(branch)
    assert repository.load_case(root.case_id, branch.revision) == branch
    alternate = store.publish(branch)
    assert isomorphic(store.load_graph(alternate.facts.identifier), alternate.facts)
    assert alternate.context.identifier != first.context.identifier
    assert (None, TG.appliesInJurisdiction, EX.otherJurisdiction) in store.load_graph(alternate.context.identifier)
    assert next(store.load_graph(first.evidence.identifier).objects(None, TG.evidenceScopeClosed)).toPython() is True
    assert list(store.load_graph(alternate.evidence.identifier).objects(None, TG.evidenceScopeClosed)) == []
