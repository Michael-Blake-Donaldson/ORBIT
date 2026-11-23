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
import java.util.Comparator;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.List;
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
    private static final int MAX_ACTIVE_PER_OWNER = 4;
    private static final int MAX_RETAINED_PER_OWNER = 16;
    private static final int MAX_SAVED_PER_OWNER = 8;
    private static final Pattern KEY = Pattern.compile("[A-Za-z0-9_-]{16,80}");
    private static final Pattern OWNER = Pattern.compile("[A-Za-z0-9_-]{1,128}");

    enum State {
        QUEUED, RUNNING, CANCELLING, COMPLETE, FAILED, CANCELLED;

        boolean terminal() {
            return this == COMPLETE || this == FAILED || this == CANCELLED;
        }
    }

    record Snapshot(UUID id, State state, MissionController.Receipt receipt, String failure,
                    Instant savedAt, String planDigest, String telemetryDigest, String engine) {}

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

    private static final System.Logger LOG = System.getLogger(MissionJobs.class.getName());
    private final MissionJobStore store;
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
        this.retention = retention;
        this.clock = clock;
        this.worker = worker;
        this.maxEntries = maxEntries;
        workers = new ThreadPoolExecutor(parallelism, parallelism, 0, TimeUnit.SECONDS,
                new ArrayBlockingQueue<>(queueSize));
        store = new MissionJobStore(spool);
        try {
            restore();
        } catch (IOException | RuntimeException e) {
            try {
                store.close();
            } catch (IOException cleanup) {
                e.addSuppressed(cleanup);
            }
            throw e;
        }
    }

    private void restore() throws IOException {
        for (var record : store.load(maxEntries)) {
            OwnerKey key = new OwnerKey(record.owner(), record.key());
            Job job = new Job(record.id(), key, record.digest(), null);
            job.state = record.state();
            job.expires = record.expires();
            job.failure = record.failure();
            job.telemetryDigest = record.telemetryDigest();
            job.savedAt = record.savedAt();
            job.engine = record.engine();
            if (job.state == State.COMPLETE) {
                try {
                    if (job.telemetryDigest != null) {
                        job.checkedTelemetry();
                    }
                    job.receipt = MissionController.readReceipt(
                            store.telemetry(job.id), MAX_RESULT_ROWS, MAX_RESULT_BYTES);
                    if (job.telemetryDigest == null) {
                        // Earlier checkpoints validated the receipt without recording the file digest.
                        job.telemetryDigest = digest(store.readTelemetry(job.id));
                        job.checkpoint(State.COMPLETE, job.expires, null, job.telemetryDigest);
                    }
                } catch (IOException ignored) {
                    job.finish(State.FAILED, null, "Stored telemetry could not be recovered.");
                    Files.deleteIfExists(store.telemetry(job.id));
                }
            } else if (!job.state.terminal()) {
                if (job.state == State.CANCELLING) {
                    job.finish(State.CANCELLED, null, null);
                } else {
                    job.finish(State.FAILED, null, "The job was interrupted by a service restart.");
                }
            }
            if (keys.put(key, job) != null) {
                throw new IOException("Job checkpoints contain duplicate submission keys.");
            }
            jobs.put(job.id, job);
        }
        expire();
    }

    synchronized Snapshot submit(String owner, String key, byte[] plan) throws JobException, IOException {
        requireOwner(owner);
        if (!validKey(key)) {
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
            if (existing.isExpired(clock.instant())) {
                throw new JobException(503, "The previous job is awaiting storage cleanup.");
            }
            if (!Arrays.equals(existing.hash, hash)) {
                throw new JobException(409, "This idempotency key is already bound to a different plan.");
            }
            return existing.snapshot();
        }
        if (jobs.size() >= maxEntries) {
            throw new JobException(503, "The job store is at capacity. Try again later.");
        }
        checkOwnerCapacity(owner);

        Job job = new Job(UUID.randomUUID(), ownerKey, hash, plan.clone());
        store.create(job.id);
        try {
            job.checkpoint(State.QUEUED, null, null, null);
            workers.execute(job);
        } catch (RejectedExecutionException ignored) {
            store.delete(job.id);
            throw new JobException(503, "The mission queue is full. Try again shortly.");
        } catch (IOException e) {
            try {
                store.delete(job.id);
            } catch (IOException cleanup) {
                e.addSuppressed(cleanup);
            }
            throw e;
        }
        jobs.put(job.id, job);
        keys.put(ownerKey, job);
        return job.snapshot();
    }

    synchronized Snapshot status(String owner, UUID id) throws JobException {
        return find(owner, id).snapshot();
    }

    synchronized List<Snapshot> savedRuns(String owner) throws JobException {
        requireOwner(owner);
        return jobs.values().stream().filter(job -> job.savedAt != null && job.key.owner().equals(owner))
                .sorted(Comparator.<Job, Instant>comparing(job -> job.savedAt).reversed().thenComparing(job -> job.id))
                .map(Job::snapshot).toList();
    }

    synchronized Snapshot saveRun(String owner, UUID id) throws JobException, IOException {
        Job job = find(owner, id);
        if (job.state != State.COMPLETE) {
            throw new JobException(409, "Only completed runs can be saved.");
        }
        if (job.savedAt == null) {
            if (savedRuns(owner).size() >= MAX_SAVED_PER_OWNER) {
                throw new JobException(429, "Your saved run limit is full. Delete a saved run to make room.");
            }
            if (jobs.values().stream().filter(candidate -> candidate.savedAt != null).count() >= Math.max(1, maxEntries / 2)) {
                throw new JobException(503, "The saved run store is at capacity.");
            }
            job.checkedPlan();
            job.checkedTelemetry();
            Instant now = clock.instant();
            job.persist(job.state, job.expires, null, job.telemetryDigest, now);
            job.savedAt = now;
        }
        return job.snapshot();
    }

    synchronized void deleteSavedRun(String owner, UUID id) throws JobException, IOException {
        Job job = findSaved(owner, id);
        Instant now = clock.instant();
        job.persist(job.state, now, null, job.telemetryDigest, null);
        job.savedAt = null;
        job.expires = now;
        expire();
    }

    synchronized byte[] savedPlan(String owner, UUID id) throws JobException {
        return findSaved(owner, id).checkedPlan();
    }

    private Job findSaved(String owner, UUID id) throws JobException {
        Job job = find(owner, id);
        if (job.savedAt == null) {
            throw new JobException(404, "Saved run not found.");
        }
        return job;
    }

    synchronized Snapshot cancel(String owner, UUID id) throws JobException, IOException {
        Job job = find(owner, id);
        cancel(job);
        return job.snapshot();
    }

    private void cancel(Job job) throws IOException {
        if (job.state == State.QUEUED) {
            job.finish(State.CANCELLED, null, null);
            workers.remove(job);
            job.body = null;
        } else if (job.state == State.RUNNING) {
            job.checkpoint(State.CANCELLING, null, null, null);
            job.state = State.CANCELLING;
            job.thread.interrupt();
        }
    }

    synchronized byte[] result(String owner, UUID id) throws JobException, IOException {
        Job job = find(owner, id);
        if (job.state != State.COMPLETE) {
            throw new JobException(409, "Telemetry is available only for successfully completed jobs.");
        }
        try {
            return job.checkedTelemetry();
        } catch (IOException ignored) {
            job.finish(State.FAILED, null, "Stored telemetry failed its integrity check.");
            throw new JobException(409, "Stored telemetry failed its integrity check.");
        }
    }

    private void checkOwnerCapacity(String owner) throws JobException {
        int retained = 0;
        int active = 0;
        for (Job job : jobs.values()) {
            if (job.key.owner().equals(owner)) {
                retained++;
                if (!job.state.terminal()) {
                    active++;
                }
            }
        }
        if (active >= MAX_ACTIVE_PER_OWNER) {
            throw new JobException(429, "You already have four active mission jobs.");
        }
        if (retained >= MAX_RETAINED_PER_OWNER) {
            throw new JobException(429, "Your retained job limit is full. Try again after older jobs expire.");
        }
    }

    synchronized void expire() {
        Instant now = clock.instant();
        var entries = jobs.values().iterator();
        while (entries.hasNext()) {
            Job job = entries.next();
            if (!job.isExpired(now)) {
                continue;
            }
            try {
                store.delete(job.id);
            } catch (IOException ignored) {
                if (!job.cleanupFailed) {
                    LOG.log(System.Logger.Level.WARNING, "Could not remove an expired mission job. Cleanup will retry.");
                    job.cleanupFailed = true;
                }
                continue;
            }
            keys.remove(job.key);
            entries.remove();
        }
    }

    private Job find(String owner, UUID id) throws JobException {
        requireOwner(owner);
        Job job = jobs.get(id);
        if (job == null || !job.key.owner().equals(owner) || job.isExpired(clock.instant())) {
            throw new JobException(404, "Mission job not found.");
        }
        return job;
    }

    private static void requireOwner(String owner) throws JobException {
        if (!validOwner(owner)) {
            throw new JobException(400, "A valid job owner is required.");
        }
    }

    static boolean validOwner(String owner) {
        return owner != null && OWNER.matcher(owner).matches();
    }

    static boolean validKey(String key) {
        return key != null && KEY.matcher(key).matches();
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
            for (Job job : jobs.values()) {
                try {
                    cancel(job);
                } catch (IOException ignored) {
                    LOG.log(System.Logger.Level.WARNING, "Could not checkpoint a job during shutdown.");
                    if (job.state == State.RUNNING) {
                        job.state = State.CANCELLING;
                        job.thread.interrupt();
                    } else if (job.state == State.QUEUED) {
                        workers.remove(job);
                        job.state = State.CANCELLED;
                        job.body = null;
                    }
                }
            }
            workers.shutdown();
        }
        try {
            if (!workers.awaitTermination(10, TimeUnit.SECONDS)) {
                throw new IOException("Workers did not stop before shutdown.");
            }
        } finally {
            store.close();
        }
    }

    private final class Job implements Runnable {
        private final UUID id;
        private final OwnerKey key;
        private final byte[] hash;
        private byte[] body;
        private State state = State.QUEUED;
        private Thread thread;
        private Instant expires;
        private boolean cleanupFailed;
        private MissionController.Receipt receipt;
        private String failure;
        private byte[] telemetryDigest;
        private Instant savedAt;
        private String engine;

        Job(UUID id, OwnerKey key, byte[] hash, byte[] body) {
            this.id = id;
            this.key = key;
            this.hash = hash;
            this.body = body;
        }

        Snapshot snapshot() {
            return new Snapshot(id, state, receipt, failure, savedAt,
                    savedAt == null ? null : HexFormat.of().formatHex(hash),
                    savedAt == null ? null : HexFormat.of().formatHex(telemetryDigest),
                    state == State.COMPLETE ? engine : null);
        }

        boolean isExpired(Instant now) {
            return savedAt == null && expires != null && !expires.isAfter(now);
        }

        void checkpoint(State next, Instant expiration, String message, byte[] artifact) throws IOException {
            persist(next, expiration, message, artifact, savedAt);
        }

        void persist(State next, Instant expiration, String message, byte[] artifact, Instant saved) throws IOException {
            store.save(new MissionJobStore.Entry(id, key.owner(), key.key(), hash, next, expiration, message, artifact,
                    saved, next == State.COMPLETE ? engine : null));
        }

        void finish(State terminal, MissionController.Receipt result, String message) throws IOException {
            Instant expiration = clock.instant().plus(retention);
            byte[] artifact = terminal == State.COMPLETE ? digest(store.readTelemetry(id)) : null;
            engine = result == null ? null : result.engine();
            persist(terminal, expiration, message, artifact, terminal == State.COMPLETE ? savedAt : null);
            state = terminal;
            thread = null;
            expires = expiration;
            receipt = result;
            failure = message;
            telemetryDigest = artifact;
            if (terminal != State.COMPLETE) {
                savedAt = null;
            }
        }

        byte[] checkedPlan() throws JobException {
            try {
                byte[] plan = store.readPlan(id);
                if (plan.length <= MAX_PLAN_BYTES && MessageDigest.isEqual(hash, digest(plan))) {
                    return plan;
                }
            } catch (IOException ignored) {
                // Jobs created before plan retention may lack the original bytes.
            }
            throw new JobException(409, "The original mission plan is unavailable or failed its integrity check.");
        }

        byte[] checkedTelemetry() throws IOException {
            byte[] data = store.readTelemetry(id);
            if (!MessageDigest.isEqual(telemetryDigest, digest(data))) {
                throw new IOException("Stored telemetry did not match the completed job digest.");
            }
            return data;
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
                staging = Files.createTempDirectory(store.directory(id), "work-");
                Path plan = Files.write(staging.resolve("plan.json"), planBytes);
                result = worker.run(plan, staging.resolve("telemetry.csv"));
            } catch (MissionController.WorkerFailure e) {
                failureMessage = switch (e.status()) {
                    case 124 -> "The simulation exceeded its deadline.";
                    case MissionController.ROW_LIMIT_EXIT ->
                            "Telemetry exceeded " + MAX_RESULT_ROWS + " rows including detected events. "
                                    + "Increase the sample interval.";
                    default -> "The simulation rejected the mission plan.";
                };
            } catch (IOException | RuntimeException e) {
                failureMessage = "The simulation could not produce valid telemetry.";
            } catch (InterruptedException e) {
                failureMessage = "The simulation was interrupted.";
            }
            synchronized (MissionJobs.this) {
                // Cancellation may leave an interrupt flag set, which would abort checkpoint I/O.
                boolean interrupted = Thread.interrupted();
                try {
                    State terminal;
                    String message = null;
                    if (state == State.CANCELLING) {
                        terminal = State.CANCELLED;
                    } else if (failureMessage == null && result != null && staging != null) {
                        store.publish(id, staging.resolve("plan.json"), staging.resolve("telemetry.csv"));
                        terminal = State.COMPLETE;
                    } else {
                        terminal = State.FAILED;
                        message = failureMessage == null ? "The simulation could not produce valid telemetry."
                                : failureMessage;
                    }
                    if (staging != null) {
                        MissionController.removeStaging(staging);
                    }
                    finish(terminal, terminal == State.COMPLETE ? result : null, message);
                } catch (IOException ignored) {
                    // An uncommitted result must never become visible, even when storage fails.
                    LOG.log(System.Logger.Level.WARNING, "Could not finalize a mission checkpoint.");
                    state = State.FAILED;
                    thread = null;
                    expires = clock.instant().plus(retention);
                    failure = "The job store could not finalize the simulation.";
                } finally {
                    if (interrupted) {
                        Thread.currentThread().interrupt();
                    }
                }
            }
        }
    }
}
