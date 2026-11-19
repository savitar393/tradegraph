package id.tradegraph;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import id.tradegraph.InferenceResult.Fact;
import id.tradegraph.JustificationAdapter.Binding;
import id.tradegraph.JustificationAdapter.Budget;
import id.tradegraph.JustificationAdapter.RuleWitness;
import id.tradegraph.JustificationAdapter.Term;
import org.semanticweb.owlapi.model.OWLClassAssertionAxiom;
import org.semanticweb.owlapi.model.OWLDataFactory;
import org.semanticweb.owlapi.model.OWLDataPropertyAssertionAxiom;
import org.semanticweb.owlapi.model.OWLIndividual;
import org.semanticweb.owlapi.model.OWLIndividualAxiom;
import org.semanticweb.owlapi.model.OWLLiteral;
import org.semanticweb.owlapi.model.OWLNamedIndividual;
import org.semanticweb.owlapi.model.OWLObject;
import org.semanticweb.owlapi.model.OWLObjectPropertyAssertionAxiom;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.model.SWRLArgument;
import org.semanticweb.owlapi.model.SWRLAtom;
import org.semanticweb.owlapi.model.SWRLBuiltInAtom;
import org.semanticweb.owlapi.model.SWRLClassAtom;
import org.semanticweb.owlapi.model.SWRLDArgument;
import org.semanticweb.owlapi.model.SWRLDataPropertyAtom;
import org.semanticweb.owlapi.model.SWRLIArgument;
import org.semanticweb.owlapi.model.SWRLIndividualArgument;
import org.semanticweb.owlapi.model.SWRLLiteralArgument;
import org.semanticweb.owlapi.model.SWRLObjectPropertyAtom;
import org.semanticweb.owlapi.model.SWRLRule;
import org.semanticweb.owlapi.model.SWRLVariable;

/** Join candidate premises, then ask the pinned Drools engine to replay the exact grounding. */
final class RuleWitnessReplay {
    private record Input(OWLIndividualAxiom axiom, List<OWLObject> values) {}

    private final OWLDataFactory factory;
    private final OWLIndividualAxiom target;
    private final Budget budget;
    private final Map<OWLObject, List<Input>> byPredicate = new HashMap<>();
    private int joinsLeft = 2_048;
    private int replaysLeft = 8;

    RuleWitnessReplay(OWLOntology core, OWLIndividualAxiom target, Budget budget) {
        this.factory = core.getOWLOntologyManager().getOWLDataFactory();
        this.target = target;
        this.budget = budget;
        core.getAxioms().stream().sorted(Comparator.comparing(Object::toString)).forEach(axiom -> {
            if (Fact.namedAssertion(axiom) != null) {
                var assertion = (OWLIndividualAxiom) axiom.getAxiomWithoutAnnotations();
                byPredicate.computeIfAbsent(predicate(assertion), key -> new ArrayList<>())
                        .add(new Input(assertion, values(assertion)));
            }
        });
    }

    RuleWitness find(Map<String, SWRLRule> rules) throws Exception {
        if (byPredicate.getOrDefault(predicate(target), List.of()).stream().anyMatch(input -> input.axiom().equals(target))) {
            return null;
        }
        for (var id : rules.keySet().stream().sorted().toList()) {
            var rule = rules.get(id);
            if (RulePreflight.check(rule) != RulePreflight.Status.ACCEPTED) {
                continue;
            }
            for (var head : rule.getHead().stream().sorted(Comparator.comparing(Object::toString)).toList()) {
                if (head.getPredicate().equals(predicate(target))) {
                    Map<SWRLVariable, OWLObject> binding = new HashMap<>();
                    if (bind(arguments(head), values(target), binding)) {
                        var premises = rule.getBody().stream().filter(atom -> !(atom instanceof SWRLBuiltInAtom))
                                .sorted(Comparator.comparingInt((SWRLAtom atom) -> inputs(atom).size())
                                        .thenComparing(Object::toString)).toList();
                        var witness = join(id, rule, head, premises, 0, binding, new ArrayList<>());
                        if (witness != null) {
                            return witness;
                        }
                    }
                }
            }
        }
        return null;
    }

    private RuleWitness join(String id, SWRLRule rule, SWRLAtom head, List<SWRLAtom> premises, int index,
                             Map<SWRLVariable, OWLObject> bindings, List<OWLIndividualAxiom> used) throws Exception {
        budget.remainingMs();
        if (index == premises.size()) {
            return replay(id, rule, head, bindings, used);
        }
        var premise = premises.get(index);
        var arguments = arguments(premise);
        for (var input : inputs(premise)) {
            if (joinsLeft-- <= 0) {
                throw new IllegalStateException("The premise-binding search limit was reached.");
            }
            Map<SWRLVariable, OWLObject> next = new HashMap<>(bindings);
            if (bind(arguments, input.values(), next)) {
                used.add(input.axiom());
                var witness = join(id, rule, head, premises, index + 1, next, used);
                used.removeLast();
                if (witness != null) {
                    return witness;
                }
            }
        }
        return null;
    }

    private RuleWitness replay(String id, SWRLRule rule, SWRLAtom head, Map<SWRLVariable, OWLObject> bindings,
                               List<OWLIndividualAxiom> premises) throws Exception {
        if (replaysLeft-- <= 0) {
            throw new IllegalStateException("The Drools replay limit was reached.");
        }
        Set<SWRLAtom> groundedBody = new HashSet<>();
        for (var atom : rule.getBody()) {
            groundedBody.add(ground(atom, bindings));
        }
        var grounded = factory.getSWRLRule(groundedBody, Set.of(ground(head, bindings)), rule.getAnnotations());
        var outcome = new RuleExecutor(Map.of(id, grounded)).execute(new HashSet<>(premises));
        budget.remainingMs();
        if (!outcome.contains(target)) {
            return null;
        }
        return new RuleWitness(id, JustificationAdapter.sourceClauses(rule, "ruleSourceClause"),
                bindings.entrySet().stream()
                        .map(entry -> new Binding(entry.getKey().getIRI().toString(), term(entry.getValue())))
                        .sorted(Comparator.comparing(Binding::variable)).toList(),
                premises.stream().map(Fact::namedAssertion).distinct()
                        .sorted(Comparator.comparing(Object::toString)).toList());
    }

    private List<Input> inputs(SWRLAtom atom) {
        return byPredicate.getOrDefault(atom.getPredicate(), List.of());
    }

    private static boolean bind(List<SWRLArgument> arguments, List<OWLObject> values,
                                Map<SWRLVariable, OWLObject> bindings) {
        for (int i = 0; i < arguments.size(); i++) {
            var argument = arguments.get(i);
            var value = values.get(i);
            if (argument instanceof SWRLVariable variable) {
                var previous = bindings.putIfAbsent(variable, value);
                if (previous != null && !previous.equals(value)) {
                    return false;
                }
            } else if (!resolved(argument, bindings).equals(value)) {
                return false;
            }
        }
        return true;
    }

    private SWRLAtom ground(SWRLAtom atom, Map<SWRLVariable, OWLObject> bindings) {
        var terms = arguments(atom).stream().map(argument -> resolved(argument, bindings)).toList();
        return switch (atom) {
            case SWRLClassAtom a -> factory.getSWRLClassAtom(a.getPredicate(), individual(terms.getFirst()));
            case SWRLObjectPropertyAtom a -> factory.getSWRLObjectPropertyAtom(
                    a.getPredicate(), individual(terms.getFirst()), individual(terms.get(1)));
            case SWRLDataPropertyAtom a -> factory.getSWRLDataPropertyAtom(
                    a.getPredicate(), individual(terms.getFirst()), literal(terms.get(1)));
            case SWRLBuiltInAtom a -> factory.getSWRLBuiltInAtom(
                    a.getPredicate(), terms.stream().map(this::literal).toList());
            default -> throw new IllegalArgumentException("Unsupported rule atom in replay.");
        };
    }

    private SWRLIArgument individual(OWLObject term) {
        return factory.getSWRLIndividualArgument((OWLIndividual) term);
    }

    private SWRLDArgument literal(OWLObject term) {
        return factory.getSWRLLiteralArgument((OWLLiteral) term);
    }

    private static OWLObject resolved(SWRLArgument argument, Map<SWRLVariable, OWLObject> bindings) {
        return switch (argument) {
            case SWRLVariable variable -> bindings.get(variable);
            case SWRLIndividualArgument constant -> constant.getIndividual();
            case SWRLLiteralArgument constant -> constant.getLiteral();
            default -> throw new IllegalArgumentException("Unsupported rule argument in replay.");
        };
    }

    private static List<SWRLArgument> arguments(SWRLAtom atom) {
        return switch (atom) {
            case SWRLClassAtom a -> List.of(a.getArgument());
            case SWRLObjectPropertyAtom a -> List.of(a.getFirstArgument(), a.getSecondArgument());
            case SWRLDataPropertyAtom a -> List.of(a.getFirstArgument(), a.getSecondArgument());
            case SWRLBuiltInAtom a -> List.copyOf(a.getArguments());
            default -> throw new IllegalArgumentException("Unsupported rule atom in replay.");
        };
    }

    private static OWLObject predicate(OWLIndividualAxiom axiom) {
        return switch (axiom) {
            case OWLClassAssertionAxiom a -> a.getClassExpression();
            case OWLObjectPropertyAssertionAxiom a -> a.getProperty();
            case OWLDataPropertyAssertionAxiom a -> a.getProperty();
            default -> throw new IllegalArgumentException("Unsupported assertion in replay.");
        };
    }

    private static List<OWLObject> values(OWLIndividualAxiom axiom) {
        return switch (axiom) {
            case OWLClassAssertionAxiom a -> List.of(a.getIndividual());
            case OWLObjectPropertyAssertionAxiom a -> List.of(a.getSubject(), a.getObject());
            case OWLDataPropertyAssertionAxiom a -> List.of(a.getSubject(), a.getObject());
            default -> throw new IllegalArgumentException("Unsupported assertion in replay.");
        };
    }

    private static Term term(OWLObject object) {
        if (object instanceof OWLLiteral literal) {
            return new Term(literal.getLiteral(), literal.getDatatype().getIRI().toString(),
                    literal.hasLang() ? literal.getLang() : null);
        }
        return new Term(((OWLNamedIndividual) object).getIRI().toString(), null, null);
    }
}
