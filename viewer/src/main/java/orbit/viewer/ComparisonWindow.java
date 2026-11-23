package orbit.viewer;

import java.io.IOException;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Objects;
import javafx.concurrent.Task;
import javafx.geometry.Pos;
import javafx.scene.Scene;
import javafx.scene.control.Button;
import javafx.scene.control.Label;
import javafx.scene.control.Slider;
import javafx.scene.control.Tooltip;
import javafx.scene.layout.BorderPane;
import javafx.scene.layout.HBox;
import javafx.scene.layout.Priority;
import javafx.scene.layout.Region;
import javafx.scene.layout.VBox;
import javafx.scene.paint.Color;
import javafx.stage.FileChooser;
import javafx.stage.Stage;

/** A separate inspector for synchronized trajectory differences. */
final class ComparisonWindow {
    private static final String POSITION_UNIT = " km";
    private static final String VELOCITY_UNIT = " m/s";
    private final Stage stage = new Stage();
    private final Button open = new Button("Open comparison CSV…");
    private final Label fileName = OrbitViewer.label("Select a comparison report", "file-name");
    private final Label count = OrbitViewer.label("", "muted");
    private final Label error = OrbitViewer.label("", "message");
    private final SummaryCard peakPosition = new SummaryCard("Peak position separation");
    private final SummaryCard peakVelocity = new SummaryCard("Peak velocity separation");
    private final SummaryCard finalPosition = new SummaryCard("Final position separation");
    private final Label selectedTime = OrbitViewer.label("No sample selected", "panel-title");
    private final Label selectedValues = OrbitViewer.label("", "mono");
    private final Label selectedFrames = OrbitViewer.label("", "muted");
    private final Label positionDelta = OrbitViewer.label("", "mono");
    private final Label velocityDelta = OrbitViewer.label("", "mono");
    private final Slider timeline = new Slider(0, 1, 0);
    private final ComparisonPlot positionPlot = new ComparisonPlot(
            "Position separation in kilometers", Color.web("#52dfcd"), ComparisonReport.Row::positionKm, this::seek);
    private final ComparisonPlot velocityPlot = new ComparisonPlot(
            "Velocity separation in meters per second", Color.web("#ffc47b"), row -> row.velocityKmS() * 1000,
            this::seek);
    private ComparisonReport report;
    private Task<ComparisonReport> loading;
    private int selected = -1;

    ComparisonWindow(Stage owner) {
        stage.initOwner(owner);
        stage.setTitle("ORBIT Trajectory Comparison");
        stage.setMinWidth(770);
        stage.setMinHeight(635);
        stage.setOnHidden(ignored -> {
            if (loading != null) {
                Task<ComparisonReport> abandoned = loading;
                loading = null;
                abandoned.cancel(true);
                open.setDisable(false);
                updateCount();
            }
        });
        open.setOnAction(ignored -> choose());
        error.setWrapText(true);
        error.managedProperty().bind(error.visibleProperty());
        error.visibleProperty().bind(error.textProperty().isNotEmpty());

        Region gap = OrbitViewer.spacer();
        HBox header = new HBox(14, OrbitViewer.label("ORBIT", "brand"), new VBox(3,
                OrbitViewer.label("TRAJECTORY COMPARISON", "eyebrow"), fileName), gap, count, open);
        header.setAlignment(Pos.CENTER_LEFT);
        header.getStyleClass().add("header");
        fileName.setMaxWidth(390);

        HBox cards = new HBox(10, peakPosition.button, peakVelocity.button, finalPosition.button);
        VBox position = chart("Position separation", "km", positionPlot);
        VBox velocity = chart("Velocity separation", "m/s", velocityPlot);
        VBox workspace = new VBox(11, cards, position, velocity, selectionPanel());
        workspace.getStyleClass().add("workspace");
        VBox.setVgrow(position, Priority.ALWAYS);
        VBox.setVgrow(velocity, Priority.ALWAYS);

        timeline.setAccessibleText("Comparison sample time");
        timeline.setDisable(true);
        timeline.valueProperty().addListener((ignored, previous, next) -> showSelection(next.doubleValue()));
        HBox.setHgrow(timeline, Priority.ALWAYS);
        HBox footer = new HBox(12, OrbitViewer.label("Elapsed mission time", "muted"), timeline);
        footer.getStyleClass().add("playback");
        footer.setAlignment(Pos.CENTER_LEFT);

        BorderPane root = new BorderPane(workspace, new VBox(header, error), null, footer, null);
        Scene scene = new Scene(root, 965, 700);
        scene.getStylesheets().add(Objects.requireNonNull(getClass().getResource("viewer.css")).toExternalForm());
        stage.setScene(scene);
    }

    void show(Path path) {
        stage.show();
        stage.toFront();
        if (path != null) {
            load(path);
        } else if (report == null && loading == null) {
            choose();
        }
    }

    private void choose() {
        FileChooser chooser = new FileChooser();
        chooser.setTitle("Open ORBIT comparison report");
        chooser.getExtensionFilters().add(new FileChooser.ExtensionFilter("Comparison CSV", "*.csv"));
        var file = chooser.showOpenDialog(stage);
        if (file != null) {
            load(file.toPath());
        }
    }

    private void load(Path path) {
        if (loading != null) {
            loading.cancel(true);
        }
        open.setDisable(true);
        error.setText("");
        count.setText("Loading report…");
        Task<ComparisonReport> task = new Task<>() {
            @Override
            protected ComparisonReport call() throws IOException {
                return ComparisonReport.load(path);
            }
        };
        task.setOnSucceeded(ignored -> {
            if (loading == task) {
                loading = null;
                open.setDisable(false);
                showReport(path, task.getValue());
            }
        });
        task.setOnFailed(ignored -> {
            if (loading == task) {
                loading = null;
                open.setDisable(false);
                updateCount();
                Throwable exception = task.getException();
                error.setText(exception instanceof IOException ? exception.getMessage() : "Unable to read the report.");
            }
        });
        loading = task;
        Thread worker = new Thread(task, "orbit-comparison-reader");
        worker.setDaemon(true);
        worker.start();
    }

    private void showReport(Path path, ComparisonReport loaded) {
        report = loaded;
        selected = -1;
        fileName.setText(path.getFileName().toString());
        fileName.setTooltip(new Tooltip(path.toAbsolutePath().toString()));
        updateCount();
        peakPosition.show(report.peakPosition(), magnitude(report.peakPosition().positionKm()) + POSITION_UNIT);
        peakVelocity.show(report.peakVelocity(), magnitude(report.peakVelocity().velocityKmS() * 1000) + VELOCITY_UNIT);
        finalPosition.show(report.last(), magnitude(report.last().positionKm()) + POSITION_UNIT);
        positionPlot.load(report, report.peakPosition());
        velocityPlot.load(report, report.peakVelocity());
        double duration = report.last().time();
        timeline.setMax(duration == 0 ? 1 : duration);
        timeline.setBlockIncrement(Math.max(0.001, duration / 100));
        timeline.setDisable(duration == 0);
        seek(0);
    }

    private void updateCount() {
        count.setText(report == null ? "" : String.format(Locale.ROOT, "%,d samples", report.rows().size()));
    }

    private void seek(double time) {
        timeline.setValue(time);
        showSelection(time);
    }

    private void showSelection(double elapsed) {
        if (report == null) {
            return;
        }
        int index = report.nearestRow(elapsed);
        if (index == selected) {
            return;
        }
        selected = index;
        ComparisonReport.Row row = report.rows().get(index);
        selectedTime.setText("T+" + OrbitViewer.formatTime(row.time(), true));
        selectedValues.setText(magnitude(row.positionKm()) + POSITION_UNIT + "   ·   "
                + magnitude(row.velocityKmS() * 1000) + VELOCITY_UNIT);
        boolean hasRtn = row.rtn() != null;
        ComparisonReport.Delta delta = hasRtn ? row.rtn() : row.inertial();
        String[] axes = hasRtn ? new String[]{"R", "T", "N"} : new String[]{"X", "Y", "Z"};
        selectedFrames.setText("Reference " + row.referenceKind().label() + "  ·  Candidate " + row.candidateKind().label()
                + "  ·  " + (hasRtn ? "RTN components" : "ECI components"));
        positionDelta.setText("Δr   " + components(axes, delta.position(), 1) + POSITION_UNIT);
        velocityDelta.setText("Δv   " + components(axes, delta.velocity(), 1000) + VELOCITY_UNIT);
        positionPlot.select(row);
        velocityPlot.select(row);
    }

    private VBox selectionPanel() {
        HBox selectedHeading = new HBox(12, selectedTime, OrbitViewer.spacer(), selectedValues);
        selectedHeading.setAlignment(Pos.CENTER_LEFT);
        VBox panel = new VBox(5, selectedHeading, selectedFrames, positionDelta, velocityDelta);
        panel.getStyleClass().addAll("panel", "comparison-selection");
        return panel;
    }

    private static VBox chart(String title, String units, ComparisonPlot plot) {
        HBox heading = new HBox(8, OrbitViewer.label(title, "panel-title"), OrbitViewer.label(units, "muted"));
        heading.getStyleClass().add("panel-heading");
        heading.setAlignment(Pos.BASELINE_LEFT);
        VBox box = new VBox(heading, plot);
        box.getStyleClass().add("panel");
        VBox.setVgrow(plot, Priority.ALWAYS);
        return box;
    }

    private static String components(String[] axes, ComparisonReport.Vector values, double scale) {
        return axes[0] + " " + magnitude(values.a() * scale) + "    "
                + axes[1] + " " + magnitude(values.b() * scale) + "    "
                + axes[2] + " " + magnitude(values.c() * scale);
    }

    private static String magnitude(double value) {
        double absolute = Math.abs(value);
        if (absolute != 0 && (absolute < 0.001 || absolute >= 1e7)) {
            return String.format(Locale.ROOT, "%.3e", value);
        }
        return String.format(Locale.ROOT, "%,.3f", value == 0 ? 0 : value);
    }

    private final class SummaryCard {
        private final Button button = new Button();
        private final Label value = OrbitViewer.label("—", "comparison-value");
        private final Label time = OrbitViewer.label("", "muted");

        SummaryCard(String title) {
            button.setAccessibleText(title);
            button.setTooltip(new Tooltip("Jump to " + title.toLowerCase(Locale.ROOT)));
            button.setGraphic(new VBox(3, OrbitViewer.label(title, "metric-label"), value, time));
            button.setDisable(true);
            button.setMaxWidth(Double.MAX_VALUE);
            button.setPrefWidth(220);
            button.getStyleClass().add("comparison-summary");
            HBox.setHgrow(button, Priority.ALWAYS);
        }

        void show(ComparisonReport.Row row, String reading) {
            value.setText(reading);
            time.setText("T+" + OrbitViewer.formatTime(row.time(), true));
            button.setOnAction(ignored -> seek(row.time()));
            button.setDisable(false);
        }
    }
}
