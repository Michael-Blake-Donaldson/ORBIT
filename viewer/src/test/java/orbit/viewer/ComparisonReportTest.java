package orbit.viewer;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ComparisonReportTest {
    @TempDir
    Path directory;

    @Test
    void findsIndependentPeaksAndSelectsTheNearestElapsedSample() throws IOException {
        ComparisonReport report = ComparisonReport.load(write(
                row(0, 0, 0, true) + row(10, 4, 0.3, false) + row(25, 2, 0.6, true)));
        assertEquals(10, report.peakPosition().time());
        assertEquals(25, report.peakVelocity().time());
        assertEquals(2, report.last().positionKm());
        assertEquals(0, report.nearestRow(5));
        assertEquals(1, report.nearestRow(17.5));
        assertEquals(2, report.nearestRow(17.6));
        assertEquals(2, report.nearestRow(1000));
        assertEquals(new ComparisonReport.Vector(2, 0, 0), report.last().rtn().position());
        assertNull(report.rows().get(1).rtn());
    }

    @Test
    void acceptsSingleSampleAndCrLf() throws IOException {
        String input = row(0, 4, 0.2, true).replace("\n", "\r\n");
        ComparisonReport report = ComparisonReport.load(write(input));
        assertEquals(1, report.rows().size());
        assertEquals(0, report.nearestRow(2));
    }

    @Test
    void rejectsEmptyDuplicateOrMisalignedTimestamps() {
        assertThrows(IOException.class, () -> ComparisonReport.load(write("")));
        assertThrows(IOException.class, () -> ComparisonReport.load(write(row(1, 0, 0, true))));
        assertThrows(IOException.class, () -> ComparisonReport.load(write(
                row(0, 0, 0, true) + row(1, 4, 0.3, false) + row(1, 2, 0.6, true))));
    }

    @Test
    void rejectsNonfiniteValuesWrongNormsAndPartialRtnVectors() {
        String valid = row(0, 4, 0.2, false);
        assertThrows(IOException.class, () -> ComparisonReport.load(write(valid.replace("0.2", "NaN"))));
        assertThrows(IOException.class, () -> ComparisonReport.load(write(valid.replaceFirst("4\\.0", "390"))));
        assertThrows(IOException.class, () -> ComparisonReport.load(write(valid.stripTrailing() + "0\n")));
        assertThrows(IOException.class, () -> ComparisonReport.load(write(valid.replace("4.0,", "4.0\r,"))));
    }

    @Test
    void displayReductionRetainsShortExtremaAndChronologicalOrder() throws IOException {
        StringBuilder content = new StringBuilder();
        for (int index = 0; index < 100; index++) {
            double separation = index == 17 ? 30 : index == 18 ? 0.25 : 4;
            content.append(row(index, separation, 0, true));
        }
        ComparisonReport report = ComparisonReport.load(write(content.toString()));
        var reduced = ComparisonPlot.reduce(report.rows(), 10, ComparisonReport.Row::positionKm);
        assertEquals(0, reduced.get(0).time());
        assertEquals(99, reduced.get(reduced.size() - 1).time());
        assertEquals(30, reduced.stream().mapToDouble(ComparisonReport.Row::positionKm).max().orElseThrow());
        assertEquals(0.25, reduced.stream().mapToDouble(ComparisonReport.Row::positionKm).min().orElseThrow());
        var times = new ArrayList<>(reduced.stream().map(ComparisonReport.Row::time).toList());
        assertEquals(times.stream().sorted().distinct().toList(), times);
    }

    private Path write(String rows) throws IOException {
        return Files.writeString(directory.resolve("comparison.csv"), ComparisonReport.HEADER + "\n" + rows);
    }

    private static String row(double time, double position, double velocity, boolean rtn) {
        String vectorFields = "," + position + ",0,0," + velocity + ",0,0";
        return time + ",sample,sample," + position + "," + velocity + vectorFields
                + (rtn ? vectorFields : ",,,,,,") + "\n";
    }
}
