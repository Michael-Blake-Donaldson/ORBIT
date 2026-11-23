package orbit.controller;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.nio.file.FileAlreadyExistsException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermission;
import java.nio.file.attribute.PosixFilePermissions;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Properties;
import java.util.Set;
import java.util.UUID;

/** Versioned local storage shared by one controller process at a time. */
final class MissionJobStore implements AutoCloseable {
    private static final int MAX_RECORD_BYTES = 4096;
    private static final String RECORD = "job.properties";
    private static final Set<PosixFilePermission> PRIVATE = PosixFilePermissions.fromString("rwx------");
    private final Path root;
    private final FileChannel lockChannel;
    private final FileLock lock;

    record Entry(UUID id, String owner, String key, byte[] digest, MissionJobs.State state,
                 Instant expires, String failure, byte[] telemetryDigest, Instant savedAt, String engine) {}

    MissionJobStore(Path spool) throws IOException {
        Path parent = spool.toRealPath();
        root = parent.resolve("orbit-job-store-v1");
        boolean posix = Files.getFileStore(parent).supportsFileAttributeView("posix");
        try {
            if (posix) {
                Files.createDirectory(root, PosixFilePermissions.asFileAttribute(PRIVATE));
            } else {
                Files.createDirectory(root);
            }
        } catch (FileAlreadyExistsException ignored) {
            if (!Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("Job store path must be a private directory.");
            }
        }
        if (posix && !PRIVATE.containsAll(Files.getPosixFilePermissions(root, LinkOption.NOFOLLOW_LINKS))) {
            throw new IOException("Job store directory must restrict permissions to its owner.");
        }
        lockChannel = FileChannel.open(root.resolve(".lock"), StandardOpenOption.CREATE, StandardOpenOption.WRITE,
                LinkOption.NOFOLLOW_LINKS);
        try {
            lock = lockChannel.tryLock();
            if (lock == null) {
                throw new IOException("Job store is already in use.");
            }
        } catch (OverlappingFileLockException e) {
            lockChannel.close();
            throw new IOException("Job store is already in use.", e);
        } catch (IOException | RuntimeException e) {
            lockChannel.close();
            throw e;
        }
    }

    Path directory(UUID id) {
        return root.resolve(id.toString());
    }

    Path telemetry(UUID id) {
        return directory(id).resolve("telemetry.csv");
    }

    void create(UUID id) throws IOException {
        Files.createDirectory(directory(id));
    }

    void delete(UUID id) throws IOException {
        MissionController.removeStaging(directory(id));
    }

    void publish(UUID id, Path plan, Path telemetry) throws IOException {
        Files.move(plan, directory(id).resolve("plan.json"), StandardCopyOption.ATOMIC_MOVE);
        Files.move(telemetry, telemetry(id), StandardCopyOption.ATOMIC_MOVE);
    }

    byte[] readPlan(UUID id) throws IOException {
        try (var input = Files.newInputStream(directory(id).resolve("plan.json"), LinkOption.NOFOLLOW_LINKS)) {
            return input.readNBytes(MissionJobs.MAX_PLAN_BYTES + 1);
        }
    }

    byte[] readTelemetry(UUID id) throws IOException {
        try (var input = Files.newInputStream(telemetry(id), LinkOption.NOFOLLOW_LINKS)) {
            byte[] data = input.readNBytes(MissionJobs.MAX_RESULT_BYTES + 1);
            if (data.length > MissionJobs.MAX_RESULT_BYTES) {
                throw new IOException("Job telemetry exceeds the result budget.");
            }
            return data;
        }
    }

    void save(Entry entry) throws IOException {
        Properties values = new Properties();
        values.setProperty("schema", "2");
        values.setProperty("owner", entry.owner());
        values.setProperty("key", entry.key());
        values.setProperty("digest", HexFormat.of().formatHex(entry.digest()));
        values.setProperty("state", entry.state().name());
        if (entry.expires() != null) {
            values.setProperty("expires", entry.expires().toString());
        }
        if (entry.failure() != null) {
            values.setProperty("failure", entry.failure());
        }
        if (entry.telemetryDigest() != null) {
            values.setProperty("telemetry_digest", HexFormat.of().formatHex(entry.telemetryDigest()));
        }
        if (entry.savedAt() != null) {
            values.setProperty("saved_at", entry.savedAt().toString());
        }
        if (entry.engine() != null) {
            values.setProperty("engine", entry.engine());
        }
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        values.store(bytes, null);
        Path job = directory(entry.id());
        Path temp = Files.createTempFile(job, "checkpoint-", ".tmp");
        try {
            try (FileChannel channel = FileChannel.open(temp, StandardOpenOption.WRITE)) {
                ByteBuffer content = ByteBuffer.wrap(bytes.toByteArray());
                while (content.hasRemaining()) {
                    channel.write(content);
                }
                channel.force(true);
            }
            Files.move(temp, job.resolve(RECORD), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        } finally {
            Files.deleteIfExists(temp);
        }
    }

    List<Entry> load(int capacity) throws IOException {
        List<Entry> entries = new ArrayList<>();
        try (var paths = Files.newDirectoryStream(root)) {
            for (Path path : paths) {
                UUID id = jobId(path.getFileName().toString());
                if (id == null) {
                    continue;
                }
                if (!Files.isDirectory(path, LinkOption.NOFOLLOW_LINKS)) {
                    throw new IOException("Invalid job store entry.");
                }
                Path record = path.resolve(RECORD);
                if (!Files.exists(record, LinkOption.NOFOLLOW_LINKS)) {
                    delete(id);
                    continue;
                }
                if (entries.size() == capacity) {
                    throw new IOException("Persisted jobs exceed the configured job capacity.");
                }
                Entry entry = loadEntry(record, id);
                cleanInterruptedFiles(path, entry.state() == MissionJobs.State.COMPLETE);
                entries.add(entry);
            }
        }
        return entries;
    }

    private static Entry loadEntry(Path path, UUID id) throws IOException {
        if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("Invalid job checkpoint.");
        }
        Properties values = new Properties();
        try (var input = Files.newInputStream(path, LinkOption.NOFOLLOW_LINKS)) {
            byte[] bytes = input.readNBytes(MAX_RECORD_BYTES + 1);
            if (bytes.length > MAX_RECORD_BYTES) {
                throw new IOException("Job checkpoint exceeds its size limit.");
            }
            values.load(new ByteArrayInputStream(bytes));
            String schema = values.getProperty("schema", "1");
            String owner = values.getProperty("owner");
            String key = values.getProperty("key");
            byte[] digest = HexFormat.of().parseHex(values.getProperty("digest", ""));
            var state = MissionJobs.State.valueOf(values.getProperty("state", ""));
            String expiry = values.getProperty("expires");
            Instant expires = expiry == null ? null : Instant.parse(expiry);
            String failure = values.getProperty("failure");
            String telemetry = values.getProperty("telemetry_digest");
            byte[] telemetryDigest = telemetry == null ? null : HexFormat.of().parseHex(telemetry);
            String saved = values.getProperty("saved_at");
            Instant savedAt = saved == null ? null : Instant.parse(saved);
            String engine = values.getProperty("engine");
            if ((!schema.equals("1") && !schema.equals("2")) || !MissionJobs.validOwner(owner)
                    || !MissionJobs.validKey(key) || digest.length != 32
                    || state.terminal() != (expires != null) || (state == MissionJobs.State.FAILED) != (failure != null)
                    || (failure != null && failure.length() > 240)
                    || (schema.equals("2") && (state == MissionJobs.State.COMPLETE) != (telemetryDigest != null))
                    || (telemetryDigest != null && telemetryDigest.length != 32)
                    || (savedAt != null && state != MissionJobs.State.COMPLETE)
                    || (engine != null && (state != MissionJobs.State.COMPLETE || !MissionController.validEngineReport(engine)))) {
                throw new IOException("Invalid job checkpoint fields.");
            }
            return new Entry(id, owner, key, digest, state, expires, failure, telemetryDigest, savedAt, engine);
        } catch (IllegalArgumentException | java.time.DateTimeException e) {
            throw new IOException("Invalid job checkpoint values.", e);
        }
    }

    private static void cleanInterruptedFiles(Path path, boolean keepTelemetry) throws IOException {
        try (var files = Files.newDirectoryStream(path)) {
            for (Path file : files) {
                String name = file.getFileName().toString();
                if (name.startsWith("work-") || name.startsWith("checkpoint-")
                        || (!keepTelemetry && (name.equals("telemetry.csv") || name.equals("plan.json")))) {
                    MissionController.removeStaging(file);
                }
            }
        }
    }

    private static UUID jobId(String name) {
        try {
            UUID id = UUID.fromString(name);
            return id.toString().equals(name) ? id : null;
        } catch (IllegalArgumentException ignored) {
            return null;
        }
    }

    @Override
    public void close() throws IOException {
        try {
            lock.release();
        } finally {
            lockChannel.close();
        }
    }
}
