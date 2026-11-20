package orbit.viewer;

import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/** Read ASCII analysis records with limits that also apply if a file grows during the read. */
final class BoundedCsv implements AutoCloseable {
    static final int MAX_ROWS = 100_000;
    private static final int MAX_BYTES = 64 * 1024 * 1024;
    private static final int MAX_ROW_BYTES = 1024;
    private final byte[] row = new byte[MAX_ROW_BYTES];
    private final InputStream input;
    private int bytesRead;

    BoundedCsv(Path path) throws IOException {
        if (!Files.isRegularFile(path)) {
            throw new IOException("The CSV path must point to a readable regular file.");
        }
        if (Files.size(path) > MAX_BYTES) {
            throw new IOException("The CSV exceeds the 64 MiB limit.");
        }
        input = new BufferedInputStream(Files.newInputStream(path));
    }

    String readLine() throws IOException {
        if (Thread.currentThread().isInterrupted()) {
            throw new IOException("CSV loading was interrupted.");
        }
        int count = 0;
        int next;
        while ((next = input.read()) != -1) {
            if (++bytesRead > MAX_BYTES) {
                throw new IOException("The CSV exceeds the 64 MiB limit.");
            }
            if (next == '\n') {
                break;
            }
            if (count == row.length || (count > 0 && row[count - 1] == '\r')
                    || (next < 32 && next != '\r') || next > 126) {
                throw new IOException("The CSV contains an invalid or oversized line.");
            }
            row[count++] = (byte) next;
        }
        if (next == -1 && count == 0) {
            return null;
        }
        if (count > 0 && row[count - 1] == '\r') {
            count--;
        }
        return new String(row, 0, count, StandardCharsets.US_ASCII);
    }

    static double number(String value, double minimum, double maximum) {
        double number = Double.parseDouble(value);
        if (!(number >= minimum && number <= maximum)) {
            throw new IllegalArgumentException("number out of range");
        }
        return number;
    }

    @Override
    public void close() throws IOException {
        input.close();
    }
}
