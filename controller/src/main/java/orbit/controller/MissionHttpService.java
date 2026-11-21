package orbit.controller;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Clock;
import java.time.Duration;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Loopback worker API for the web adapter. The shared credential stays server-side. */
final class MissionHttpService implements AutoCloseable {
    private static final String JSON = "application/json; charset=utf-8";
    private static final String TELEMETRY = "text/csv; charset=utf-8";
    private static final System.Logger LOG = System.getLogger(MissionHttpService.class.getName());
    private final HttpServer server;
    private final ThreadPoolExecutor requests;
    private final ScheduledExecutorService maintenance;
    private final MissionJobs jobs;
    private final byte[] authorization;

    MissionHttpService(int port, String token, MissionJobs jobs) throws IOException {
        if (token == null || !token.matches("[A-Za-z0-9_-]{32,128}")) {
            throw new IllegalArgumentException("Set ORBIT_JOB_TOKEN to a token with 32 to 128 URL-safe characters.");
        }
        this.jobs = jobs;
        authorization = ("Bearer " + token).getBytes(StandardCharsets.US_ASCII);
        server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 24);
        requests = new ThreadPoolExecutor(4, 4, 0, TimeUnit.SECONDS, new ArrayBlockingQueue<>(24));
        maintenance = Executors.newSingleThreadScheduledExecutor();
        server.setExecutor(requests);
        server.createContext("/", this::handle);
        server.start();
        maintenance.scheduleWithFixedDelay(jobs::expire, 30, 30, TimeUnit.SECONDS);
    }

    int port() {
        return server.getAddress().getPort();
    }

    private void handle(HttpExchange exchange) throws IOException {
        try (exchange) {
            String supplied = header(exchange, "Authorization");
            if (supplied == null || !MessageDigest.isEqual(authorization, supplied.getBytes(StandardCharsets.UTF_8))
                    || exchange.getRequestHeaders().containsKey("Origin")) {
                send(exchange, error(403, "The job endpoint is private."));
                return;
            }
            Reply response;
            try {
                response = route(exchange);
            } catch (MissionJobs.JobException e) {
                response = error(e.status(), e.getMessage());
            } catch (IOException e) {
                LOG.log(System.Logger.Level.WARNING, "A mission job request could not be completed.");
                response = error(500, "The job service could not finish the request.");
            }
            send(exchange, response);
        }
    }

    private Reply route(HttpExchange exchange) throws IOException, MissionJobs.JobException {
        String path = exchange.getRequestURI().getRawPath();
        String method = exchange.getRequestMethod();
        if (path.equals("/health") && method.equals("GET")) {
            return json(200, "{\"status\":\"ready\"}");
        }
        String owner = header(exchange, "X-Orbit-Owner");
        if (path.equals("/jobs") && method.equals("POST")) {
            String content = header(exchange, "Content-Type");
            if (content == null || !content.split(";", 2)[0].strip().equalsIgnoreCase("application/json")) {
                return error(415, "Send a UTF-8 JSON mission plan.");
            }
            byte[] input = exchange.getRequestBody().readNBytes(MissionJobs.MAX_PLAN_BYTES + 1);
            return snapshot(202, jobs.submit(owner, header(exchange, "Idempotency-Key"), input));
        }
        String[] parts = path.split("/", -1);
        if ((parts.length == 3 || parts.length == 4) && parts[1].equals("jobs")) {
            UUID id;
            try {
                id = UUID.fromString(parts[2]);
                if (!id.toString().equals(parts[2])) {
                    return error(404, "Mission job not found.");
                }
            } catch (IllegalArgumentException ignored) {
                return error(404, "Mission job not found.");
            }
            if (parts.length == 4 && parts[3].equals("telemetry") && method.equals("GET")) {
                return new Reply(200, TELEMETRY, jobs.result(owner, id));
            }
            if (parts.length == 3) {
                return switch (method) {
                    case "GET" -> snapshot(200, jobs.status(owner, id));
                    case "DELETE" -> snapshot(200, jobs.cancel(owner, id));
                    default -> error(405, "Use GET or DELETE for a mission job.");
                };
            }
        }
        return error(404, "Job service route not found.");
    }

    private static String header(HttpExchange exchange, String name) {
        List<String> values = exchange.getRequestHeaders().get(name);
        return values != null && values.size() == 1 ? values.get(0) : null;
    }

    private static Reply snapshot(int status, MissionJobs.Snapshot job) {
        String fields = "\"id\":" + quote(job.id().toString()) + ",\"state\":" +
                quote(job.state().name().toLowerCase(Locale.ROOT));
        if (job.failure() != null) {
            fields += ",\"message\":" + quote(job.failure());
        }
        if (job.receipt() != null) {
            var receipt = job.receipt();
            fields += ",\"outcome\":" + quote(receipt.outcome()) + ",\"elapsed_s\":" + receipt.elapsedSeconds()
                    + ",\"rows\":" + receipt.rows();
        }
        return json(status, "{" + fields + "}");
    }

    private static Reply error(int status, String message) {
        return json(status, "{\"message\":" + quote(message) + "}");
    }

    private static String quote(String value) {
        return "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"").replaceAll("[\\p{Cc}]", " ") + "\"";
    }

    private static Reply json(int status, String value) {
        return new Reply(status, JSON, value.getBytes(StandardCharsets.UTF_8));
    }

    private static void send(HttpExchange exchange, Reply response) throws IOException {
        exchange.getResponseHeaders().set("Content-Type", response.type());
        exchange.getResponseHeaders().set("Cache-Control", "no-store");
        exchange.getResponseHeaders().set("X-Content-Type-Options", "nosniff");
        exchange.sendResponseHeaders(response.status(), response.body().length);
        exchange.getResponseBody().write(response.body());
    }

    @Override
    public void close() {
        server.stop(1);
        maintenance.shutdownNow();
        requests.shutdownNow();
    }

    static int serve(String[] args) {
        if (args.length != 4) {
            System.err.println("Usage: java -jar orbit-controller.jar --serve <python> <port> <spool-directory>");
            return 2;
        }
        try {
            Path python = Path.of(args[1]).toAbsolutePath();
            Path spool = Path.of(args[3]);
            int port = Integer.parseInt(args[2]);
            if (!Files.isRegularFile(python) || !Files.isExecutable(python) || !Files.isDirectory(spool)
                    || port < 1024 || port > 65535) {
                throw new IllegalArgumentException("Supply a trusted Python executable, port, and existing spool directory.");
            }
            System.setProperty("sun.net.httpserver.maxReqTime", "10");
            System.setProperty("sun.net.httpserver.maxRspTime", "15");
            System.setProperty("jdk.httpserver.maxConnections", "64");
            System.setProperty("sun.net.httpserver.maxReqHeaders", "24");
            CountDownLatch stopped = new CountDownLatch(1);
            CountDownLatch cleaned = new CountDownLatch(1);
            Thread shutdown = new Thread(() -> {
                stopped.countDown();
                try {
                    cleaned.await(15, TimeUnit.SECONDS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            }, "orbit-job-shutdown");
            Runtime.getRuntime().addShutdownHook(shutdown);
            try (var jobs = new MissionJobs(spool, 2, 8, 64, Duration.ofMinutes(30), Clock.systemUTC(),
                     (plan, telemetry) -> MissionController.simulate(
                             python, plan, telemetry, 15, MissionJobs.MAX_RESULT_ROWS,
                             MissionJobs.MAX_RESULT_BYTES, ProcessBuilder.Redirect.DISCARD));
                 var service = new MissionHttpService(port, System.getenv("ORBIT_JOB_TOKEN"), jobs)) {
                System.out.println("ORBIT job service at http://127.0.0.1:" + service.port());
                stopped.await();
            } finally {
                cleaned.countDown();
                try {
                    Runtime.getRuntime().removeShutdownHook(shutdown);
                } catch (IllegalStateException ignored) {
                    // The shutdown hook owns the remaining JVM lifetime.
                }
            }
            return 0;
        } catch (IllegalArgumentException e) {
            System.err.println("ORBIT job service configuration is invalid. Check the executable, port, spool, and token.");
            return 2;
        } catch (IOException e) {
            System.err.println("ORBIT job service could not start or clean up its resources.");
            return 1;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return 130;
        }
    }

    private record Reply(int status, String type, byte[] body) {}
}
