package id.tradegraph;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;

import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLAxiom;
import org.semanticweb.owlapi.model.OWLClassAssertionAxiom;
import org.semanticweb.owlapi.model.OWLDataPropertyAssertionAxiom;
import org.semanticweb.owlapi.model.OWLEntity;
import org.semanticweb.owlapi.model.OWLIndividualAxiom;
import org.semanticweb.owlapi.model.OWLLiteral;
import org.semanticweb.owlapi.model.OWLObjectPropertyAssertionAxiom;
import org.semanticweb.owlapi.model.SWRLBuiltInAtom;
import org.semanticweb.owlapi.model.SWRLDArgument;
import org.semanticweb.owlapi.model.SWRLDataPropertyAtom;
import org.semanticweb.owlapi.model.SWRLLiteralArgument;
import org.semanticweb.owlapi.model.SWRLRule;
import org.semanticweb.owlapi.model.SWRLVariable;
import org.semanticweb.owlapi.vocab.OWL2Datatype;
import org.swrlapi.factory.SWRLAPIFactory;

/** Execute positive rules on a scratch ontology containing only named facts. */
public final class RuleExecutor {
    private enum ComparisonType { DECIMAL, UTC_SECONDS, UNSUPPORTED }

    static final class UnsupportedRule extends RuntimeException {
        UnsupportedRule(String message) {
            super(message);
        }
    }

    private final Map<String, SWRLRule> rules;
    private final Set<OWLEntity> headPredicates = new HashSet<>();

    RuleExecutor(Map<String, SWRLRule> rules) {
        this.rules = Map.copyOf(rules);
        rules.forEach((id, rule) -> {
            if (RulePreflight.check(rule) != RulePreflight.Status.ACCEPTED) {
                throw new UnsupportedRule("Rule " + id + " is outside the supported SWRL syntax.");
            }
            rule.getHead().forEach(atom -> {
                headPredicates.add((OWLEntity) atom.getPredicate());
            });
        });
    }

    Set<OWLIndividualAxiom> execute(Set<OWLIndividualAxiom> facts) throws Exception {
        if (rules.isEmpty()) {
            return Set.of();
        }
        checkComparisonInputs(facts);
        var manager = OWLManager.createOWLOntologyManager();
        var factory = manager.getOWLDataFactory();
        Set<OWLAxiom> input = new HashSet<>(facts);
        input.addAll(rules.values());
        Set<OWLAxiom> declarations = new HashSet<>();
        for (var axiom : input) {
            for (var entity : axiom.getSignature()) {
                if (!entity.isBuiltIn()) {
                    declarations.add(factory.getOWLDeclarationAxiom(entity));
                }
            }
        }
        input.addAll(declarations);
        var ruleEngine = SWRLAPIFactory.createSWRLRuleEngine(manager.createOntology(input));
        ruleEngine.importAssertedOWLAxioms();
        if (ruleEngine.getNumberOfImportedSWRLRules() != rules.size()) {
            throw new IllegalStateException("SWRL engine imported a different number of active rules.");
        }
        ruleEngine.run();
        Set<OWLIndividualAxiom> derived = new HashSet<>();
        for (var axiom : ruleEngine.getInferredOWLAxioms()) {
            if (isNamedHeadFact(axiom)) {
                derived.add((OWLIndividualAxiom) axiom.getAxiomWithoutAnnotations());
            }
        }
        derived.removeAll(facts);
        return derived;
    }

    private boolean isNamedHeadFact(OWLAxiom axiom) {
        return switch (axiom) {
            case OWLClassAssertionAxiom fact -> !fact.getIndividual().isAnonymous()
                    && headPredicates.contains(fact.getClassExpression());
            case OWLObjectPropertyAssertionAxiom fact -> !fact.getSubject().isAnonymous()
                    && !fact.getObject().isAnonymous() && headPredicates.contains(fact.getProperty());
            case OWLDataPropertyAssertionAxiom fact -> !fact.getSubject().isAnonymous()
                    && headPredicates.contains(fact.getProperty());
            default -> false;
        };
    }

    private void checkComparisonInputs(Set<OWLIndividualAxiom> facts) {
        Map<OWLEntity, Set<ComparisonType>> types = new HashMap<>();
        for (var fact : facts) {
            if (fact instanceof OWLDataPropertyAssertionAxiom data) {
                types.computeIfAbsent(data.getProperty().asOWLDataProperty(), key -> new HashSet<>())
                        .add(comparisonType(data.getObject()));
            }
        }
        // A rule head can bind a subsequent comparison during the same Drools run.
        boolean changed;
        do {
            changed = false;
            for (var rule : rules.values()) {
                for (var atom : rule.getHead()) {
                    if (atom instanceof SWRLDataPropertyAtom data) {
                        var possible = operandTypes(data.getSecondArgument(), rule, types);
                        changed |= types.computeIfAbsent(data.getPredicate().asOWLDataProperty(), key -> new HashSet<>())
                                .addAll(possible);
                    }
                }
            }
        } while (changed);
        rules.forEach((id, rule) -> {
            for (var atom : rule.getBody()) {
                if (atom instanceof SWRLBuiltInAtom comparison) {
                    var left = operandTypes(comparison.getArguments().get(0), rule, types);
                    var right = operandTypes(comparison.getArguments().get(1), rule, types);
                    if (left.size() > 1 || right.size() > 1
                            || left.contains(ComparisonType.UNSUPPORTED) || right.contains(ComparisonType.UNSUPPORTED)
                            || (!left.isEmpty() && !right.isEmpty() && !left.equals(right))) {
                        throw new UnsupportedRule("Rule " + id
                                + " requires two decimals or two UTC dateTimes with second precision.");
                    }
                }
            }
        });
    }

    private static Set<ComparisonType> operandTypes(SWRLDArgument argument, SWRLRule rule,
                                                    Map<OWLEntity, Set<ComparisonType>> types) {
        if (argument instanceof SWRLLiteralArgument constant) {
            return Set.of(comparisonType(constant.getLiteral()));
        }
        var variable = (SWRLVariable) argument;
        Set<ComparisonType> possible = new HashSet<>();
        for (var atom : rule.getBody()) {
            if (atom instanceof SWRLDataPropertyAtom data && data.getSecondArgument().equals(variable)) {
                possible.addAll(types.getOrDefault(data.getPredicate().asOWLDataProperty(), Set.of()));
            }
        }
        return possible;
    }

    private static ComparisonType comparisonType(OWLLiteral value) {
        IRI type = value.getDatatype().getIRI();
        if (type.equals(OWL2Datatype.XSD_DECIMAL.getIRI())) {
            return ComparisonType.DECIMAL;
        }
        if (type.equals(OWL2Datatype.XSD_DATE_TIME.getIRI())
                && value.getLiteral().matches("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")) {
            return ComparisonType.UTC_SECONDS;
        }
        return ComparisonType.UNSUPPORTED;
    }
}
