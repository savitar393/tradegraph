from dataclasses import FrozenInstanceError, replace
from datetime import datetime
import json
import sqlite3

import pytest
from rdflib import BNode, Literal, Namespace, RDF, XSD

from tradegraph.cases.repository import CaseNotFound, CaseRepository, RevisionConflict
from tradegraph.contracts import AssessmentContext, EvidenceClaim, FactPatch
from tradegraph.graph.store import CASE_DATA, TG, project_case_graphs


EX = Namespace("urn:example:case#")
FACT = (EX.product, TG.hasBatch, EX.firstBatch)


@pytest.fixture
def context():
    return AssessmentContext(
        kb_sha256="a" * 64,
        engine_profile_id="tradegraph-jdk21-owl2dl-swrl-comparisons-v1",
    )


def test_branch_preserves_base_and_main_head(tmp_path, context):
    repository = CaseRepository(tmp_path / "cases.db")
    original = repository.create_case("<urn:example:case#product> <urn:tradegraph:kb#hasBatch> "
                                      "<urn:example:case#firstBatch> .", context)
    replacement = (EX.product, TG.hasBatch, EX.alternateBatch)
    revised = repository.revise_case(
        original.case_id, original.revision,
        FactPatch(reason="Compare another batch", add_triples=(replacement,), remove_triples=(FACT,)),
        branch=True,
    )

    assert revised.revision > original.revision
    assert revised.parent_revision == original.revision
    assert repository.load_head(original.case_id) == original
    reopened = CaseRepository(tmp_path / "cases.db")
    assert reopened.load_case(original.case_id, original.revision) == original
    assert reopened.load_case(original.case_id, revised.revision) == revised
    assert FACT in project_case_graphs(original).facts
    assert FACT not in project_case_graphs(revised).facts
    assert replacement in project_case_graphs(revised).facts
    with pytest.raises(FrozenInstanceError):
        original.revision = 99


def test_stale_mainline_write_requires_an_explicit_branch(tmp_path, context):
    repository = CaseRepository(tmp_path / "cases.db")
    first = repository.create_case("", context)
    second = repository.revise_case(first.case_id, 1, FactPatch(reason="Change"))
    with pytest.raises(RevisionConflict):
        repository.revise_case(first.case_id, 1, FactPatch(reason="Stale write"))
    branch = repository.revise_case(first.case_id, 1, FactPatch(reason="What-if"), branch=True)
    assert repository.load_head(first.case_id) == second
    assert branch.revision > second.revision
    assert branch.parent_revision == first.revision
    with pytest.raises(CaseNotFound):
        repository.load_case(first.case_id, branch.revision + 1)


def test_explicit_nulls_and_evidence_claims_survive_reload(tmp_path, context):
    path = tmp_path / "cases.db"
    repository = CaseRepository(path)
    absent = EvidenceClaim(str(EX.claim), str(EX.product), str(EX.origin), str(EX.declaration))
    typed = EvidenceClaim(
        str(EX.measurement), str(EX.product), str(EX.mass), str(EX.declaration),
        value=Literal("0.100", datatype=XSD.decimal, normalize=False),
    )
    first = repository.create_case("", context, evidence_claims=(absent, typed))
    second = CaseRepository(path).load_case(first.case_id, first.revision)

    assert second == first
    assert second.context.jurisdiction_iri is None
    assert second.context.transaction_at is None
    claims = {claim.claim_iri: claim for claim in second.evidence_claims}
    assert claims[str(EX.claim)].value is None
    assert second.evidence_refs == (str(EX.claim), str(EX.measurement))
    assert str(claims[str(EX.measurement)].value) == "0.100"
    with sqlite3.connect(path) as connection:
        payload = json.loads(connection.execute("SELECT context_json FROM revisions").fetchone()[0])
    assert payload["jurisdiction_iri"] is None
    projected = project_case_graphs(second)
    assert (EX.claim, RDF.type, TG.EvidenceClaim) in projected.evidence
    assert projected.evidence.value(EX.claim, CASE_DATA.sourceReference) == EX.declaration
    assert projected.evidence.value(EX.claim, CASE_DATA.verificationStatus) == Literal("UNVERIFIED")
    assert projected.evidence.value(EX.claim, RDF.object) is None
    assert (EX.product, EX.mass, typed.value) not in projected.facts


def test_explicitly_verified_claim_is_still_a_claim(tmp_path, context):
    claim = EvidenceClaim(str(EX.claim), str(EX.product), str(EX.origin), str(EX.declaration),
                          value=EX.country, verification_status="VERIFIED")
    snapshot = CaseRepository(tmp_path / "cases.db").create_case("", context, evidence_claims=(claim,))
    projected = project_case_graphs(snapshot)
    assert (EX.claim, RDF.object, EX.country) in projected.evidence
    assert (EX.product, EX.origin, EX.country) not in projected.facts


def test_evidence_closure_can_be_revoked_and_is_cleared_for_a_new_context(tmp_path, context):
    repository = CaseRepository(tmp_path / "cases.db")
    context = replace(context, evidence_scope_id="declared-scope", jurisdiction_iri=str(EX.firstJurisdiction))
    claim = EvidenceClaim(str(EX.claim), str(EX.product), str(EX.origin), str(EX.declaration))
    first = repository.create_case("", context, evidence_claims=(claim,), evidence_scope_closed=True)
    revoked = repository.revise_case(
        first.case_id, 1, FactPatch(reason="Reopen scope", evidence_scope_closed=False), branch=True,
    )
    scoped_context = replace(context, jurisdiction_iri=str(EX.secondJurisdiction), kb_sha256="b" * 64)
    repinned = repository.revise_case(
        first.case_id, 1, FactPatch(reason="Reassess", context=scoped_context), branch=True,
    )
    other_scope = repository.revise_case(
        first.case_id, 1,
        FactPatch(reason="New evidence scope", context=replace(context, evidence_scope_id="another-scope")),
        branch=True,
    )
    assert repository.load_case(first.case_id, 1).evidence_scope_closed is True
    assert revoked.evidence_scope_closed is False
    assert repinned.evidence_scope_closed is None
    assert other_scope.evidence_refs == ()
    assert repinned.context.kb_sha256 == "b" * 64

    first_graphs, revoked_graphs, repinned_graphs = map(project_case_graphs, (first, revoked, repinned))
    assert len({g.context.identifier for g in (first_graphs, revoked_graphs, repinned_graphs)}) == 3
    assert next(first_graphs.evidence.objects(None, TG.evidenceScopeClosed)).toPython() is True
    assert next(revoked_graphs.evidence.objects(None, TG.evidenceScopeClosed)).toPython() is False
    assert list(repinned_graphs.evidence.objects(None, TG.evidenceScopeClosed)) == []
    assert (None, TG.appliesInJurisdiction, EX.firstJurisdiction) in first_graphs.context
    assert (None, TG.appliesInJurisdiction, EX.firstJurisdiction) not in repinned_graphs.context


def test_rejects_unscoped_closure_and_naive_time(tmp_path, context):
    repository = CaseRepository(tmp_path / "cases.db")
    with pytest.raises(ValueError, match="evidence_scope_id"):
        repository.create_case("", context, evidence_scope_closed=True)
    with pytest.raises(ValueError, match="timezone"):
        repository.create_case("", replace(context, transaction_at=datetime(2026, 1, 1)))


def test_aware_time_is_projected_as_utc_without_assuming_an_offset(tmp_path, context):
    time = datetime.fromisoformat("2026-01-01T08:00:00+07:00")
    snapshot = CaseRepository(tmp_path / "cases.db").create_case("", replace(context, transaction_at=time))
    assert snapshot.context.transaction_at.isoformat() == "2026-01-01T08:00:00+07:00"
    literal = next(project_case_graphs(snapshot).context.objects(None, TG.transactionAt))
    assert literal.datatype == XSD.dateTime
    assert str(literal) == "2026-01-01T01:00:00Z"


def test_can_patch_an_anonymous_node_from_the_persisted_graph(tmp_path, context):
    repository = CaseRepository(tmp_path / "cases.db")
    original = repository.create_case(
        '@prefix ex: <urn:example:case#> . ex:product ex:packing [ ex:count +01 ] .', context,
    )
    graph = project_case_graphs(original).facts
    node = graph.value(EX.product, EX.packing)
    old_count = graph.value(node, EX["count"])
    assert isinstance(node, BNode)
    assert str(old_count) == "+01"
    revised = repository.revise_case(original.case_id, 1, FactPatch(
        reason="Correct package count", remove_triples=((node, EX["count"], old_count),),
        add_triples=((node, EX["count"], Literal(2)),),
    ))
    assert project_case_graphs(revised).facts.value(node, EX["count"]) == Literal(2)
    assert project_case_graphs(original).facts.value(node, EX["count"]) == old_count


def test_patch_accepts_a_plain_rdf_string_for_a_persisted_string(tmp_path, context):
    repository = CaseRepository(tmp_path / "cases.db")
    original = repository.create_case(
        '@prefix ex: <urn:example:case#> . ex:product ex:reference "001" .', context,
    )
    revised = repository.revise_case(original.case_id, 1, FactPatch(
        reason="Remove unconfirmed reference",
        remove_triples=((EX.product, EX.reference, Literal("001")),),
    ))
    assert len(project_case_graphs(revised).facts) == 0
    assert len(project_case_graphs(original).facts) == 1


def test_rejects_a_missing_retraction_and_schema_axioms_without_committing(tmp_path, context):
    repository = CaseRepository(tmp_path / "cases.db")
    first = repository.create_case("", context)
    with pytest.raises(ValueError, match="retract"):
        repository.revise_case(first.case_id, 1, FactPatch(reason="Missing input", remove_triples=(FACT,)))
    with pytest.raises(ValueError, match="assertions"):
        repository.create_case(
            "<urn:a> <http://www.w3.org/2000/01/rdf-schema#subClassOf> <urn:b> .", context,
        )
    second = repository.revise_case(first.case_id, 1, FactPatch(reason="Valid change", add_triples=(FACT,)))
    assert second.revision == 2
