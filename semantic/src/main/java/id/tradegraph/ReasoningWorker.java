package id.tradegraph;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Map;

import com.fasterxml.jackson.databind.ObjectMapper;

/** Long-lived stdio JSON-lines worker that reuses one warm JVM across independent reasoning jobs. */
public final class ReasoningWorker {
    record Command(String entrypoint, String requestPath, String resultPath) {}

    public static void main(String[] args) throws Exception {
        PrintStream protocol = System.out;
        System.setOut(System.err);

        var json = new ObjectMapper();
        try (var reader = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || "exit".equals(line)) {
                    break;
                }
                try {
                    var command = json.readValue(line, Command.class);
                    switch (command.entrypoint()) {
                        case "ReasoningMain" -> {
                            var job = json.readValue(Path.of(command.requestPath()).toFile(), ReasoningJob.class);
                            var result = new ReasoningCoordinator().reason(job);
                            json.writeValue(Path.of(command.resultPath()).toFile(), result);
                        }
                        case "JustificationAdapter" -> {
                            var req = json.readValue(
                                    Path.of(command.requestPath()).toFile(), JustificationAdapter.Request.class);
                            var reply = new JustificationAdapter().justify(req);
                            json.writeValue(Path.of(command.resultPath()).toFile(), reply);
                        }
                        default -> throw new IllegalArgumentException(
                                "Unsupported worker entrypoint: " + command.entrypoint());
                    }
                    protocol.println(json.writeValueAsString(Map.of("ok", true)));
                } catch (Throwable error) {
                    String message = error.getClass().getSimpleName() + ": " + error.getMessage();
                    protocol.println(json.writeValueAsString(Map.of("ok", false, "error", message)));
                }
                protocol.flush();
            }
        }
    }
}
