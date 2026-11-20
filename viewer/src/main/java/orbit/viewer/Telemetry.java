package orbit.viewer;

import static orbit.viewer.BoundedCsv.number;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/** Reader and elapsed-time index for the numerical engine's telemetry format. */
final class Telemetry {
    static final double EARTH_RADIUS_KM = 6378.137;
    static final String HEADER = "time_s,kind,x_km,y_km,z_km,vx_km_s,vy_km_s,vz_km_s,"
            + "altitude_km,speed_km_s,specific_energy_km2_s2,mass_kg,semi_major_axis_km,"
            + "eccentricity,inclination_deg,raan_deg,argument_of_periapsis_deg,true_anomaly_deg";
    private static final int COLUMN_COUNT = 18;

    enum Kind {
        SAMPLE("Sample"), BURN_END("Burn cutoff"), PRE_BURN("Pre-burn"), POST_BURN("Post-burn"),
        BURN_START("Burn ignition"), PERIAPSIS("Periapsis"), APOAPSIS("Apoapsis"),
        COMPLETE("Complete"), IMPACT("Impact");

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

        boolean maneuverEvent() {
            return this == POST_BURN || this == BURN_START || this == BURN_END;
        }

        boolean apsis() {
            return this == PERIAPSIS || this == APOAPSIS;
        }

        static Kind parse(String value) {
            return switch (value) {
                case "sample" -> SAMPLE;
                case "pre_burn" -> PRE_BURN;
                case "post_burn" -> POST_BURN;
                case "burn_start" -> BURN_START;
                case "burn_end" -> BURN_END;
                case "periapsis" -> PERIAPSIS;
                case "apoapsis" -> APOAPSIS;
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
        List<Sample> rows = new ArrayList<>();
        List<Integer> events = new ArrayList<>();
        double extent = EARTH_RADIUS_KM;
        double minAltitude = Double.POSITIVE_INFINITY;
        double maxAltitude = Double.NEGATIVE_INFINITY;
        try (BoundedCsv input = new BoundedCsv(path)) {
            if (!HEADER.equals(input.readLine())) {
                throw new IOException("The file is missing the ORBIT telemetry header.");
            }
            Sample previous = null;
            boolean burning = false;
            for (String line = input.readLine(); line != null; line = input.readLine()) {
                if (rows.size() == BoundedCsv.MAX_ROWS) {
                    throw new IOException("Telemetry exceeds the 100,000-row limit.");
                }
                Sample row = parse(line, rows.size() + 2);
                checkOrder(previous, row, burning, rows.size() + 2);
                if (row.kind().maneuverEvent()) {
                    burning = row.kind() == Kind.BURN_START;
                }
                if (row.kind().maneuverEvent() || row.kind().apsis() || row.kind().terminal()) {
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

    private static Sample parse(String line, int lineNumber) throws IOException {
        String[] fields = line.split(",", -1);
        if (fields.length != COLUMN_COUNT) {
            throw new IOException("Line " + lineNumber + " must have " + COLUMN_COUNT + " columns.");
        }
        try {
            double time = number(fields[0], 0, 2_592_000);
            Kind kind = Kind.parse(fields[1]);
            double x = number(fields[2], -1e12, 1e12);
            double y = number(fields[3], -1e12, 1e12);
            double z = number(fields[4], -1e12, 1e12);
            for (int i = 5; i <= 7; i++) {
                number(fields[i], -1e5, 1e5);
            }
            double altitude = number(fields[8], -EARTH_RADIUS_KM, 2e12);
            double speed = number(fields[9], 0, 2e5);
            double energy = number(fields[10], -1e12, 1e12);
            double mass = optional(fields[11], 0, 1e8);
            double axis = optional(fields[12], -1e23, 1e23);
            double eccentricity = number(fields[13], 0, 1e17);
            double inclination = optional(fields[14], 0, 180);
            double raan = optional(fields[15], 0, 360);
            optional(fields[16], 0, 360);
            optional(fields[17], 0, 360);
            return new Sample(time, kind, x, y, z, altitude, speed, energy, mass, axis, eccentricity, inclination, raan);
        } catch (IllegalArgumentException exception) {
            throw new IOException("Line " + lineNumber + " contains an invalid telemetry value.", exception);
        }
    }

    private static void checkOrder(Sample previous, Sample row, boolean burning, int lineNumber) throws IOException {
        Kind kind = row.kind();
        boolean valid;
        if (previous == null) {
            valid = row.time() == 0 && (kind == Kind.SAMPLE || kind == Kind.PRE_BURN || kind == Kind.BURN_START);
        } else if (previous.kind() == Kind.PRE_BURN) {
            valid = kind == Kind.POST_BURN && row.time() == previous.time();
        } else {
            valid = !previous.kind().terminal() && kind != Kind.POST_BURN
                    && (row.time() > previous.time()
                        || (row.time() == previous.time() && sameTimeAllowed(previous.kind(), kind)));
        }
        boolean allowedDuringThrust = kind == Kind.SAMPLE || kind == Kind.BURN_END || kind == Kind.IMPACT;
        if (!valid || (burning && !allowedDuringThrust) || (!burning && kind == Kind.BURN_END)) {
            throw new IOException("Line " + lineNumber + " has an unexpected timestamp or event order.");
        }
    }

    private static boolean sameTimeAllowed(Kind previous, Kind next) {
        return next == Kind.IMPACT || switch (previous) {
            case BURN_END -> next == Kind.PRE_BURN || next == Kind.BURN_START || next == Kind.COMPLETE;
            case POST_BURN -> next == Kind.BURN_START || next == Kind.COMPLETE;
            default -> false;
        };
    }

    private static double optional(String value, double minimum, double maximum) {
        return value.isEmpty() ? Double.NaN : number(value, minimum, maximum);
    }
}
