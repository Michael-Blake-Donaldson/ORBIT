package orbit.viewer;

import static orbit.viewer.BoundedCsv.number;

import java.io.IOException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/** The populated samples from Python's synchronized comparison grid. */
record ComparisonReport(List<ComparisonReport.Row> rows, ComparisonReport.Row peakPosition,
                        ComparisonReport.Row peakVelocity) {
    static final String HEADER = "time_s,reference_kind,candidate_kind,position_difference_km,"
            + "velocity_difference_km_s,delta_x_km,delta_y_km,delta_z_km,delta_vx_km_s,delta_vy_km_s,delta_vz_km_s,"
            + "delta_r_km,delta_t_km,delta_n_km,delta_vr_km_s,delta_vt_km_s,delta_vn_km_s";
    private static final double MAX_POSITION_DIFFERENCE = 4e12;
    private static final double MAX_VELOCITY_DIFFERENCE = 4e5;

    record Vector(double a, double b, double c) {
        double length() {
            return Math.hypot(Math.hypot(a, b), c);
        }
    }

    record Delta(Vector position, Vector velocity) {}

    record Row(double time, Telemetry.Kind referenceKind, Telemetry.Kind candidateKind,
               double positionKm, double velocityKmS, Delta inertial, Delta rtn) {}

    ComparisonReport {
        rows = List.copyOf(rows);
    }

    Row last() {
        return rows.get(rows.size() - 1);
    }

    int nearestRow(double time) {
        int first = 0;
        int end = rows.size() - 1;
        while (first < end) {
            int middle = first + (end - first) / 2;
            if (rows.get(middle).time() < time) {
                first = middle + 1;
            } else {
                end = middle;
            }
        }
        if (first > 0 && time - rows.get(first - 1).time() <= rows.get(first).time() - time) {
            return first - 1;
        }
        return first;
    }

    static ComparisonReport load(Path path) throws IOException {
        List<Row> rows = new ArrayList<>();
        Row peakPosition = null;
        Row peakVelocity = null;
        double previousTime = -1;
        try (BoundedCsv csv = new BoundedCsv(path)) {
            if (!HEADER.equals(csv.readLine())) {
                throw new IOException("The file is missing the ORBIT comparison header.");
            }
            for (String line = csv.readLine(); line != null; line = csv.readLine()) {
                if (rows.size() == BoundedCsv.MAX_ROWS) {
                    throw new IOException("Comparison exceeds the 100,000-row limit.");
                }
                int lineNumber = rows.size() + 2;
                Row row = parse(line, lineNumber);
                if ((rows.isEmpty() && row.time() != 0) || row.time() <= previousTime) {
                    throw new IOException("Line " + lineNumber + " has an unexpected comparison timestamp.");
                }
                rows.add(row);
                previousTime = row.time();
                if (peakPosition == null || row.positionKm() > peakPosition.positionKm()) {
                    peakPosition = row;
                }
                if (peakVelocity == null || row.velocityKmS() > peakVelocity.velocityKmS()) {
                    peakVelocity = row;
                }
            }
        }
        if (rows.isEmpty()) {
            throw new IOException("The comparison has no samples.");
        }
        return new ComparisonReport(rows, peakPosition, peakVelocity);
    }

    private static Row parse(String line, int lineNumber) throws IOException {
        String[] fields = line.split(",", -1);
        if (fields.length != 17) {
            throw new IOException("Line " + lineNumber + " must have 17 comparison columns.");
        }
        try {
            double time = number(fields[0], 0, 2_592_000);
            Telemetry.Kind reference = Telemetry.Kind.parse(fields[1]);
            Telemetry.Kind candidate = Telemetry.Kind.parse(fields[2]);
            double positionKm = number(fields[3], 0, MAX_POSITION_DIFFERENCE);
            double velocityKmS = number(fields[4], 0, MAX_VELOCITY_DIFFERENCE);
            Delta inertial = parseDelta(fields, 5);
            checkMagnitude(positionKm, inertial.position().length());
            checkMagnitude(velocityKmS, inertial.velocity().length());
            boolean missingRtn = true;
            for (int index = 11; index < fields.length; index++) {
                missingRtn &= fields[index].isEmpty();
            }
            Delta rtn = missingRtn ? null : parseDelta(fields, 11);
            return new Row(time, reference, candidate, positionKm, velocityKmS, inertial, rtn);
        } catch (IllegalArgumentException exception) {
            throw new IOException("Line " + lineNumber + " contains an invalid comparison value.", exception);
        }
    }

    private static Delta parseDelta(String[] fields, int start) {
        return new Delta(parseVector(fields, start, MAX_POSITION_DIFFERENCE),
                parseVector(fields, start + 3, MAX_VELOCITY_DIFFERENCE));
    }

    private static Vector parseVector(String[] fields, int start, double bound) {
        return new Vector(number(fields[start], -bound, bound), number(fields[start + 1], -bound, bound),
                number(fields[start + 2], -bound, bound));
    }

    private static void checkMagnitude(double magnitude, double norm) {
        double tolerance = Math.max(4 * Math.ulp(norm), 1e-12 * norm);
        if (Math.abs(magnitude - norm) > tolerance) {
            throw new IllegalArgumentException("difference must match its inertial vector");
        }
    }
}
