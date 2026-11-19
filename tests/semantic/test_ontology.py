"""Check the domain ontology through the same OWLAPI and HermiT build as Task 01."""

import pytest


@pytest.fixture(scope="module")
def ontology_cases(maven_contract):
    return maven_contract("OntologyContractTest")


@pytest.mark.parametrize(
    "method_name",
    [
        "checksOwlDlConsistencyAndSatisfiability",
        "entailsMaterialChainAndInversesWithoutLegalOrigin",
        "keepsRolesAndFindingsScopedToTheirContexts",
        "derivesTypesFromAllDomainsAndRange",
        "requiresProvableDistinctnessForMaterialCardinality",
        "entailsEqualityForSingleCaseRevisionAndDetectsConflict",
        "rejectsChainSuperpropertyInCardinality",
        "existentialDoesNotSupplyNamedEvidence",
        "derivesNestedLocationAndTypesAssessmentInstants",
        "retainsTariffAxiomProvenanceAndInitialCoverage",
    ],
)
def test_ontology_contract(ontology_cases, method_name):
    assert ontology_cases[method_name] == 1
