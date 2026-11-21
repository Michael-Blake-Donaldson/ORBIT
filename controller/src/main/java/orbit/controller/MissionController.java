package orbit.controller;

import java.io.BufferedReader;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.util.Iterator;
import java.util.Locale;
import java.util.Objects;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
import java.util.stream.Stream;

/** Runs an isolated ORBIT worker and publishes telemetry only after a successful exit. */
public final class MissionController {
    private static final String USAGE =
            "Usage: java -jar orbit-controller-0.1.0.jar <python> <mission.json> <output.csv> [timeout-seconds]\n"
                    + "       java -jar orbit-controller-0.1.0.jar --serve <python> <port> <spool-directory>";
    private static final int DEFAULT_TIMEOUT_SECONDS = 120;
    private static final int MAX_TIMEOUT_SECONDS = 3600;
    static final int MAX_ROWS = 100_000;
    static final int MAX_CSV_BYTES = 64 * 1024 * 1024;
    private static final Pattern TERMINAL_CONTROLS = Pattern.compile("[\\p{Cc}\\p{Cf}]");

    private MissionController() {}

    public static void main(String[] args) {
        System.exit(args.length > 0 && args[0].equals("--serve") ? MissionHttpService.serve(args) : run(args));
    }

    static int run(String[] args) {
        if (args.length == 1 && args[0].equals("--help")) {
            System.out.println(USAGE);
            return 0;
        }
        if (args.length < 3 || args.length > 4) {
            System.err.println(USAGE);
            return 2;
        }
        try {
            int timeout = args.length == 4 ? Integer.parseInt(args[3]) : DEFAULT_TIMEOUT_SECONDS;
            if (timeout < 1 || timeout > MAX_TIMEOUT_SECONDS) {
                throw new IllegalArgumentException("timeout-seconds must be between 1 and 3600");
            }
            Path python = Path.of(args[0]).toAbsolutePath();
            Path plan = Path.of(args[1]).toRealPath();
            Path requestedOutput = Path.of(args[2]).toAbsolutePath();
            Path parent = requestedOutput.getParent();
            if (!Files.isRegularFile(python) || !Files.isExecutable(python)) {
                throw new IllegalArgumentException("python must point to a trusted executable");
            }
            if (!Files.isRegularFile(plan)) {
                throw new IllegalArgumentException("mission plan must be a regular file");
            }
            if (parent == null || !Files.isDirectory(parent)) {
                throw new IllegalArgumentException("output must be a file in an existing directory");
            }
            return execute(python, plan, parent.toRealPath().resolve(requestedOutput.getFileName()), timeout);
        } catch (IllegalArgumentException e) {
            diagnostic(e.getMessage());
            return 2;
        } catch (WorkerFailure e) {
            diagnostic(e.getMessage());
            return e.status();
        } catch (IOException | UnsupportedOperationException e) {
            diagnostic(e.getMessage());
            return 1;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            diagnostic("mission controller interrupted");
            return 130;
        }
    }

    private static int execute(Path python, Path plan, Path output, int timeout)
            throws IOException, InterruptedException {
        if (Files.exists(output, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("output already exists");
        }
        Path staging = Files.createTempDirectory(output.getParent(), ".orbit-controller-");
        try {
            Path telemetry = staging.resolve("telemetry.csv");
            Receipt receipt = simulate(python, plan, telemetry, timeout, MAX_ROWS, MAX_CSV_BYTES,
                    ProcessBuilder.Redirect.INHERIT);
            // Link creation publishes the completed file without replacing a concurrent result.
            Files.createLink(output, telemetry);
            System.out.printf(Locale.ROOT, "ORBIT %s at %.6f s (%d telemetry rows)%n",
                    receipt.outcome(), receipt.elapsedSeconds(), receipt.rows());
            return 0;
        } finally {
            try {
                removeStaging(staging);
            } catch (IOException e) {
                diagnostic("could not remove staging directory: " + staging);
            }
        }
    }

    static Receipt simulate(Path python, Path plan, Path telemetry, int timeout, int maxRows, int maxBytes,
                            ProcessBuilder.Redirect diagnostics) throws IOException, InterruptedException {
        ProcessBuilder command = new ProcessBuilder(
                python.toString(), "-I", "-X", "utf8", "-m", "orbit_sim", "--",
                plan.toString(), telemetry.toString())
                .directory(telemetry.getParent().toFile())
                .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                .redirectError(diagnostics);
        command.environment().remove("ORBIT_JOB_TOKEN");
        Process worker = command.start();
        try {
            worker.getOutputStream().close();
            if (!worker.waitFor(timeout, TimeUnit.SECONDS)) {
                throw new WorkerFailure(124, "simulation exceeded the " + timeout + " second timeout");
            }
            if (worker.exitValue() != 0) {
                throw new WorkerFailure(1, "simulation worker exited with code " + worker.exitValue());
            }
        } finally {
            if (worker.isAlive()) {
                terminate(worker);
            }
        }
        return readReceipt(telemetry, maxRows, maxBytes);
    }

    private static void terminate(Process worker) throws InterruptedException {
        // The Windows virtual-environment launcher can start a second Python process.
        var children = worker.descendants().toList();
        children.forEach(ProcessHandle::destroyForcibly);
        worker.destroyForcibly();
        worker.waitFor(5, TimeUnit.SECONDS);
    }

    private static Receipt readReceipt(Path telemetry, int maxRows, int maxBytes) throws IOException {
        if (!Files.isRegularFile(telemetry, LinkOption.NOFOLLOW_LINKS) || Files.size(telemetry) > maxBytes) {
            throw new IOException("worker did not provide a regular telemetry file within the size limit");
        }
        try (BufferedReader file = Files.newBufferedReader(telemetry)) {
            String header = file.readLine();
            if (header == null || !header.startsWith("time_s,kind,")) {
                throw new IOException("invalid telemetry header");
            }
            int rows = 0;
            String last = "";
            for (String line = file.readLine(); line != null; line = file.readLine()) {
                if (++rows > maxRows) {
                    throw new IOException("telemetry exceeds the row limit");
                }
                last = line;
            }
            String[] fields = last.split(",", 3);
            if (fields.length != 3 || !(fields[1].equals("complete") || fields[1].equals("impact"))) {
                throw new IOException("telemetry is missing a terminal row");
            }
            try {
                double elapsed = Double.parseDouble(fields[0]);
                if (!Double.isFinite(elapsed) || elapsed < 0) {
                    throw new NumberFormatException();
                }
                return new Receipt(fields[1], elapsed, rows);
            } catch (NumberFormatException e) {
                throw new IOException("invalid terminal telemetry timestamp", e);
            }
        }
    }

    static void removeStaging(Path staging) throws IOException {
        try (Stream<Path> descendants = Files.walk(staging)) {
            Iterator<Path> paths = descendants.sorted((left, right) ->
                    Integer.compare(right.getNameCount(), left.getNameCount())).iterator();
            while (paths.hasNext()) {
                Files.deleteIfExists(paths.next());
            }
        }
    }

    private static void diagnostic(String message) {
        String safe = TERMINAL_CONTROLS.matcher(Objects.toString(message, "operation failed")).replaceAll("?");
        System.err.println("ORBIT controller: " + safe);
    }

    record Receipt(String outcome, double elapsedSeconds, int rows) {}

    static final class WorkerFailure extends IOException {
        private final int status;

        WorkerFailure(int status, String message) {
            super(message);
            this.status = status;
        }

        int status() {
            return status;
        }
    }
}
