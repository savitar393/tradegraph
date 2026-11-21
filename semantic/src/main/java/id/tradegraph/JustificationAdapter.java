package id.tradegraph;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.TimeUnit;

import com.fasterxml.jackson.databind.ObjectMapper;
import id.tradegraph.InferenceResult.Fact;
import org.semanticweb.HermiT.ReasonerFactory;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLAnnotationValue;
import org.semanticweb.owlapi.model.OWLAxiom;
import org.semanticweb.owlapi.model.OWLLiteral;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.reasoner.SimpleConfiguration;
import uk.ac.manchester.cs.owlapi.modularity.ModuleType;
import uk.ac.manchester.cs.owlapi.modularity.SyntacticLocalityModuleExtractor;

/** Find one bounded, independently replayed witness for a selected inference. */
public final class JustificationAdapter {
    private static final String KB = "http://www.semanticweb.org/tradegraph/kb#";

    record Request(ReasoningJob job, Fact target, int maxOwlChecks) {}
    record AxiomWitness(List<String> axiomIds, List<String> sourceClauses, String rendering, Fact fact) {}
    record Term(String value, String datatype, String language) {}
    record Binding(String variable, Term term) {}
    record RuleWitness(
            String ruleId, List<String> sourceClauses, List<Binding> bindings, List<Fact> premises) {}
    record Reply(
            ReasoningJob.Context context, String availability, List<AxiomWitness> axioms,
            RuleWitness rule, List<String> diagnostics) {}

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("Expected a justification request and result file.");
        }
        var json = new ObjectMapper();
        var request = json.readValue(Path.of(args[0]).toFile(), Request.class);
        json.writeValue(Path.of(args[1]).toFile(), new JustificationAdapter().justify(request));
    }

    Reply justify(Request request) {
        var job = request.job();
        try {
            var budget = new Budget(job.timeoutMs());
            var core = KbOwlValidator.load(job.corePath());
            var target = request.target().toAssertion(core.getOWLOntologyManager().getOWLDataFactory());
            return switch (request.target().kind()) {
                case "OWL_ENTAILED" -> new Reply(job.context(), "AVAILABLE",
                        owlWitness(core, target, request.maxOwlChecks(), budget), null, List.of());
                case "RULE_DERIVED" -> {
                    var replay = new RuleWitnessReplay(core, target, budget);
                    var witness = replay.find(ReasoningCoordinator.loadRules(job, core));
                    if (witness == null) {
                        yield unavailable(job, "No verified binding could be replayed from the prior handoff.");
                    }
                    yield new Reply(job.context(), "AVAILABLE", List.of(), witness, List.of());
                }
                default -> unavailable(job, "Unsupported inference kind.");
            };
        } catch (Exception error) {
            return unavailable(job, error.getClass().getSimpleName() + ": " + error.getMessage());
        }
    }

    private static List<AxiomWitness> owlWitness(
            OWLOntology core, OWLAxiom target, int maxChecks, Budget budget) throws Exception {
        budget.remainingMs();
        var extractor = new SyntacticLocalityModuleExtractor(
                OWLManager.createOWLOntologyManager(), core, ModuleType.STAR);
        List<OWLAxiom> kept = new ArrayList<>(extractor.extract(target.getSignature()).stream()
                .filter(OWLAxiom::isLogicalAxiom).sorted(Comparator.comparing(OWLAxiom::toString)).toList());
        var checks = new EntailmentChecks(maxChecks, budget, target);
        if (!checks.entails(kept)) {
            throw new IllegalStateException("The selected OWL module did not entail the finding.");
        }
        // Try removing blocks first, then each surviving axiom. Each test uses an independent HermiT ontology.
        for (int block = Math.max(1, kept.size() / 2); block > 0; block /= 2) {
            for (int offset = 0; offset < kept.size();) {
                var attempt = new ArrayList<>(kept);
                attempt.subList(offset, Math.min(offset + block, attempt.size())).clear();
                if (checks.entails(attempt)) {
                    kept = attempt;
                } else {
                    offset += block;
                }
            }
        }
        return kept.stream().map(axiom -> new AxiomWitness(
                annotations(axiom, "axiomId").stream().filter(OWLLiteral.class::isInstance)
                        .map(value -> ((OWLLiteral) value).getLiteral()).distinct().sorted().toList(),
                sourceClauses(axiom, "axiomSource"),
                axiom.getAxiomWithoutAnnotations().toString(), Fact.namedAssertion(axiom))).toList();
    }

    static List<String> sourceClauses(OWLAxiom axiom, String property) {
        return annotations(axiom, property).stream().filter(IRI.class::isInstance)
                .map(Object::toString).distinct().sorted().toList();
    }

    private static List<OWLAnnotationValue> annotations(OWLAxiom axiom, String property) {
        return axiom.getAnnotations().stream()
                .filter(annotation -> annotation.getProperty().getIRI().equals(IRI.create(KB + property)))
                .map(annotation -> annotation.getValue()).toList();
    }

    private static Reply unavailable(ReasoningJob job, String diagnostic) {
        return new Reply(job.context(), "EXPLANATION_UNAVAILABLE", List.of(), null, List.of(diagnostic));
    }

    static final class Budget {
        private final long started = System.nanoTime();
        private final long timeoutMs;

        Budget(long timeoutMs) {
            this.timeoutMs = timeoutMs;
        }

        long remainingMs() {
            long remaining = timeoutMs - TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started);
            if (remaining <= 0) {
                throw new IllegalStateException("The explanation time budget expired.");
            }
            return remaining;
        }
    }

    private static final class EntailmentChecks {
        private int remaining;
        private final Budget budget;
        private final OWLAxiom conclusion;

        EntailmentChecks(int remaining, Budget budget, OWLAxiom conclusion) {
            this.remaining = remaining;
            this.budget = budget;
            this.conclusion = conclusion;
        }

        boolean entails(List<OWLAxiom> axioms) throws Exception {
            if (remaining-- <= 0) {
                throw new IllegalStateException("The OWL entailment-check limit was reached.");
            }
            var manager = OWLManager.createOWLOntologyManager();
            Set<OWLAxiom> input = new HashSet<>(axioms);
            var ontology = manager.createOntology(input);
            var reasoner = new ReasonerFactory().createReasoner(
                    ontology, new SimpleConfiguration(budget.remainingMs()));
            try {
                return reasoner.isConsistent() && reasoner.isEntailed(conclusion);
            } finally {
                reasoner.dispose();
            }
        }
    }
}
