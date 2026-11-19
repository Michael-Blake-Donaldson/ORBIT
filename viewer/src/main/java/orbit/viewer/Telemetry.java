package orbit.viewer;

import java.io.BufferedInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/** Reader and elapsed-time index for the numerical engine's telemetry format. */
final class Telemetry {
    static final double EARTH_RADIUS_KM = 6378.137;
    static final String HEADER = "time_s,kind,x_km,y_km,z_km,vx_km_s,vy_km_s,vz_km_s,"
            + "altitude_km,speed_km_s,specific_energy_km2_s2,mass_kg,semi_major_axis_km,"
            + "eccentricity,inclination_deg,raan_deg,argument_of_periapsis_deg,true_anomaly_deg";
    private static final int MAX_BYTES = 64 * 1024 * 1024;
    private static final int MAX_ROWS = 100_000;
    private static final int MAX_ROW_BYTES = 1024;
    private static final int COLUMN_COUNT = 18;

    enum Kind {
        SAMPLE("Sample"), PRE_BURN("Pre-burn"), POST_BURN("Post-burn"), COMPLETE("Complete"), IMPACT("Impact");

        private final String label;

        Kind(String label) {
            this.label = label;
        }

        String label() {
            return label;
        }

        boolean terminal() {
            return this == COMPLETE || this == IMPACT;
        }

        static Kind parse(String value) {
            return switch (value) {
                case "sample" -> SAMPLE;
                case "pre_burn" -> PRE_BURN;
                case "post_burn" -> POST_BURN;
                case "complete" -> COMPLETE;
                case "impact" -> IMPACT;
                default -> throw new IllegalArgumentException("unknown row kind");
            };
        }
    }

    record Sample(
            double time, Kind kind, double x, double y, double z,
            double altitude, double speed, double energy, double mass,
            double axis, double eccentricity, double inclination, double raan
    ) {}

    record Flight(
            List<Sample> rows, List<Integer> events, double extent, double minAltitude, double maxAltitude
    ) {
        Flight {
            rows = List.copyOf(rows);
            events = List.copyOf(events);
        }

        Sample last() {
            return rows.get(rows.size() - 1);
        }

        // Use the last row at a shared timestamp so time scrubbing shows the post-burn state.
        int rowAt(double elapsed) {
            int first = 0;
            int end = rows.size();
            while (first < end) {
                int middle = first + (end - first) / 2;
                if (rows.get(middle).time() <= elapsed) {
                    first = middle + 1;
                } else {
                    end = middle;
                }
            }
            return Math.max(0, first - 1);
        }
    }

    private Telemetry() {}

    static Flight load(Path path) throws IOException {
        if (!Files.isRegularFile(path)) {
            throw new IOException("The telemetry path does not point to a readable regular file.");
        }
        if (Files.size(path) > MAX_BYTES) {
            throw new IOException("Telemetry exceeds the 64 MiB limit.");
        }
        List<Sample> rows = new ArrayList<>();
        List<Integer> events = new ArrayList<>();
        double extent = EARTH_RADIUS_KM;
        double minAltitude = Double.POSITIVE_INFINITY;
        double maxAltitude = Double.NEGATIVE_INFINITY;
        byte[] buffer = new byte[MAX_ROW_BYTES];
        try (InputStream input = new BufferedInputStream(Files.newInputStream(path))) {
            if (!HEADER.equals(readLine(input, buffer))) {
                throw new IOException("The file is missing the ORBIT telemetry header.");
            }
            Sample previous = null;
            for (String line = readLine(input, buffer); line != null; line = readLine(input, buffer)) {
                if (Thread.currentThread().isInterrupted()) {
                    throw new IOException("Telemetry loading was interrupted.");
                }
                if (rows.size() == MAX_ROWS) {
                    throw new IOException("Telemetry exceeds the 100,000-row limit.");
                }
                Sample row = parse(line, rows.size() + 2);
                checkOrder(previous, row, rows.size() + 2);
                if (row.kind() == Kind.POST_BURN || row.kind().terminal()) {
                    events.add(rows.size());
                }
                rows.add(row);
                extent = Math.max(extent, Math.max(Math.abs(row.x()), Math.max(Math.abs(row.y()), Math.abs(row.z()))));
                minAltitude = Math.min(minAltitude, row.altitude());
                maxAltitude = Math.max(maxAltitude, row.altitude());
                previous = row;
            }
            if (previous == null || !previous.kind().terminal()) {
                throw new IOException("Telemetry is missing the final complete or impact row.");
            }
        }
        return new Flight(rows, events, extent, minAltitude, maxAltitude);
    }

    private static String readLine(InputStream input, byte[] buffer) throws IOException {
        int count = 0;
        int next;
        while ((next = input.read()) != -1 && next != '\n') {
            if (count == buffer.length || (next < 32 && next != '\r') || next > 126) {
                throw new IOException("Telemetry contains an invalid or oversized line.");
            }
            buffer[count++] = (byte) next;
        }
        if (next == -1 && count == 0) {
            return null;
        }
        if (count > 0 && buffer[count - 1] == '\r') {
            count--;
        }
        return new String(buffer, 0, count, StandardCharsets.US_ASCII);
    }

    private static Sample parse(String line, int lineNumber) throws IOException {
        String[] fields = line.split(",", -1);
        if (fields.length != COLUMN_COUNT) {
            throw new IOException("Line " + lineNumber + " must have " + COLUMN_COUNT + " columns.");
        }
        try {
            double time = bounded(fields[0], 0, 2_592_000);
            Kind kind = Kind.parse(fields[1]);
            double x = bounded(fields[2], -1e12, 1e12);
            double y = bounded(fields[3], -1e12, 1e12);
            double z = bounded(fields[4], -1e12, 1e12);
            for (int i = 5; i <= 7; i++) {
                bounded(fields[i], -1e5, 1e5);
            }
            double altitude = bounded(fields[8], -EARTH_RADIUS_KM, 2e12);
            double speed = bounded(fields[9], 0, 2e5);
            double energy = bounded(fields[10], -1e12, 1e12);
            double mass = optional(fields[11], 0, 1e8);
            double axis = optional(fields[12], -1e23, 1e23);
            double eccentricity = bounded(fields[13], 0, 1e17);
            double inclination = optional(fields[14], 0, 180);
            double raan = optional(fields[15], 0, 360);
            optional(fields[16], 0, 360);
            optional(fields[17], 0, 360);
            return new Sample(time, kind, x, y, z, altitude, speed, energy, mass, axis, eccentricity, inclination, raan);
        } catch (IllegalArgumentException exception) {
            throw new IOException("Line " + lineNumber + " contains an invalid telemetry value.", exception);
        }
    }

    private static void checkOrder(Sample previous, Sample row, int lineNumber) throws IOException {
        boolean valid;
        if (previous == null) {
            valid = row.time() == 0 && (row.kind() == Kind.SAMPLE || row.kind() == Kind.PRE_BURN);
        } else if (previous.kind() == Kind.PRE_BURN) {
            valid = row.kind() == Kind.POST_BURN && row.time() == previous.time();
        } else {
            boolean sameTime = row.time() == previous.time();
            valid = !previous.kind().terminal() && row.kind() != Kind.POST_BURN
                    && (row.time() > previous.time() || (sameTime && row.kind() == Kind.IMPACT)
                        || (sameTime && previous.kind() == Kind.POST_BURN && row.kind() == Kind.COMPLETE));
        }
        if (!valid) {
            throw new IOException("Line " + lineNumber + " has an unexpected timestamp or event order.");
        }
    }

    private static double bounded(String value, double minimum, double maximum) {
        double number = Double.parseDouble(value);
        if (!(number >= minimum && number <= maximum)) {
            throw new IllegalArgumentException("number out of range");
        }
        return number;
    }

    private static double optional(String value, double minimum, double maximum) {
        return value.isEmpty() ? Double.NaN : bounded(value, minimum, maximum);
    }
}
