package id.tradegraph;

import java.nio.file.Path;

import com.fasterxml.jackson.databind.ObjectMapper;

/** CLI transport. Keep engine logging separate from the structured result file. */
public final class ReasoningMain {
    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            throw new IllegalArgumentException("Expected a job file and a result file.");
        }
        var json = new ObjectMapper();
        var job = json.readValue(Path.of(args[0]).toFile(), ReasoningJob.class);
        var result = new ReasoningCoordinator().reason(job);
        json.writeValue(Path.of(args[1]).toFile(), result);
    }
}
