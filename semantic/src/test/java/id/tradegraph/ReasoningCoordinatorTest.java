package id.tradegraph;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ReasoningCoordinatorTest {
    private static final String NS = "urn:tradegraph:fixture#";

    @TempDir
    Path directory;

    @Test
    void exchangesNamedFactsUntilFixedPoint() throws Exception {
        var job = job(3, "greaterThanOrEqual", "");
        var result = new ReasoningCoordinator().reason(job);

        assertEquals("CONVERGED", result.executionStatus(), () -> result.diagnostics().toString());
        assertEquals("CONSISTENT", result.consistency());
        assertEquals("NOT_CLAIMED", result.completenessClaim());
        assertEquals(job.context(), result.context());
        assertEquals(1, find(result, "material_m", "OWL_ENTAILED").iteration());
        assertEquals(1, find(result, "RuleResult", "RULE_DERIVED").iteration());
        assertEquals(2, find(result, "ClassifiedAfterRules", "OWL_ENTAILED").iteration());
    }

    @Test
    void checksConsistencyAfterFeedbackEvenOnTheLastPass() throws Exception {
        var result = new ReasoningCoordinator().reason(job(1, "greaterThanOrEqual", """
                :Block a owl:Class ; owl:disjointWith :RuleResult .
                :product_p a :Block .
                """));

        assertEquals("FAILED", result.executionStatus(), () -> result.diagnostics().toString());
        assertEquals("INCONSISTENT", result.consistency());
        assertTrue(result.inferredFacts().isEmpty());
    }

    @Test
    void stopsAtThePassLimit() throws Exception {
        var result = new ReasoningCoordinator().reason(job(1, "greaterThanOrEqual", ""));

        assertEquals("LIMIT_REACHED", result.executionStatus(), () -> result.diagnostics().toString());
        assertEquals("CONSISTENT", result.consistency());
        assertTrue(result.inferredFacts().isEmpty());
    }

    @Test
    void rejectsUnprofiledRuleBeforeReasoning() throws Exception {
        var result = new ReasoningCoordinator().reason(job(3, "notEqual", ""));

        assertEquals("UNSUPPORTED", result.executionStatus(), () -> result.diagnostics().toString());
        assertTrue(result.inferredFacts().isEmpty());
        assertTrue(result.diagnostics().getFirst().contains("materialRule"));
    }

    private static InferenceResult.Fact find(InferenceResult result, String value, String kind) {
        return result.inferredFacts().stream()
                .filter(fact -> fact.subject().equals(NS + "product_p")
                        && fact.value().equals(NS + value) && fact.kind().equals(kind))
                .findFirst().orElseThrow();
    }

    private ReasoningJob job(int maxIterations, String builtin, String extraCore) throws Exception {
        var core = Files.readString(Path.of(getClass().getResource("/core.ttl").toURI()));
        var rules = Files.readString(Path.of(getClass().getResource("/rules.ttl").toURI()));
        var coreFile = Files.writeString(directory.resolve("core.ttl"), core + """

                :RuleResult a owl:Class ; rdfs:subClassOf :ClassifiedAfterRules .
                :ClassifiedAfterRules a owl:Class .
                """ + extraCore);
        var rulesFile = Files.writeString(directory.resolve("rules.ttl"), rules
                .replace(":ruleId", "<urn:tradegraph:kb#ruleId>")
                .replace("swrlb:greaterThanOrEqual", "swrlb:" + builtin));
        return new ReasoningJob(new ReasoningJob.Context("fixture", "fixture", "fixture", 1, NS + "context"),
                coreFile.toString(), rulesFile.toString(), Map.of(NS + "materialRule", List.of(3, 1)),
                maxIterations, 20_000);
    }
}
