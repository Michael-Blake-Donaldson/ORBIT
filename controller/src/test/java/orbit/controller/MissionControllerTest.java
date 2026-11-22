package orbit.controller;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class MissionControllerTest {
    private static final String HEADER = "time_s,kind,x_km,y_km,z_km,vx_km_s,vy_km_s,vz_km_s,altitude_km,speed_km_s,"
            + "specific_energy_km2_s2,mass_kg,semi_major_axis_km,eccentricity,inclination_deg,raan_deg,"
            + "argument_of_periapsis_deg,true_anomaly_deg\n";
    private static final String STATE = ",7000,0,0,0,7.5,0,621.863,7.5,-28.47,,7000,0,0,,,\n";
    static final String COMPLETE_CSV = HEADER + row(0, "complete");

    @TempDir
    Path directory;

    @Test
    void readsCanonicalRowsIncludingSharedManeuverEpochs() throws IOException {
        Path path = directory.resolve("telemetry.csv");
        Files.writeString(path, HEADER + row(0, "sample") + row(1, "pre_burn") + row(1, "post_burn")
                + row(2, "complete"));
        assertEquals(new MissionController.Receipt("complete", 2, 4), MissionController.readReceipt(path, 4, 4096));
    }

    @Test
    void rejectsMalformedTelemetryBeforePublication() throws IOException {
        Path path = directory.resolve("telemetry.csv");
        for (String csv : List.of(
                "time_s,kind,x_km\n0,complete,7000\n",
                HEADER + row(1, "complete"),
                HEADER + row(0, "sample") + row(2, "sample") + row(1, "impact"),
                HEADER + row(0, "sample") + row(1, "impact") + row(2, "complete"),
                HEADER + row(0, "sample") + row(1, "unknown") + row(2, "complete"),
                HEADER + row(0, "sample").replace("-28.47", "NaN") + row(2, "complete"),
                HEADER + row(0, "sample") + row(2, "complete").replace(",7.5,", ",0x1p0,"))) {
            Files.writeString(path, csv);
            assertThrows(IOException.class, () -> MissionController.readReceipt(path, 4, 4096), csv);
        }
    }

    private static String row(int elapsed, String kind) {
        return elapsed + "," + kind + STATE;
    }
}
