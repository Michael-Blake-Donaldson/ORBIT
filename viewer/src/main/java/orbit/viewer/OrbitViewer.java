package orbit.viewer;

import java.io.IOException;
import java.nio.file.InvalidPathException;
import java.nio.file.Path;
import java.util.List;
import java.util.Locale;
import java.util.Objects;
import javafx.animation.AnimationTimer;
import javafx.application.Application;
import javafx.collections.FXCollections;
import javafx.concurrent.Task;
import javafx.geometry.Pos;
import javafx.scene.Node;
import javafx.scene.Scene;
import javafx.scene.control.Button;
import javafx.scene.control.ComboBox;
import javafx.scene.control.Label;
import javafx.scene.control.ListCell;
import javafx.scene.control.ListView;
import javafx.scene.control.Slider;
import javafx.scene.control.Tooltip;
import javafx.scene.input.KeyCode;
import javafx.scene.input.KeyCodeCombination;
import javafx.scene.input.KeyCombination;
import javafx.scene.input.MouseEvent;
import javafx.scene.layout.BorderPane;
import javafx.scene.layout.GridPane;
import javafx.scene.layout.HBox;
import javafx.scene.layout.Priority;
import javafx.scene.layout.Region;
import javafx.scene.layout.VBox;
import javafx.stage.FileChooser;
import javafx.stage.Stage;
import javafx.util.StringConverter;

/** Desktop viewer for a completed ORBIT telemetry file. */
public final class OrbitViewer extends Application {
    private final Label fileName = label("Open an ORBIT telemetry CSV", "file-name");
    private final Label status = label("Awaiting telemetry", "status");
    private final Label message = label("", "message");
    private final Button open = new Button("Open CSV…");
    private final Button play = new Button("Play");
    private final Button previous = new Button("‹");
    private final Button next = new Button("›");
    private final Slider timeline = new Slider(0, 1, 0);
    private final ComboBox<Integer> rate = new ComboBox<>();
    private final Label clock = label("00:00:00 / 00:00:00", "mono");
    private final Label rowLabel = label("No sample selected", "muted");
    private final FlightPlots.Trajectory trajectory = new FlightPlots.Trajectory();
    private final FlightPlots.Altitude altitudePlot = new FlightPlots.Altitude();
    private final ListView<Integer> events = new ListView<>();
    private final Metric altitude = new Metric("Altitude", "km");
    private final Metric speed = new Metric("Speed", "km/s");
    private final Metric energy = new Metric("Specific energy", "km²/s²");
    private final Metric mass = new Metric("Mass", "kg");
    private final Metric axis = new Metric("Semi-major axis", "km");
    private final Metric eccentricity = new Metric("Eccentricity", "");
    private final Metric inclination = new Metric("Inclination", "°");
    private final Metric raan = new Metric("RAAN", "°");
    private final List<Metric> metrics = List.of(altitude, speed, energy, mass, axis, eccentricity, inclination, raan);

    private Telemetry.Flight flight;
    private Task<Telemetry.Flight> loading;
    private int selected = -1;
    private boolean selectingRow;
    private boolean playing;
    private long lastFrame;
    private final AnimationTimer animation = new AnimationTimer() {
        @Override
        public void handle(long now) {
            if (lastFrame != 0) {
                advance(Math.min(0.1, (now - lastFrame) / 1e9) * rate.getValue());
            }
            lastFrame = now;
        }
    };

    @Override
    public void start(Stage stage) {
        open.setId("open-telemetry");
        open.setOnAction(ignored -> choose(stage));
        message.setWrapText(true);
        message.managedProperty().bind(message.visibleProperty());
        message.visibleProperty().bind(message.textProperty().isNotEmpty());

        VBox plotColumn = new VBox(12, trajectoryPanel(), panel("Altitude profile", altitudePlot));
        HBox.setHgrow(plotColumn, Priority.ALWAYS);
        VBox.setVgrow(plotColumn.getChildren().get(0), Priority.ALWAYS);
        VBox rail = new VBox(12, inspector(), eventPanel());
        rail.setMinWidth(260);
        rail.setPrefWidth(275);
        rail.setMaxWidth(294);
        VBox.setVgrow(rail.getChildren().get(1), Priority.ALWAYS);
        HBox workspace = new HBox(14, plotColumn, rail);
        workspace.getStyleClass().add("workspace");

        BorderPane root = new BorderPane(workspace, header(), null, playback(), null);
        Scene scene = new Scene(root, 1200, 800);
        scene.getStylesheets().add(Objects.requireNonNull(getClass().getResource("viewer.css")).toExternalForm());
        scene.getAccelerators().put(new KeyCodeCombination(KeyCode.O, KeyCombination.SHORTCUT_DOWN), open::fire);
        stage.setTitle("ORBIT Mission Viewer");
        stage.setMinWidth(900);
        stage.setMinHeight(680);
        stage.setScene(scene);
        updateControls();
        stage.show();
        if (!getParameters().getRaw().isEmpty()) {
            try {
                load(Path.of(getParameters().getRaw().get(0)));
            } catch (InvalidPathException exception) {
                message.setText("The telemetry path is invalid.");
            }
        }
    }

    @Override
    public void stop() {
        setPlaying(false);
        if (loading != null) {
            loading.cancel(true);
        }
    }

    private Node header() {
        HBox identity = new HBox(14, label("ORBIT", "brand"), new VBox(3,
                label("MISSION TELEMETRY", "eyebrow"), fileName));
        identity.setAlignment(Pos.CENTER_LEFT);
        fileName.setMaxWidth(470);
        HBox header = new HBox(16, identity, spacer(), status, open);
        header.getStyleClass().add("header");
        header.setAlignment(Pos.CENTER_LEFT);
        VBox top = new VBox(header, message);
        return top;
    }

    private VBox trajectoryPanel() {
        ComboBox<FlightPlots.Plane> plane = new ComboBox<>(FXCollections.observableArrayList(FlightPlots.Plane.values()));
        plane.setValue(FlightPlots.Plane.XY);
        plane.setAccessibleText("Projection plane");
        plane.valueProperty().addListener((observable, old, value) -> trajectory.setPlane(value));
        Slider zoom = new Slider(0.5, 2.0, 1);
        zoom.setPrefWidth(78);
        zoom.setAccessibleText("Trajectory zoom");
        zoom.valueProperty().addListener((observable, old, value) -> trajectory.setZoom(value.doubleValue()));
        Button fit = new Button("Fit");
        fit.setOnAction(ignored -> zoom.setValue(1));
        HBox heading = new HBox(9, label("Trajectory", "panel-title"), label("ECI projection", "muted"),
                spacer(), plane, label("Zoom", "muted"), zoom, fit);
        heading.getStyleClass().add("panel-heading");
        heading.setAlignment(Pos.CENTER_LEFT);
        HBox legend = new HBox(19, legend("●", "Spacecraft", "spacecraft-color"),
                legend("━", "Flown path", "spacecraft-color"), legend("━", "Remaining path", "planned-color"),
                legend("○", "Burn", "burn-color"));
        legend.getStyleClass().add("legend");
        VBox panel = new VBox(heading, trajectory, legend);
        panel.getStyleClass().add("panel");
        VBox.setVgrow(trajectory, Priority.ALWAYS);
        return panel;
    }

    private VBox inspector() {
        GridPane grid = new GridPane();
        grid.getStyleClass().add("metric-grid");
        for (int index = 0; index < metrics.size(); index++) {
            VBox box = metrics.get(index).box();
            grid.add(box, index % 2, index / 2);
            GridPane.setHgrow(box, Priority.ALWAYS);
        }
        VBox inspector = panel("Selected sample", new VBox(rowLabel, grid));
        rowLabel.getStyleClass().add("sample-time");
        return inspector;
    }

    private VBox eventPanel() {
        events.setId("mission-events");
        events.setAccessibleText("Mission events. Select an event to inspect it.");
        events.setPlaceholder(label("Burns and final outcome appear here", "muted"));
        events.setMinHeight(88);
        events.setPrefHeight(150);
        events.setCellFactory(view -> new ListCell<>() {
            @Override
            protected void updateItem(Integer index, boolean empty) {
                super.updateItem(index, empty);
                if (empty || index == null || flight == null) {
                    setText(null);
                    setGraphic(null);
                    return;
                }
                Telemetry.Sample row = flight.rows().get(index);
                String name = row.kind() == Telemetry.Kind.POST_BURN ? "Impulse applied" : row.kind().label();
                HBox content = new HBox(9, label("●", eventColor(row.kind())),
                        label(name, "event-name"), spacer(), label(formatTime(row.time(), false), "mono"));
                content.setAlignment(Pos.CENTER_LEFT);
                setText(null);
                setGraphic(content);
            }
        });
        events.getSelectionModel().selectedItemProperty().addListener((observable, old, index) -> {
            if (index != null) {
                selectRow(index);
            }
        });
        VBox panel = panel("Mission events", events);
        VBox.setVgrow(events, Priority.ALWAYS);
        return panel;
    }

    private Node playback() {
        play.setId("playback-toggle");
        play.getStyleClass().add("accent-button");
        play.setOnAction(ignored -> {
            if (playing) {
                setPlaying(false);
            } else if (flight != null) {
                if (timeline.getValue() >= flight.last().time()) {
                    timeline.setValue(0);
                }
                setPlaying(true);
            }
        });
        previous.setAccessibleText("Previous telemetry row");
        previous.setTooltip(new Tooltip("Previous telemetry row"));
        previous.setOnAction(ignored -> step(-1));
        next.setAccessibleText("Next telemetry row");
        next.setTooltip(new Tooltip("Next telemetry row"));
        next.setOnAction(ignored -> step(1));
        rate.getItems().setAll(60, 300, 900, 3600);
        rate.setValue(300);
        rate.setAccessibleText("Playback speed");
        rate.setConverter(new StringConverter<>() {
            @Override
            public String toString(Integer value) {
                return value == null ? "" : value + "×";
            }

            @Override
            public Integer fromString(String value) {
                return Integer.valueOf(value.replace("×", ""));
            }
        });
        timeline.setId("mission-timeline");
        timeline.setAccessibleText("Elapsed mission time");
        timeline.addEventFilter(MouseEvent.MOUSE_PRESSED, ignored -> setPlaying(false));
        timeline.valueProperty().addListener((observable, old, value) -> {
            if (!selectingRow) {
                seek(value.doubleValue());
            }
        });
        HBox.setHgrow(timeline, Priority.ALWAYS);
        HBox controls = new HBox(10, play, previous, next, rate, timeline, clock);
        controls.setAlignment(Pos.CENTER_LEFT);
        controls.getStyleClass().add("playback");
        return controls;
    }

    private void choose(Stage stage) {
        FileChooser chooser = new FileChooser();
        chooser.setTitle("Open ORBIT telemetry");
        chooser.getExtensionFilters().add(new FileChooser.ExtensionFilter("Telemetry CSV", "*.csv"));
        var file = chooser.showOpenDialog(stage);
        if (file != null) {
            load(file.toPath());
        }
    }

    private void load(Path path) {
        setPlaying(false);
        open.setDisable(true);
        message.setText("");
        status.setText("Loading telemetry…");
        Task<Telemetry.Flight> task = new Task<>() {
            @Override
            protected Telemetry.Flight call() throws IOException {
                return Telemetry.load(path);
            }
        };
        task.setOnSucceeded(ignored -> {
            loading = null;
            open.setDisable(false);
            showFlight(path, task.getValue());
        });
        task.setOnFailed(ignored -> {
            loading = null;
            open.setDisable(false);
            updateStatus();
            Throwable error = task.getException();
            message.setText(error instanceof IOException ? error.getMessage() : "Unable to read telemetry.");
        });
        loading = task;
        Thread worker = new Thread(task, "orbit-telemetry-reader");
        worker.setDaemon(true);
        worker.start();
    }

    private void showFlight(Path path, Telemetry.Flight loaded) {
        flight = loaded;
        selected = -1;
        fileName.setText(path.getFileName().toString());
        fileName.setTooltip(new Tooltip(path.toAbsolutePath().toString()));
        updateStatus();
        List<Integer> displayRows = FlightPlots.displayRows(flight);
        trajectory.load(flight, displayRows);
        altitudePlot.load(flight, displayRows);
        events.getSelectionModel().clearSelection();
        events.getItems().setAll(flight.events());
        double duration = flight.last().time();
        timeline.setMax(duration);
        timeline.setBlockIncrement(Math.max(0.001, duration / 100));
        timeline.setValue(0);
        rate.setValue(duration >= 30_000 ? 3600 : 300);
        seek(0);
    }

    private void updateStatus() {
        String text = flight == null ? "Awaiting telemetry"
                : flight.last().kind().label() + "  ·  " + String.format(Locale.ROOT, "%,d rows", flight.rows().size());
        status.setText(text);
        status.pseudoClassStateChanged(javafx.css.PseudoClass.getPseudoClass("impact"),
                flight != null && flight.last().kind() == Telemetry.Kind.IMPACT);
    }

    private void seek(double elapsed) {
        if (flight != null) {
            updateClock(elapsed);
            inspect(flight.rowAt(elapsed));
        }
    }

    private void inspect(int index) {
        if (index != selected) {
            selected = index;
            Telemetry.Sample row = flight.rows().get(index);
            rowLabel.setText("T+" + formatTime(row.time(), true) + "  ·  " + row.kind().label());
            altitude.show(row.altitude(), 2);
            speed.show(row.speed(), 3);
            energy.show(row.energy(), 3);
            mass.show(row.mass(), 2);
            axis.show(row.axis(), 2);
            eccentricity.show(row.eccentricity(), 5);
            inclination.show(row.inclination(), 2);
            raan.show(row.raan(), 2);
            trajectory.select(index);
            altitudePlot.select(index);
            Integer event = events.getSelectionModel().getSelectedItem();
            if (event != null && event != index) {
                events.getSelectionModel().clearSelection();
            }
            updateControls();
        }
    }

    private void selectRow(int index) {
        setPlaying(false);
        double time = flight.rows().get(index).time();
        selectingRow = true;
        try {
            timeline.setValue(time);
        } finally {
            selectingRow = false;
        }
        updateClock(time);
        inspect(index);
    }

    private void step(int direction) {
        if (flight != null && selected + direction >= 0 && selected + direction < flight.rows().size()) {
            selectRow(selected + direction);
        }
    }

    private void updateClock(double elapsed) {
        clock.setText(formatTime(elapsed, false) + " / " + formatTime(flight.last().time(), false));
    }

    private void advance(double seconds) {
        double target = Math.min(flight.last().time(), timeline.getValue() + seconds);
        timeline.setValue(target);
        if (target >= flight.last().time()) {
            setPlaying(false);
        }
    }

    private void setPlaying(boolean enabled) {
        playing = enabled;
        play.setText(enabled ? "Pause" : "Play");
        lastFrame = 0;
        if (enabled) {
            animation.start();
        } else {
            animation.stop();
        }
    }

    private void updateControls() {
        boolean canPlay = flight != null && flight.last().time() > 0;
        play.setDisable(!canPlay);
        rate.setDisable(!canPlay);
        timeline.setDisable(!canPlay);
        previous.setDisable(selected <= 0);
        next.setDisable(flight == null || selected >= flight.rows().size() - 1);
    }

    private static VBox panel(String title, Node content) {
        Label heading = label(title, "panel-title", "panel-heading");
        VBox panel = new VBox(heading, content);
        panel.getStyleClass().add("panel");
        return panel;
    }

    private static HBox legend(String symbol, String title, String color) {
        HBox entry = new HBox(6, label(symbol, color), label(title, "muted"));
        entry.setAlignment(Pos.CENTER_LEFT);
        return entry;
    }

    private static String eventColor(Telemetry.Kind kind) {
        return switch (kind) {
            case POST_BURN -> "burn-color";
            case IMPACT -> "impact-color";
            default -> "spacecraft-color";
        };
    }

    private static Region spacer() {
        Region spacer = new Region();
        HBox.setHgrow(spacer, Priority.ALWAYS);
        return spacer;
    }

    private static Label label(String text, String... styles) {
        Label label = new Label(text);
        label.getStyleClass().addAll(styles);
        return label;
    }

    private static String formatTime(double seconds, boolean milliseconds) {
        long ms = Math.round(seconds * 1000);
        String clock = String.format(Locale.ROOT, "%02d:%02d:%02d", ms / 3_600_000, ms / 60_000 % 60, ms / 1000 % 60);
        return milliseconds ? clock + String.format(Locale.ROOT, ".%03d", ms % 1000) : clock;
    }

    private static final class Metric {
        private final Label value = label("—", "metric-value");
        private final VBox box;
        private final String unit;

        Metric(String title, String unit) {
            this.unit = unit;
            box = new VBox(4, label(title, "metric-label"), value);
            box.getStyleClass().add("metric");
            box.setMaxWidth(Double.MAX_VALUE);
        }

        VBox box() {
            return box;
        }

        void show(double number, int decimals) {
            if (Double.isNaN(number)) {
                value.setText("—");
                return;
            }
            String pattern = Math.abs(number) >= 1e7 ? "%.3e" : "%,." + decimals + "f";
            value.setText(String.format(Locale.ROOT, pattern, number) + (unit.isEmpty() ? "" : " " + unit));
        }
    }

    public static void main(String[] args) {
        launch(args);
    }
}
