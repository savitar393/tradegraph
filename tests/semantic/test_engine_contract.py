"""Verify the Java engine contract through a fresh Maven run."""

import pytest


@pytest.fixture(scope="module")
def java_cases(maven_contract):
    return maven_contract("CompatibilityTest")


@pytest.mark.parametrize(
    ("method_name", "expected_runs"),
    [
        ("classifiesAndEntailsInverseChainAndNominal", 1),
        ("requiresProvableDistinctnessForQualifiedCardinality", 1),
        ("detectsEqualityConflict", 1),
        ("checksNonSimpleChainPropertyInCardinality", 1),
        ("classifiesTypedOwlValues", 1),
        ("comparesBoundLiteralsInDrools", 8),
        ("rejectsBuiltInOutsideTheProfile", 1),
        ("rejectsRecursiveValueGeneration", 1),
        ("rejectsUnboundComparisonAndHeadVariables", 1),
        ("rejectsIndividualVariableAsComparisonOperand", 1),
        ("handsOffOnlyTheEntailedNamedFactToSwrl", 1),
    ],
)
def test_engine_contract(java_cases, method_name, expected_runs):
    assert java_cases[method_name] == expected_runs
