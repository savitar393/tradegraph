package id.tradegraph;

import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.TimeUnit;

import id.tradegraph.InferenceResult.Fact;
import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owlapi.model.AxiomType;
import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLClassExpression;
import org.semanticweb.owlapi.model.OWLDataHasValue;
import org.semanticweb.owlapi.model.OWLDataProperty;
import org.semanticweb.owlapi.model.OWLDataPropertyAssertionAxiom;
import org.semanticweb.owlapi.model.OWLDataPropertyExpression;
import org.semanticweb.owlapi.model.OWLDataRestriction;
import org.semanticweb.owlapi.model.OWLIndividualAxiom;
import org.semanticweb.owlapi.model.OWLLiteral;
import org.semanticweb.owlapi.model.OWLNamedIndividual;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.model.SWRLRule;
import org.semanticweb.owlapi.profiles.OWL2DLProfile;
import org.semanticweb.owlapi.reasoner.FreshEntityPolicy;
import org.semanticweb.owlapi.reasoner.IndividualNodeSetPolicy;
import org.semanticweb.owlapi.reasoner.InferenceType;
import org.semanticweb.owlapi.reasoner.NullReasonerProgressMonitor;
import org.semanticweb.owlapi.reasoner.OWLReasoner;
import org.semanticweb.owlapi.reasoner.SimpleConfiguration;
import org.semanticweb.owlapi.reasoner.TimeOutException;

public final class ReasoningCoordinator {
    private static final IRI RULE_ID = IRI.create("http://www.semanticweb.org/tradegraph/kb#ruleId");

    public InferenceResult reason(ReasoningJob job) {
        long started = System.nanoTime();
        String consistency = null;
        try {
            var core = KbOwlValidator.load(job.corePath());
            var rules = new RuleExecutor(loadRules(job, core));
            var profile = new OWL2DLProfile().checkOntology(core);
            if (!profile.isInProfile()) {
                return stopped(job, "UNSUPPORTED", null, profile.toString());
            }
            Map<OWLIndividualAxiom, Fact> inferred = new HashMap<>();
            Set<OWLIndividualAxiom> existing = new HashSet<>();
            core.getAxioms().stream().filter(OWLIndividualAxiom.class::isInstance)
                    .map(axiom -> (OWLIndividualAxiom) axiom.getAxiomWithoutAnnotations())
                    .forEach(existing::add);
            var manager = core.getOWLOntologyManager();
            for (int iteration = 1; ; iteration++) {
                var config = new SimpleConfiguration(new NullReasonerProgressMonitor(), FreshEntityPolicy.DISALLOW,
                        remainingMs(job, started), IndividualNodeSetPolicy.BY_NAME);
                var reasoner = new ReasonerFactory().createReasoner(core, config);
                Set<OWLIndividualAxiom> closure;
                try {
                    boolean consistent = reasoner.isConsistent();
                    remainingMs(job, started);
                    if (!consistent) {
                        return stopped(job, "FAILED", "INCONSISTENT",
                                "HermiT found inconsistent OWL assertions at pass " + iteration + ".");
                    }
                    consistency = "CONSISTENT";
                    if (iteration > job.maxIterations()) {
                        return stopped(job, "LIMIT_REACHED", consistency,
                                "Reached the configured number of OWL/SWRL passes.");
                    }
                    closure = materialize(core, reasoner, job, started);
                } finally {
                    reasoner.dispose();
                }
                for (var axiom : closure) {
                    if (existing.add(axiom)) {
                        inferred.put(axiom, Fact.from(axiom, "OWL_ENTAILED", iteration));
                    }
                }
                var derived = new HashSet<>(rules.execute(closure));
                derived.removeAll(existing);
                remainingMs(job, started);
                if (derived.isEmpty()) {
                    return new InferenceResult("CONVERGED", consistency, job.context(),
                            inferred.values().stream().sorted(Comparator.comparing(Fact::toString)).toList(),
                            "NOT_CLAIMED", List.of());
                }
                // The next consistency check must inspect the changed ontology, even at the pass limit.
                consistency = null;
                for (var axiom : derived) {
                    existing.add(axiom);
                    inferred.put(axiom, Fact.from(axiom, "RULE_DERIVED", iteration));
                    manager.addAxiom(core, axiom);
                }
            }
        } catch (RuleExecutor.UnsupportedRule error) {
            return stopped(job, "UNSUPPORTED", consistency, error.getMessage());
        } catch (TimeOutException error) {
            return stopped(job, "TIMEOUT", consistency, "The semantic engine exceeded its time budget.");
        } catch (Exception error) {
            return stopped(job, "FAILED", consistency, error.getClass().getSimpleName() + ": " + error.getMessage());
        }
    }

    static Map<String, SWRLRule> loadRules(ReasoningJob job, OWLOntology core) throws Exception {
        if (job.ruleAtomCounts().isEmpty()) {
            return Map.of();
        }
        var ontology = KbOwlValidator.load(job.rulesPath());
        var factory = ontology.getOWLOntologyManager().getOWLDataFactory();
        var ruleId = factory.getOWLAnnotationProperty(RULE_ID);
        Map<String, SWRLRule> rules = new HashMap<>();
        for (var rule : ontology.getAxioms(AxiomType.SWRL_RULE)) {
            var ids = rule.getAnnotations(ruleId);
            if (ids.size() != 1 || !(ids.iterator().next().getValue() instanceof OWLLiteral literal)
                    || rules.putIfAbsent(literal.getLiteral(), rule) != null) {
                throw new IllegalStateException("Expected one unique literal ruleId per rule.");
            }
            var counts = List.of(rule.getBody().size(), rule.getHead().size());
            if (!counts.equals(job.ruleAtomCounts().get(literal.getLiteral()))) {
                throw new IllegalStateException("Rule atoms changed in OWLAPI for " + literal.getLiteral());
            }
            for (var entity : rule.getAxiomWithoutAnnotations().getSignature()) {
                if (!entity.isBuiltIn() && !core.containsEntityInSignature(entity)) {
                    throw new RuleExecutor.UnsupportedRule("Rule " + literal.getLiteral()
                            + " refers to an entity outside the OWL core: " + entity);
                }
            }
        }
        if (!rules.keySet().equals(job.ruleAtomCounts().keySet())) {
            throw new IllegalStateException("OWLAPI did not load every active rule.");
        }
        return rules;
    }

    private static Set<OWLIndividualAxiom> materialize(
            OWLOntology core, OWLReasoner reasoner, ReasoningJob job, long started) {
        var factory = core.getOWLOntologyManager().getOWLDataFactory();
        var objectProperties = core.getObjectPropertiesInSignature().stream()
                .filter(property -> !property.isBuiltIn()).toList();
        var dataProperties = core.getDataPropertiesInSignature().stream()
                .filter(property -> !property.isBuiltIn()).toList();

        // Sound candidate pruning for data properties: an OWL 2 DL ontology can entail DataPropertyAssertion(p, s, v)
        // only if p (or a sub-property) occurs in a TBox data restriction, or s (or a sameAs alias) asserts q <= p.
        Set<OWLDataProperty> tboxDataProps = new HashSet<>();
        for (OWLClassExpression expr : core.getNestedClassExpressions()) {
            if (expr instanceof OWLDataRestriction restriction
                    && restriction.getProperty() instanceof OWLDataProperty dp && !dp.isBuiltIn()) {
                tboxDataProps.add(dp);
                for (var sup : reasoner.getSuperDataProperties(dp, false).getFlattened()) {
                    if (!sup.isBuiltIn()) {
                        tboxDataProps.add(sup);
                    }
                }
            }
        }
        reasoner.precomputeInferences(InferenceType.SAME_INDIVIDUAL);
        Map<OWLDataProperty, Set<OWLDataProperty>> superDataProps = new HashMap<>();
        Map<OWLNamedIndividual, Set<OWLNamedIndividual>> sameCache = new HashMap<>();
        Map<OWLNamedIndividual, Set<OWLDataProperty>> candidateDataProps = new HashMap<>();
        for (OWLDataPropertyAssertionAxiom axiom : core.getAxioms(AxiomType.DATA_PROPERTY_ASSERTION)) {
            if (axiom.getSubject().isAnonymous() || !(axiom.getProperty() instanceof OWLDataProperty dp) || dp.isBuiltIn()) {
                continue;
            }
            var supers = superDataProps.computeIfAbsent(dp, prop -> {
                Set<OWLDataProperty> set = new HashSet<>();
                set.add(prop);
                for (var sup : reasoner.getSuperDataProperties(prop, false).getFlattened()) {
                    if (!sup.isBuiltIn()) {
                        set.add(sup);
                    }
                }
                return set;
            });
            OWLNamedIndividual subj = axiom.getSubject().asOWLNamedIndividual();
            var aliases = sameCache.computeIfAbsent(subj, key -> reasoner.getSameIndividuals(key).getEntities());
            for (var alias : aliases) {
                candidateDataProps.computeIfAbsent(alias, key -> new HashSet<>()).addAll(supers);
            }
        }

        Set<OWLIndividualAxiom> facts = new HashSet<>();
        for (var subject : core.getIndividualsInSignature()) {
            remainingMs(job, started);
            for (var type : reasoner.getTypes(subject, false).getFlattened()) {
                if (!type.isBuiltIn()) {
                    facts.add(factory.getOWLClassAssertionAxiom(type, subject));
                }
            }
            // BY_NAME retains each alias entailed by HermiT so joins and query bindings keep their IRIs.
            for (var property : objectProperties) {
                for (var object : reasoner.getObjectPropertyValues(subject, property).getFlattened()) {
                    facts.add(factory.getOWLObjectPropertyAssertionAxiom(property, subject, object));
                }
            }
            Set<OWLDataProperty> subjectDataProps = candidateDataProps.get(subject);
            if (!tboxDataProps.isEmpty()) {
                for (var property : dataProperties) {
                    if (tboxDataProps.contains(property)
                            || (subjectDataProps != null && subjectDataProps.contains(property))) {
                        for (var value : reasoner.getDataPropertyValues(subject, property)) {
                            facts.add(factory.getOWLDataPropertyAssertionAxiom(property, subject, value));
                        }
                    }
                }
            } else if (subjectDataProps != null) {
                for (var property : subjectDataProps) {
                    for (var value : reasoner.getDataPropertyValues(subject, property)) {
                        facts.add(factory.getOWLDataPropertyAssertionAxiom(property, subject, value));
                    }
                }
            }
        }
        return facts;
    }

    private static long remainingMs(ReasoningJob job, long started) {
        long remaining = job.timeoutMs() - TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started);
        if (remaining <= 0) {
            throw new TimeOutException();
        }
        return remaining;
    }

    private static InferenceResult stopped(ReasoningJob job, String status, String consistency, String diagnostic) {
        return new InferenceResult(status, consistency, job.context(), List.of(), "NOT_CLAIMED", List.of(diagnostic));
    }
}
