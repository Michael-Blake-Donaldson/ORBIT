package orbit.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpRequest.BodyPublishers;
import java.net.http.HttpResponse.BodyHandlers;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Duration;
import java.util.regex.Pattern;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class MissionHttpServiceTest {
    private static final String TOKEN = "controller-integration-example-token";
    private static final String KEY = "request-key-unique-01";
    private static final String CSV = MissionControllerTest.COMPLETE_CSV;
    private static final Pattern JOB = Pattern.compile("\"id\":\"([a-f0-9-]+)\"");
    private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    @TempDir
    Path spool;

    @Test
    void runsAndRetrievesOneAuthorizedJobUsingAStableSubmissionKey() throws Exception {
        try (var jobs = new MissionJobs(spool, 1, 2, 6, Duration.ofMinutes(1), Clock.systemUTC(),
                (plan, telemetry) -> {
                    Files.writeString(telemetry, CSV);
                    return new MissionController.Receipt("complete", 0, 1);
                });
             var service = new MissionHttpService(0, TOKEN, jobs, Path.of("unused"))) {
            assertEquals(403, send(request(service, "/health")).statusCode());
            assertEquals(403, send(authorized(service, "/health").header("Origin", "http://example.invalid"))
                    .statusCode());
            assertEquals(200, send(authorized(service, "/health")).statusCode());
            assertEquals(415, send(authorized(service, "/jobs").POST(BodyPublishers.ofString("{}"))).statusCode());
            var initial = submit(service, "{}");
            assertEquals(202, initial.statusCode());
            String id = id(initial.body());
            assertEquals(id, id(submit(service, "{}").body()));
            assertEquals(409, submit(service, "{\"changed\":true}").statusCode());
            assertEquals(404, send(authorized(service, "/jobs/../../anything")).statusCode());
            String path = "/jobs/" + id;
            HttpResponse<String> status = send(authorized(service, path));
            long deadline = System.nanoTime() + Duration.ofSeconds(3).toNanos();
            while (!status.body().contains("\"state\":\"complete\"") && System.nanoTime() < deadline) {
                Thread.sleep(15);
                status = send(authorized(service, path));
            }
            assertTrue(status.body().contains("\"state\":\"complete\""));
            var result = send(authorized(service, path + "/telemetry"));
            assertEquals(CSV, result.body());
            assertEquals("no-store", result.headers().firstValue("Cache-Control").orElseThrow());
            assertEquals(404, send(authorized(service, path).setHeader("X-Orbit-Owner", "other-owner")).statusCode());
            assertEquals(404, send(authorized(service, path + "/telemetry")
                    .setHeader("X-Orbit-Owner", "other-owner")).statusCode());
            assertEquals(404, send(authorized(service, path).setHeader("X-Orbit-Owner", "other-owner").DELETE())
                    .statusCode());
            var other = send(authorized(service, "/jobs").setHeader("X-Orbit-Owner", "other-owner")
                    .header("Idempotency-Key", KEY).header("Content-Type", "application/json")
                    .POST(BodyPublishers.ofString("{}")));
            assertEquals(202, other.statusCode());
            assertTrue(!id(other.body()).equals(id));
        }
    }

    private HttpResponse<String> submit(MissionHttpService service, String plan) throws Exception {
        return send(authorized(service, "/jobs").header("Content-Type", "application/json")
                .header("Idempotency-Key", KEY).POST(BodyPublishers.ofString(plan)));
    }

    private HttpResponse<String> send(HttpRequest.Builder request) throws Exception {
        return client.send(request.build(), BodyHandlers.ofString());
    }

    private static HttpRequest.Builder request(MissionHttpService service, String path) {
        return HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + service.port() + path))
                .timeout(Duration.ofSeconds(5));
    }

    private static HttpRequest.Builder authorized(MissionHttpService service, String path) {
        return request(service, path).header("Authorization", "Bearer " + TOKEN)
                .header("X-Orbit-Owner", "test-owner");
    }

    private static String id(String json) {
        var match = JOB.matcher(json);
        assertTrue(match.find());
        return match.group(1);
    }
}
