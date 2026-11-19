package orbit.viewer;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class TelemetryTest {
    @TempDir
    Path directory;

    @Test
    void indexesBurnsByElapsedTimeAndKeepsBothStates() throws IOException {
        String rows = row(0, "sample") + row(8, "pre_burn") + row(8, "post_burn") + row(12, "complete");
        Telemetry.Flight flight = Telemetry.load(write(rows.replace("\n", "\r\n")));
        assertEquals(0, flight.rowAt(7.5));
        assertEquals(2, flight.rowAt(8));
        assertEquals(2, flight.rowAt(11));
        assertEquals(3, flight.rowAt(12));
        assertEquals(java.util.List.of(2, 3), flight.events());
        assertTrue(Double.isNaN(flight.last().mass()));
    }

    @Test
    void selectsCompleteRowAfterAnEndTimeBurn() throws IOException {
        Telemetry.Flight flight = Telemetry.load(write(
                row(0, "pre_burn") + row(0, "post_burn")
                        + row(10, "pre_burn") + row(10, "post_burn") + row(10, "complete")));
        assertEquals(1, flight.rowAt(0));
        assertEquals(4, flight.rowAt(10));
        assertEquals(java.util.List.of(1, 3, 4), flight.events());
    }

    @Test
    void acceptsAnImpactAtTheInitialTimestamp() throws IOException {
        Telemetry.Flight flight = Telemetry.load(write(row(0, "sample") + row(0, "impact").stripTrailing()));
        assertEquals(Telemetry.Kind.IMPACT, flight.last().kind());
        assertEquals(1, flight.rowAt(0));
    }

    @Test
    void indexesFiniteBurnsAndAnImpulseAtTheSharedBoundary() throws IOException {
        Telemetry.Flight flight = Telemetry.load(write(
                row(0, "burn_start") + row(5, "sample") + row(10, "burn_end")
                        + row(10, "pre_burn") + row(10, "post_burn") + row(10, "burn_start")
                        + row(20, "burn_end") + row(20, "complete")));
        assertEquals(java.util.List.of(0, 2, 4, 5, 6, 7), flight.events());
        assertEquals(5, flight.rowAt(10));
        assertEquals(7, flight.rowAt(20));
    }

    @Test
    void permitsAnImpactDuringThrustAndRejectsInvalidFiniteBurnSequences() throws IOException {
        String start = row(0, "burn_start");
        assertEquals(Telemetry.Kind.IMPACT, Telemetry.load(write(start + row(10, "impact"))).last().kind());
        assertThrows(IOException.class, () -> Telemetry.load(write(start + row(10, "complete"))));
        assertThrows(IOException.class, () -> Telemetry.load(write(start + row(5, "burn_start") + row(10, "impact"))));
        assertThrows(IOException.class, () -> Telemetry.load(write(
                row(0, "sample") + row(5, "burn_end") + row(10, "complete"))));
    }

    @Test
    void indexesApsidesBetweenCadenceRows() throws IOException {
        Telemetry.Flight flight = Telemetry.load(write(
                row(0, "sample") + row(5.1, "apoapsis") + row(6, "sample")
                        + row(12.8, "periapsis") + row(15, "complete")));
        assertEquals(java.util.List.of(1, 3, 4), flight.events());
        assertEquals(1, flight.rowAt(5.2));
        assertEquals(Telemetry.Kind.PERIAPSIS, flight.rows().get(3).kind());
    }

    @Test
    void rejectsMalformedOrNonfiniteNumbersAndOversizedRows() throws IOException {
        assertThrows(IOException.class, () -> Telemetry.load(write("0,sample\n")));
        String invalid = row(0, "sample").replace("7000", "NaN");
        assertThrows(IOException.class, () -> Telemetry.load(write(invalid + row(10, "complete"))));
        assertThrows(IOException.class, () -> Telemetry.load(write("9".repeat(1025))));
    }

    @Test
    void rejectsIncompleteOrOutOfOrderTelemetry() throws IOException {
        assertThrows(IOException.class, () -> Telemetry.load(write(row(0, "sample"))));
        assertThrows(IOException.class, () -> Telemetry.load(write(row(0, "sample") + row(1, "post_burn"))));
        assertThrows(IOException.class, () -> Telemetry.load(write(
                row(0, "sample") + row(1, "complete") + row(2, "sample"))));
        assertThrows(IOException.class, () -> Telemetry.load(write(
                row(0, "sample") + row(2, "sample") + row(1, "impact"))));
    }

    private Path write(String rows) throws IOException {
        return Files.writeString(directory.resolve("telemetry.csv"), Telemetry.HEADER + "\n" + rows);
    }

    private static String row(double time, String kind) {
        return time + "," + kind + ",7000,0,0,0,7.54,0,621.863,7.54,-28.47,,7000,0,0,,,\n";
    }
}
