package orbit.controller;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class MissionJobsTest {
    private static final byte[] PLAN = "{}".getBytes(StandardCharsets.UTF_8);
    private static final byte[] CSV = MissionControllerTest.COMPLETE_CSV.getBytes(StandardCharsets.UTF_8);
    private static final String OWNER = "test-owner";
    private static final String FIRST = "example-run-key-01";
    private static final String SECOND = "example-run-key-02";
    private static final String THIRD = "example-run-key-03";

    @TempDir
    Path spool;

    @Test
    void repeatedSubmissionRunsOnceAndAChangedPlanConflicts() throws Exception {
        var calls = new AtomicInteger();
        try (MissionJobs jobs = service((plan, telemetry) -> {
            calls.incrementAndGet();
            Files.write(telemetry, CSV);
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            var job = jobs.submit(OWNER, FIRST, PLAN);
            assertEquals(job.id(), jobs.submit(OWNER, FIRST, PLAN).id());
            assertEquals(409, assertThrows(MissionJobs.JobException.class,
                    () -> jobs.submit(OWNER, FIRST, "{ }".getBytes(StandardCharsets.UTF_8))).status());
            assertEquals(MissionJobs.State.COMPLETE, terminal(jobs, job.id()).state());
            assertArrayEquals(CSV, jobs.result(OWNER, job.id()));
            assertEquals(job.id(), jobs.submit(OWNER, FIRST, PLAN).id());
            assertEquals(1, calls.get());
        }
    }

    @Test
    void queueIsBoundedAndCancellationDiscardsBothQueuedAndPartialResults() throws Exception {
        CountDownLatch started = new CountDownLatch(1);
        CountDownLatch hold = new CountDownLatch(1);
        try (MissionJobs jobs = service((plan, telemetry) -> {
            Files.writeString(telemetry, "partial");
            started.countDown();
            hold.await();
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            var running = jobs.submit(OWNER, FIRST, PLAN);
            assertTrue(started.await(5, TimeUnit.SECONDS));
            var queued = jobs.submit(OWNER, SECOND, PLAN);
            assertEquals(MissionJobs.State.QUEUED, queued.state());
            assertEquals(503, assertThrows(MissionJobs.JobException.class, () -> jobs.submit(OWNER, THIRD, PLAN)).status());
            assertEquals(MissionJobs.State.CANCELLED, jobs.cancel(OWNER, queued.id()).state());
            assertEquals(MissionJobs.State.CANCELLING, jobs.cancel(OWNER, running.id()).state());
            assertEquals(MissionJobs.State.CANCELLED, terminal(jobs, running.id()).state());
            assertEquals(409, assertThrows(MissionJobs.JobException.class, () -> jobs.result(OWNER, running.id())).status());
            try (var paths = Files.walk(spool)) {
                assertTrue(paths.noneMatch(path -> path.endsWith("plan.json") || path.endsWith("telemetry.csv")));
            }
        }
    }

    @Test
    void failedWorkersExposeAStatusWithoutPartialTelemetryOrPrivateDetails() throws Exception {
        try (MissionJobs jobs = service((plan, telemetry) -> {
            Files.writeString(telemetry, "partial");
            throw new IOException("/private/directory");
        })) {
            var failed = terminal(jobs, jobs.submit(OWNER, FIRST, PLAN).id());
            assertEquals(MissionJobs.State.FAILED, failed.state());
            assertEquals("The simulation could not produce valid telemetry.", failed.failure());
            assertEquals(409, assertThrows(MissionJobs.JobException.class, () -> jobs.result(OWNER, failed.id())).status());
        }
    }

    @Test
    void terminalJobsExpireWithTheirArtifactsAndReleaseTheIdempotencyKey() throws Exception {
        AtomicReference<Instant> time = new AtomicReference<>(Instant.EPOCH);
        try (var jobs = new MissionJobs(spool, 1, 1, 1, Duration.ofMinutes(1), clock(time, ZoneOffset.UTC), (plan, telemetry) -> {
            Files.write(telemetry, CSV);
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            var completed = terminal(jobs, jobs.submit(OWNER, FIRST, PLAN).id());
            assertArrayEquals(CSV, jobs.result(OWNER, completed.id()));
            time.set(Instant.EPOCH.plus(Duration.ofMinutes(1)));
            assertEquals(404, assertThrows(MissionJobs.JobException.class,
                    () -> jobs.status(OWNER, completed.id())).status());
            assertEquals(404, assertThrows(MissionJobs.JobException.class,
                    () -> jobs.cancel(OWNER, completed.id())).status());
            jobs.expire();
            assertEquals(404, assertThrows(MissionJobs.JobException.class,
                    () -> jobs.result(OWNER, completed.id())).status());
            try (var paths = Files.walk(spool)) {
                assertTrue(paths.noneMatch(path -> path.endsWith("job.properties") || path.endsWith("telemetry.csv")));
            }
            assertEquals(MissionJobs.State.COMPLETE, terminal(jobs, jobs.submit(OWNER, FIRST, PLAN).id()).state());
        }
    }

    @Test
    void activeOwnerLimitPreservesCapacityForOtherOwnersAndAllowsRetries() throws Exception {
        CountDownLatch hold = new CountDownLatch(1);
        try (var jobs = new MissionJobs(spool, 1, 8, 32, Duration.ofMinutes(1), Clock.systemUTC(), (plan, telemetry) -> {
            hold.await();
            return null;
        })) {
            UUID first = jobs.submit(OWNER, FIRST, PLAN).id();
            jobs.submit(OWNER, SECOND, PLAN);
            jobs.submit(OWNER, THIRD, PLAN);
            jobs.submit(OWNER, "example-run-key-04", PLAN);
            assertEquals(first, jobs.submit(OWNER, FIRST, PLAN).id());
            assertEquals(429, assertThrows(MissionJobs.JobException.class,
                    () -> jobs.submit(OWNER, "example-run-key-05", PLAN)).status());
            assertEquals(MissionJobs.State.QUEUED, jobs.submit("another-owner", FIRST, PLAN).state());
        }
    }

    @Test
    void retainedOwnerLimitDoesNotFillTheSharedStore() throws Exception {
        try (var jobs = new MissionJobs(spool, 1, 8, 32, Duration.ofMinutes(1), Clock.systemUTC(), (plan, telemetry) -> {
            Files.write(telemetry, CSV);
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            for (int index = 0; index < 16; index++) {
                assertEquals(MissionJobs.State.COMPLETE,
                        terminal(jobs, jobs.submit(OWNER, "retention-example-" + index, PLAN).id()).state());
            }
            assertEquals(429, assertThrows(MissionJobs.JobException.class, () -> jobs.submit(OWNER, FIRST, PLAN)).status());
            assertEquals(MissionJobs.State.COMPLETE,
                    terminal(jobs, "another-owner", jobs.submit("another-owner", FIRST, PLAN).id()).state());
        }
    }

    @Test
    void completedResultAndKeySurviveClosingAndReopeningTheStore() throws Exception {
        UUID id;
        try (MissionJobs jobs = service((plan, telemetry) -> {
            Files.write(telemetry, CSV);
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            id = terminal(jobs, jobs.submit(OWNER, FIRST, PLAN).id()).id();
            assertEquals(MissionJobs.State.COMPLETE, jobs.status(OWNER, id).state());
            assertThrows(IOException.class, () -> service((plan, telemetry) -> null));
        }
        try (MissionJobs reopened = service((plan, telemetry) -> {
            throw new AssertionError("retry must not rerun a recovered job");
        })) {
            assertEquals(id, reopened.submit(OWNER, FIRST, PLAN).id());
            assertEquals(MissionJobs.State.COMPLETE, reopened.status(OWNER, id).state());
            assertArrayEquals(CSV, reopened.result(OWNER, id));
            assertEquals(404, assertThrows(MissionJobs.JobException.class,
                    () -> reopened.result("other-owner", id)).status());
        }
    }

    @Test
    void interruptedJobsAndCancellationIntentRecoverWithoutExposingUncommittedTelemetry() throws Exception {
        UUID running = UUID.randomUUID();
        UUID cancelling = UUID.randomUUID();
        byte[] hash = java.security.MessageDigest.getInstance("SHA-256").digest(PLAN);
        try (var store = new MissionJobStore(spool)) {
            store.create(running);
            store.save(new MissionJobStore.Entry(
                    running, OWNER, FIRST, hash, MissionJobs.State.QUEUED, null, null, null, null));
            Files.write(store.telemetry(running), CSV);
            store.create(cancelling);
            store.save(new MissionJobStore.Entry(
                    cancelling, OWNER, SECOND, hash, MissionJobs.State.CANCELLING, null, null, null, null));
            Path interruptedWork = Files.createTempDirectory(store.directory(cancelling), "work-");
            Files.write(interruptedWork.resolve("telemetry.csv"), CSV);
        }
        try (MissionJobs reopened = service((plan, telemetry) -> null)) {
            assertEquals(MissionJobs.State.FAILED, reopened.status(OWNER, running).state());
            assertEquals(MissionJobs.State.CANCELLED, reopened.status(OWNER, cancelling).state());
            assertEquals(running, reopened.submit(OWNER, FIRST, PLAN).id());
            assertEquals(409, assertThrows(MissionJobs.JobException.class,
                    () -> reopened.result(OWNER, running)).status());
            try (var paths = Files.walk(spool)) {
                assertTrue(paths.noneMatch(path -> path.endsWith("telemetry.csv")));
            }
        }
    }

    @Test
    void tamperedTelemetryFailsBothLiveExportAndRestartRecovery() throws Exception {
        UUID live;
        UUID restarted;
        try (MissionJobs jobs = service((plan, telemetry) -> {
            Files.write(telemetry, CSV);
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            live = terminal(jobs, jobs.submit(OWNER, FIRST, PLAN).id()).id();
            restarted = terminal(jobs, jobs.submit(OWNER, SECOND, PLAN).id()).id();
            alterTelemetry(live);
            assertEquals(409, assertThrows(MissionJobs.JobException.class, () -> jobs.result(OWNER, live)).status());
            assertEquals(MissionJobs.State.FAILED, jobs.status(OWNER, live).state());
        }
        alterTelemetry(restarted);
        try (MissionJobs jobs = service((plan, telemetry) -> null)) {
            assertEquals(MissionJobs.State.FAILED, jobs.status(OWNER, restarted).state());
            assertEquals(409, assertThrows(MissionJobs.JobException.class, () -> jobs.result(OWNER, restarted)).status());
        }
    }

    @Test
    void checksumsAreBackfilledForExistingCompletedCheckpoints() throws Exception {
        UUID id;
        try (MissionJobs jobs = service((plan, telemetry) -> {
            Files.write(telemetry, CSV);
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            id = terminal(jobs, jobs.submit(OWNER, FIRST, PLAN).id()).id();
        }
        Path checkpoint = spool.resolve("orbit-job-store-v1").resolve(id.toString()).resolve("job.properties");
        String older = Files.readString(checkpoint).replaceAll("(?m)^(schema|telemetry_digest)=.*\\R", "");
        Files.writeString(checkpoint, older);
        try (MissionJobs jobs = service((plan, telemetry) -> null)) {
            assertArrayEquals(CSV, jobs.result(OWNER, id));
            assertTrue(Files.readString(checkpoint).contains("telemetry_digest="));
        }
    }

    @Test
    void savedRunsKeepTheExactPlanPastExpiryAndRestartAndSupportOwnerDeletion() throws Exception {
        var time = new AtomicReference<>(Instant.parse("2025-01-01T00:00:00Z"));
        Clock clock = clock(time, ZoneOffset.UTC);
        UUID id;
        try (var jobs = new MissionJobs(spool, 1, 1, 4, Duration.ofMinutes(1), clock, (plan, telemetry) -> {
            Files.write(telemetry, CSV);
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            id = terminal(jobs, jobs.submit(OWNER, FIRST, PLAN).id()).id();
            assertTrue(jobs.savedRuns(OWNER).isEmpty());
            assertEquals(404, assertThrows(MissionJobs.JobException.class, () -> jobs.savedPlan(OWNER, id)).status());
            assertEquals(time.get(), jobs.saveRun(OWNER, id).savedAt());
            assertEquals(404, assertThrows(MissionJobs.JobException.class,
                    () -> jobs.deleteSavedRun("other-owner", id)).status());
        }
        time.set(time.get().plusSeconds(600));
        try (var jobs = new MissionJobs(spool, 1, 1, 4, Duration.ofMinutes(1), clock, (plan, telemetry) -> null)) {
            assertEquals(id, jobs.savedRuns(OWNER).get(0).id());
            assertTrue(jobs.savedRuns("other-owner").isEmpty());
            assertArrayEquals(PLAN, jobs.savedPlan(OWNER, id));
            assertArrayEquals(CSV, jobs.result(OWNER, id));
            assertEquals(64, jobs.saveRun(OWNER, id).planDigest().length());
            jobs.deleteSavedRun(OWNER, id);
            assertTrue(jobs.savedRuns(OWNER).isEmpty());
        }
        try (var jobs = new MissionJobs(spool, 1, 1, 4, Duration.ofMinutes(1), clock, (plan, telemetry) -> null)) {
            assertEquals(404, assertThrows(MissionJobs.JobException.class, () -> jobs.status(OWNER, id)).status());
        }
    }

    private void alterTelemetry(UUID job) throws IOException {
        Path telemetry = spool.resolve("orbit-job-store-v1").resolve(job.toString()).resolve("telemetry.csv");
        Files.writeString(telemetry, MissionControllerTest.COMPLETE_CSV.replace(",7000,", ",8000,"));
    }

    private static Clock clock(AtomicReference<Instant> time, ZoneId zone) {
        return new Clock() {
            @Override
            public ZoneId getZone() {
                return zone;
            }

            @Override
            public Clock withZone(ZoneId updated) {
                return clock(time, updated);
            }

            @Override
            public Instant instant() {
                return time.get();
            }
        };
    }

    private MissionJobs service(MissionJobs.Worker worker) throws IOException {
        return new MissionJobs(spool, 1, 1, 4, Duration.ofMinutes(1), Clock.systemUTC(), worker);
    }

    private static MissionJobs.Snapshot terminal(MissionJobs jobs, UUID id) throws Exception {
        return terminal(jobs, OWNER, id);
    }

    private static MissionJobs.Snapshot terminal(MissionJobs jobs, String owner, UUID id) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        MissionJobs.Snapshot current = jobs.status(owner, id);
        while (!current.state().terminal() && System.nanoTime() < deadline) {
            Thread.sleep(10);
            current = jobs.status(owner, id);
        }
        assertTrue(current.state().terminal());
        return current;
    }
}
