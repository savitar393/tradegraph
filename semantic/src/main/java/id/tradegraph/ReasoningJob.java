package id.tradegraph;

import java.util.List;
import java.util.Map;

/** The wire job contains local projections produced from one pinned snapshot pair. */
public record ReasoningJob(
        Context context,
        String corePath,
        String rulesPath,
        Map<String, List<Integer>> ruleAtomCounts,
        int maxIterations,
        long timeoutMs) {
    public record Context(
            String kbSha256, String engineProfileId, String caseId, int caseRevision, String contextIri) {
    }
}
