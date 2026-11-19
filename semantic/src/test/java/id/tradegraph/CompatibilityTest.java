package id.tradegraph;

import static id.tradegraph.RulePreflight.Status.ACCEPTED;
import static id.tradegraph.RulePreflight.Status.UNSUPPORTED;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.List;
import java.util.Set;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.CsvSource;
import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.model.AxiomType;
import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLAxiom;
import org.semanticweb.owlapi.model.OWLDataFactory;
import org.semanticweb.owlapi.model.OWLDataProperty;
import org.semanticweb.owlapi.model.OWLLiteral;
import org.semanticweb.owlapi.model.OWLNamedIndividual;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.model.SWRLDataPropertyAtom;
import org.semanticweb.owlapi.model.SWRLVariable;
import org.semanticweb.owlapi.profiles.OWL2DLProfile;
import org.semanticweb.owlapi.profiles.violations.UseOfNonSimplePropertyInCardinalityRestriction;
import org.semanticweb.owlapi.reasoner.InferenceType;
import org.semanticweb.owlapi.vocab.OWL2Datatype;
import org.swrlapi.factory.SWRLAPIFactory;

class CompatibilityTest {
    private static final OWLDataFactory FACTORY = OWLManager.getOWLDataFactory();

    @Test
    void classifiesAndEntailsInverseChainAndNominal() throws Exception {
        var core = load("core.ttl");
        var report = new OWL2DLProfile().checkOntology(core);
        assertTrue(report.isInProfile(), report::toString);
        var reasoner = new ReasonerFactory().createReasoner(core);
        try {
            assertTrue(reasoner.isConsistent());
            reasoner.precomputeInferences(InferenceType.CLASS_HIERARCHY);
            for (var type : core.getClassesInSignature()) {
                assertTrue(reasoner.isSatisfiable(type), type::toString);
            }
            var tracedProduct = FACTORY.getOWLClass(iri("TracedProduct"));
            assertTrue(reasoner.getSuperClasses(tracedProduct, false)
                    .containsEntity(FACTORY.getOWLClass(iri("Product"))));
            assertTrue(reasoner.isEntailed(type("TracedProduct", "product_p")));
            assertTrue(reasoner.isEntailed(edge("batchFor", "batch_b", "product_p")));
            assertFalse(core.containsAxiom(materialDependency()));
            assertTrue(reasoner.isEntailed(materialDependency()));
            assertTrue(reasoner.isEntailed(type("ReferenceMaterial", "material_m")));
            assertTrue(reasoner.isEntailed(FACTORY.getOWLSameIndividualAxiom(
                    individual("referenceAlias"), individual("material_m"))));
        } finally {
            reasoner.dispose();
        }
    }

    @Test
    void requiresProvableDistinctnessForQualifiedCardinality() throws Exception {
        var core = load("core.ttl");
        var first = individual("candidateA");
        var second = individual("candidateB");
        var distinct = FACTORY.getOWLDifferentIndividualsAxiom(first, second);
        var minimum = type("TwoMaterials", "unknownCount");
        var reasoner = new ReasonerFactory().createReasoner(core);
        try {
            assertTrue(reasoner.isConsistent());
            assertFalse(reasoner.isEntailed(distinct));
            assertFalse(reasoner.isEntailed(FACTORY.getOWLSameIndividualAxiom(first, second)));
            assertFalse(reasoner.isEntailed(minimum));
            assertTrue(reasoner.isEntailed(FACTORY.getOWLSameIndividualAxiom(
                    individual("material_m"), individual("anotherAlias"))));

            core.getOWLOntologyManager().addAxiom(core, distinct);
            reasoner.flush();
            assertTrue(reasoner.isConsistent());
            assertTrue(reasoner.isEntailed(distinct));
            assertTrue(reasoner.isEntailed(minimum));
        } finally {
            reasoner.dispose();
        }
    }

    @Test
    void detectsEqualityConflict() throws Exception {
        var core = load("core.ttl");
        core.getOWLOntologyManager().addAxiom(core, FACTORY.getOWLDifferentIndividualsAxiom(
                individual("material_m"), individual("referenceAlias")));
        var reasoner = new ReasonerFactory().createReasoner(core);
        try {
            assertFalse(reasoner.isConsistent());
        } finally {
            reasoner.dispose();
        }
    }

    @Test
    void checksNonSimpleChainPropertyInCardinality() throws Exception {
        var core = load("core.ttl");
        var restriction = FACTORY.getOWLObjectMinCardinality(2,
                FACTORY.getOWLObjectProperty(iri("dependsOnMaterial")), FACTORY.getOWLClass(iri("Material")));
        core.getOWLOntologyManager().addAxiom(core,
                FACTORY.getOWLSubClassOfAxiom(FACTORY.getOWLClass(iri("Product")), restriction));
        var report = new OWL2DLProfile().checkOntology(core);
        assertFalse(report.isInProfile());
        assertTrue(report.getViolations().stream().anyMatch(violation ->
                violation instanceof UseOfNonSimplePropertyInCardinalityRestriction),
                report::toString);
    }

    @Test
    void classifiesTypedOwlValues() throws Exception {
        var core = load("core.ttl");
        var reasoner = new ReasonerFactory().createReasoner(core);
        try {
            assertTrue(reasoner.isConsistent());
            assertTrue(reasoner.isEntailed(type("MeasuredProduct", "product_p")));
        } finally {
            reasoner.dispose();
        }
    }

    @ParameterizedTest(name = "{0} over {1}")
    @CsvSource({
        "lessThan, decimal, true, false, false",
        "lessThanOrEqual, decimal, true, true, false",
        "greaterThan, decimal, false, false, true",
        "greaterThanOrEqual, decimal, false, true, true",
        "lessThan, instant, true, false, false",
        "lessThanOrEqual, instant, true, true, false",
        "greaterThan, instant, false, false, true",
        "greaterThanOrEqual, instant, false, true, true"
    })
    void comparesBoundLiteralsInDrools(String operator, String valueType, boolean belowMatches,
            boolean equalMatches, boolean aboveMatches) throws Exception {
        var manager = OWLManager.createOWLOntologyManager();
        var rules = manager.createOntology(iri("comparisons"));
        var property = FACTORY.getOWLDataProperty(iri("value"));
        var matched = FACTORY.getOWLClass(iri("Matched"));
        var subject = variable("subject");
        var value = variable("valueVariable");
        var values = valueType.equals("decimal")
                ? new Values(OWL2Datatype.XSD_DECIMAL, "0.0999999999999999999999", "0.10", "0.100",
                        "0.1000000000000000000001")
                : new Values(OWL2Datatype.XSD_DATE_TIME, "2026-01-14T23:59:59Z", "2026-01-15T00:00:00Z",
                        "2026-01-15T00:00:00Z", "2026-01-15T00:00:01Z");
        var premise = FACTORY.getSWRLDataPropertyAtom(property, subject, value);
        var filter = FACTORY.getSWRLBuiltInAtom(IRI.create(RulePreflight.SWRLB + operator),
                List.of(value, FACTORY.getSWRLLiteralArgument(values.literal(values.threshold()))));
        var rule = FACTORY.getSWRLRule(Set.of(premise, filter),
                Set.of(FACTORY.getSWRLClassAtom(matched, subject)));
        manager.addAxioms(rules, Set.of(
                FACTORY.getOWLDeclarationAxiom(property),
                FACTORY.getOWLDeclarationAxiom(matched),
                value(property, "below", values.literal(values.below())),
                value(property, "equal", values.literal(values.equal())),
                value(property, "above", values.literal(values.above())),
                FACTORY.getOWLDeclarationAxiom(individual("missingValue")),
                rule));

        inferRules(rules);
        assertEquals(belowMatches, rules.containsAxiom(type("Matched", "below")));
        assertEquals(equalMatches, rules.containsAxiom(type("Matched", "equal")));
        assertEquals(aboveMatches, rules.containsAxiom(type("Matched", "above")));
        assertFalse(rules.containsAxiom(type("Matched", "missingValue")));
    }

    @Test
    void rejectsBuiltInOutsideTheProfile() {
        var subject = variable("subject");
        var value = variable("valueVariable");
        var premise = numericPremise(subject, value);
        var outsideProfile = FACTORY.getSWRLBuiltInAtom(IRI.create(RulePreflight.SWRLB + "notEqual"),
                List.of(value, FACTORY.getSWRLLiteralArgument(FACTORY.getOWLLiteral(0))));
        var head = FACTORY.getSWRLClassAtom(FACTORY.getOWLClass(iri("RuleResult")), subject);
        assertEquals(UNSUPPORTED, RulePreflight.check(
                FACTORY.getSWRLRule(Set.of(premise, outsideProfile), Set.of(head))));
    }

    @Test
    void rejectsRecursiveValueGeneration() {
        var subject = variable("subject");
        var current = variable("current");
        var next = variable("next");
        var increment = FACTORY.getSWRLBuiltInAtom(IRI.create(RulePreflight.SWRLB + "add"),
                List.of(next, current, FACTORY.getSWRLLiteralArgument(FACTORY.getOWLLiteral(1))));
        var rule = FACTORY.getSWRLRule(Set.of(numericPremise(subject, current), increment),
                Set.of(numericPremise(subject, next)));
        assertEquals(UNSUPPORTED, RulePreflight.check(rule));
    }

    @Test
    void rejectsUnboundComparisonAndHeadVariables() {
        var subject = variable("subject");
        var current = variable("current");
        var unbound = variable("unbound");
        var premise = numericPremise(subject, current);
        var comparison = FACTORY.getSWRLBuiltInAtom(IRI.create(RulePreflight.SWRLB + "lessThan"),
                List.of(current, unbound));
        assertEquals(UNSUPPORTED, RulePreflight.check(FACTORY.getSWRLRule(
                Set.of(premise, comparison), Set.of(premise))));
        assertEquals(UNSUPPORTED, RulePreflight.check(FACTORY.getSWRLRule(
                Set.of(premise), Set.of(numericPremise(subject, unbound)))));
    }

    @Test
    void rejectsIndividualVariableAsComparisonOperand() {
        var subject = variable("subject");
        var value = variable("valueVariable");
        var premise = numericPremise(subject, value);
        var comparison = FACTORY.getSWRLBuiltInAtom(IRI.create(RulePreflight.SWRLB + "lessThan"),
                List.of(subject, value));
        assertEquals(UNSUPPORTED, RulePreflight.check(FACTORY.getSWRLRule(
                Set.of(premise, comparison), Set.of(premise))));
    }

    @Test
    void handsOffOnlyTheEntailedNamedFactToSwrl() throws Exception {
        var rules = load("rules.ttl");
        var result = type("RuleResult", "product_p");
        var importedRules = rules.getAxioms(AxiomType.SWRL_RULE);
        assertEquals(1, importedRules.size());
        var importedRule = importedRules.iterator().next();
        assertEquals(3, importedRule.getBody().size());
        assertEquals(1, importedRule.getHead().size());
        var ruleId = FACTORY.getOWLAnnotation(FACTORY.getOWLAnnotationProperty(iri("ruleId")),
                FACTORY.getOWLLiteral(iri("materialRule").toString()));
        assertTrue(importedRule.getAnnotations().contains(ruleId));
        assertFalse(rules.containsAxiom(result));
        assertFalse(rules.containsAxiom(materialDependency()));
        inferRules(rules);
        assertFalse(rules.containsAxiom(result));

        var core = load("core.ttl");
        var reasoner = new ReasonerFactory().createReasoner(core);
        try {
            assertTrue(reasoner.isConsistent());
            assertFalse(core.containsAxiom(materialDependency()));
            assertTrue(reasoner.isEntailed(materialDependency()));
            rules.getOWLOntologyManager().addAxiom(rules, materialDependency());
        } finally {
            reasoner.dispose();
        }
        inferRules(rules);
        assertTrue(rules.containsAxiom(result));
        assertFalse(core.containsAxiom(result));
    }

    private static void inferRules(OWLOntology rules) throws Exception {
        for (var rule : rules.getAxioms(AxiomType.SWRL_RULE)) {
            assertEquals(ACCEPTED, RulePreflight.check(rule), rule::toString);
        }
        SWRLAPIFactory.createSWRLRuleEngine(rules).infer();
    }

    private static OWLOntology load(String resource) throws Exception {
        try (var input = CompatibilityTest.class.getResourceAsStream("/" + resource)) {
            assertNotNull(input, resource);
            return OWLManager.createOWLOntologyManager().loadOntologyFromOntologyDocument(input);
        }
    }

    private static IRI iri(String localName) {
        return IRI.create("urn:tradegraph:fixture#" + localName);
    }

    private static OWLNamedIndividual individual(String name) {
        return FACTORY.getOWLNamedIndividual(iri(name));
    }

    private static SWRLVariable variable(String name) {
        return FACTORY.getSWRLVariable(iri(name));
    }

    private static OWLAxiom type(String type, String name) {
        return FACTORY.getOWLClassAssertionAxiom(FACTORY.getOWLClass(iri(type)), individual(name));
    }

    private static OWLAxiom edge(String property, String subject, String object) {
        return FACTORY.getOWLObjectPropertyAssertionAxiom(FACTORY.getOWLObjectProperty(iri(property)),
                individual(subject), individual(object));
    }

    private static OWLAxiom materialDependency() {
        return edge("dependsOnMaterial", "product_p", "material_m");
    }

    private static OWLAxiom value(OWLDataProperty property, String name, OWLLiteral literal) {
        return FACTORY.getOWLDataPropertyAssertionAxiom(property, individual(name), literal);
    }

    private static SWRLDataPropertyAtom numericPremise(SWRLVariable subject, SWRLVariable value) {
        return FACTORY.getSWRLDataPropertyAtom(FACTORY.getOWLDataProperty(iri("counter")), subject, value);
    }

    private record Values(OWL2Datatype datatype, String below, String threshold, String equal, String above) {
        OWLLiteral literal(String lexicalForm) {
            return FACTORY.getOWLLiteral(lexicalForm, datatype);
        }
    }
}
