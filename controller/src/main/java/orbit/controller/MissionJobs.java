package orbit.controller;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;

/** Bounded, single-instance job execution. Only completed jobs expose telemetry. */
final class MissionJobs implements AutoCloseable {
    static final int MAX_PLAN_BYTES = 256 * 1024;
    static final int MAX_RESULT_BYTES = 4 * 1024 * 1024;
    static final int MAX_RESULT_ROWS = 4_000;
    private static final Pattern KEY = Pattern.compile("[A-Za-z0-9_-]{16,80}");
    private static final Pattern OWNER = Pattern.compile("[A-Za-z0-9_-]{1,128}");

    enum State {
        QUEUED, RUNNING, CANCELLING, COMPLETE, FAILED, CANCELLED;

        boolean terminal() {
            return this == COMPLETE || this == FAILED || this == CANCELLED;
        }
    }

    record Snapshot(UUID id, State state, MissionController.Receipt receipt, String failure) {}

    private record OwnerKey(String owner, String key) {}

    @FunctionalInterface
    interface Worker {
        MissionController.Receipt run(Path plan, Path telemetry) throws IOException, InterruptedException;
    }

    static final class JobException extends Exception {
        private final int status;

        JobException(int status, String message) {
            super(message);
            this.status = status;
        }

        int status() {
            return status;
        }
    }

    private final Path spool;
    private final Duration retention;
    private final Clock clock;
    private final Worker worker;
    private final ThreadPoolExecutor workers;
    private final int maxEntries;
    private final Map<UUID, Job> jobs = new HashMap<>();
    private final Map<OwnerKey, Job> keys = new HashMap<>();
    private boolean closed;

    MissionJobs(Path spool, int parallelism, int queueSize, int maxEntries, Duration retention, Clock clock,
                Worker worker) throws IOException {
        this.spool = Files.createTempDirectory(spool.toRealPath(), "orbit-jobs-");
        this.retention = retention;
        this.clock = clock;
        this.worker = worker;
        this.maxEntries = maxEntries;
        workers = new ThreadPoolExecutor(parallelism, parallelism, 0, TimeUnit.SECONDS,
                new ArrayBlockingQueue<>(queueSize));
    }

    synchronized Snapshot submit(String owner, String key, byte[] plan) throws JobException, IOException {
        requireOwner(owner);
        if (key == null || !KEY.matcher(key).matches()) {
            throw new JobException(400, "Provide a valid idempotency key.");
        }
        if (plan.length == 0 || plan.length > MAX_PLAN_BYTES) {
            throw new JobException(413, "The job requires a mission plan within 256 KiB.");
        }
        if (closed) {
            throw new JobException(503, "The job service is shutting down.");
        }
        expire();
        byte[] hash = digest(plan);
        OwnerKey ownerKey = new OwnerKey(owner, key);
        Job existing = keys.get(ownerKey);
        if (existing != null) {
            if (!Arrays.equals(existing.hash, hash)) {
                throw new JobException(409, "This idempotency key is already bound to a different plan.");
            }
            return existing.snapshot();
        }
        if (jobs.size() >= maxEntries) {
            throw new JobException(503, "The job store is at capacity. Try again later.");
        }

        Job job = new Job(ownerKey, hash, plan.clone());
        try {
            workers.execute(job);
        } catch (RejectedExecutionException ignored) {
            throw new JobException(503, "The mission queue is full. Try again shortly.");
        }
        jobs.put(job.id, job);
        keys.put(ownerKey, job);
        return job.snapshot();
    }

    synchronized Snapshot status(String owner, UUID id) throws JobException, IOException {
        expire();
        return find(owner, id).snapshot();
    }

    synchronized Snapshot cancel(String owner, UUID id) throws JobException, IOException {
        expire();
        Job job = find(owner, id);
        cancel(job);
        return job.snapshot();
    }

    private void cancel(Job job) {
        if (job.state == State.QUEUED) {
            workers.remove(job);
            job.body = null;
            job.finish(State.CANCELLED);
        } else if (job.state == State.RUNNING) {
            job.state = State.CANCELLING;
            job.thread.interrupt();
        }
    }

    synchronized byte[] result(String owner, UUID id) throws JobException, IOException {
        expire();
        Job job = find(owner, id);
        if (job.state != State.COMPLETE) {
            throw new JobException(409, "Telemetry is available only for successfully completed jobs.");
        }
        try (var input = Files.newInputStream(job.directory.resolve("telemetry.csv"))) {
            byte[] result = input.readNBytes(MAX_RESULT_BYTES + 1);
            if (result.length > MAX_RESULT_BYTES) {
                throw new IOException("Job telemetry exceeds the result budget.");
            }
            return result;
        }
    }

    synchronized void expire() throws IOException {
        Instant now = clock.instant();
        var entries = jobs.values().iterator();
        while (entries.hasNext()) {
            Job job = entries.next();
            if (job.expires != null && !job.expires.isAfter(now)) {
                if (job.directory != null) {
                    MissionController.removeStaging(job.directory);
                }
                keys.remove(job.key);
                entries.remove();
            }
        }
    }

    private Job find(String owner, UUID id) throws JobException {
        requireOwner(owner);
        Job job = jobs.get(id);
        if (job == null || !job.key.owner().equals(owner)) {
            throw new JobException(404, "Mission job not found.");
        }
        return job;
    }

    private static void requireOwner(String owner) throws JobException {
        if (owner == null || !OWNER.matcher(owner).matches()) {
            throw new JobException(400, "A valid job owner is required.");
        }
    }

    private static byte[] digest(byte[] body) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(body);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is unavailable", e);
        }
    }

    @Override
    public void close() throws IOException, InterruptedException {
        synchronized (this) {
            closed = true;
            jobs.values().forEach(this::cancel);
            workers.shutdown();
        }
        if (!workers.awaitTermination(10, TimeUnit.SECONDS)) {
            throw new IOException("Workers did not stop before shutdown.");
        }
        MissionController.removeStaging(spool);
    }

    private final class Job implements Runnable {
        private final UUID id = UUID.randomUUID();
        private final OwnerKey key;
        private final byte[] hash;
        private byte[] body;
        private State state = State.QUEUED;
        private Path directory;
        private Thread thread;
        private Instant expires;
        private MissionController.Receipt receipt;
        private String failure;

        Job(OwnerKey key, byte[] hash, byte[] body) {
            this.key = key;
            this.hash = hash;
            this.body = body;
        }

        Snapshot snapshot() {
            return new Snapshot(id, state, receipt, failure);
        }

        void finish(State terminal) {
            state = terminal;
            thread = null;
            expires = clock.instant().plus(retention);
        }

        @Override
        public void run() {
            byte[] planBytes;
            synchronized (MissionJobs.this) {
                if (state != State.QUEUED) {
                    return;
                }
                state = State.RUNNING;
                thread = Thread.currentThread();
                planBytes = body;
                body = null;
            }
            Path staging = null;
            MissionController.Receipt result = null;
            String failureMessage = null;
            try {
                staging = Files.createTempDirectory(spool, "run-");
                Path plan = Files.write(staging.resolve("plan.json"), planBytes);
                result = worker.run(plan, staging.resolve("telemetry.csv"));
                Files.delete(plan);
            } catch (MissionController.WorkerFailure e) {
                failureMessage = e.status() == 124 ? "The simulation exceeded its deadline."
                        : "The simulation rejected the mission plan.";
            } catch (IOException | RuntimeException e) {
                failureMessage = "The simulation could not produce valid telemetry.";
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                failureMessage = "The simulation was interrupted.";
            }
            synchronized (MissionJobs.this) {
                if (state == State.RUNNING && failureMessage == null && result != null) {
                    directory = staging;
                    receipt = result;
                    finish(State.COMPLETE);
                    return;
                }
            }
            Path cleanupDirectory = null;
            if (staging != null) {
                try {
                    MissionController.removeStaging(staging);
                } catch (IOException e) {
                    // Retain the private directory for the expiry sweep to retry cleanup.
                    cleanupDirectory = staging;
                }
            }
            synchronized (MissionJobs.this) {
                directory = cleanupDirectory;
                if (state == State.CANCELLING) {
                    finish(State.CANCELLED);
                } else {
                    failure = failureMessage == null ? "The simulation could not produce valid telemetry." : failureMessage;
                    finish(State.FAILED);
                }
            }
        }
    }
}
