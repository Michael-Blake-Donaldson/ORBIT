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
    private static final byte[] CSV = "time_s,kind,x_km\n0,complete,7000\n".getBytes(StandardCharsets.UTF_8);
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
            var job = jobs.submit(FIRST, PLAN);
            assertEquals(job.id(), jobs.submit(FIRST, PLAN).id());
            assertEquals(409, assertThrows(MissionJobs.JobException.class,
                    () -> jobs.submit(FIRST, "{ }".getBytes(StandardCharsets.UTF_8))).status());
            assertEquals(MissionJobs.State.COMPLETE, terminal(jobs, job.id()).state());
            assertArrayEquals(CSV, jobs.result(job.id()));
            assertEquals(job.id(), jobs.submit(FIRST, PLAN).id());
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
            var running = jobs.submit(FIRST, PLAN);
            assertTrue(started.await(5, TimeUnit.SECONDS));
            var queued = jobs.submit(SECOND, PLAN);
            assertEquals(MissionJobs.State.QUEUED, queued.state());
            assertEquals(503, assertThrows(MissionJobs.JobException.class, () -> jobs.submit(THIRD, PLAN)).status());
            assertEquals(MissionJobs.State.CANCELLED, jobs.cancel(queued.id()).state());
            assertEquals(MissionJobs.State.CANCELLING, jobs.cancel(running.id()).state());
            assertEquals(MissionJobs.State.CANCELLED, terminal(jobs, running.id()).state());
            assertEquals(409, assertThrows(MissionJobs.JobException.class, () -> jobs.result(running.id())).status());
            try (var paths = Files.walk(spool)) {
                assertTrue(paths.noneMatch(Files::isRegularFile));
            }
        }
    }

    @Test
    void failedWorkersExposeAStatusWithoutPartialTelemetryOrPrivateDetails() throws Exception {
        try (MissionJobs jobs = service((plan, telemetry) -> {
            Files.writeString(telemetry, "partial");
            throw new IOException("/private/directory");
        })) {
            var failed = terminal(jobs, jobs.submit(FIRST, PLAN).id());
            assertEquals(MissionJobs.State.FAILED, failed.state());
            assertEquals("The simulation could not produce valid telemetry.", failed.failure());
            assertEquals(409, assertThrows(MissionJobs.JobException.class, () -> jobs.result(failed.id())).status());
        }
    }

    @Test
    void terminalJobsExpireWithTheirArtifactsAndReleaseTheIdempotencyKey() throws Exception {
        AtomicReference<Instant> time = new AtomicReference<>(Instant.EPOCH);
        try (var jobs = new MissionJobs(spool, 1, 1, 1, Duration.ofMinutes(1), clock(time, ZoneOffset.UTC), (plan, telemetry) -> {
            Files.write(telemetry, CSV);
            return new MissionController.Receipt("complete", 0, 1);
        })) {
            var completed = terminal(jobs, jobs.submit(FIRST, PLAN).id());
            assertArrayEquals(CSV, jobs.result(completed.id()));
            time.set(Instant.EPOCH.plus(Duration.ofMinutes(1)));
            jobs.expire();
            assertEquals(404, assertThrows(MissionJobs.JobException.class,
                    () -> jobs.result(completed.id())).status());
            try (var paths = Files.walk(spool)) {
                assertTrue(paths.noneMatch(Files::isRegularFile));
            }
            assertEquals(MissionJobs.State.COMPLETE, terminal(jobs, jobs.submit(FIRST, PLAN).id()).state());
        }
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
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        MissionJobs.Snapshot current = jobs.status(id);
        while (!current.state().terminal() && System.nanoTime() < deadline) {
            Thread.sleep(10);
            current = jobs.status(id);
        }
        assertTrue(current.state().terminal());
        return current;
    }
}
