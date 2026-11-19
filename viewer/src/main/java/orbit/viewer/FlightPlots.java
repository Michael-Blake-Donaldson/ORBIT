package orbit.viewer;

import java.util.List;
import java.util.TreeSet;
import javafx.scene.canvas.Canvas;
import javafx.scene.canvas.GraphicsContext;
import javafx.scene.layout.Region;
import javafx.scene.paint.Color;
import javafx.scene.paint.CycleMethod;
import javafx.scene.paint.RadialGradient;
import javafx.scene.paint.Stop;
import javafx.scene.text.Font;

/** Equal-scale ECI projections and an elapsed-time altitude profile. */
final class FlightPlots {
    private static final Color BACKGROUND = Color.web("#0a121d");
    private static final Color GRID = Color.web("#202e40");
    private static final Color AXIS = Color.web("#34485d");
    private static final Color MUTED = Color.web("#91a6bf");
    private static final Color TEAL = Color.web("#52dfcd");
    private static final Color AMBER = Color.web("#ffc47b");
    private static final Color IMPACT = Color.web("#fd8792");
    private static final Color FUTURE = Color.web("#617d9b");
    private static final Font CAPTION = Font.font("Monospaced", 11);
    private static final int DISPLAY_POINTS = 2500;

    enum Plane {
        XY(0, 1), XZ(0, 2), YZ(1, 2);

        private final int horizontal;
        private final int vertical;

        Plane(int horizontal, int vertical) {
            this.horizontal = horizontal;
            this.vertical = vertical;
        }

        private static double coordinate(Telemetry.Sample row, int axis) {
            return switch (axis) {
                case 0 -> row.x();
                case 1 -> row.y();
                default -> row.z();
            };
        }
    }

    private FlightPlots() {}

    static List<Integer> displayRows(Telemetry.Flight flight) {
        TreeSet<Integer> indices = new TreeSet<>(flight.events());
        int size = flight.rows().size();
        int stride = Math.max(1, (size + DISPLAY_POINTS - 1) / DISPLAY_POINTS);
        for (int index = 0; index < size; index += stride) {
            indices.add(index);
        }
        // Preserve both sides of every impulse when reducing long trajectories for display.
        for (int event : flight.events()) {
            if (flight.rows().get(event).kind() == Telemetry.Kind.POST_BURN) {
                indices.add(event - 1);
            }
        }
        return List.copyOf(indices);
    }

    private abstract static class Plot extends Region {
        private final Canvas canvas = new Canvas();
        Telemetry.Flight flight;
        List<Integer> indices = List.of();
        int selected;

        Plot() {
            getChildren().add(canvas);
            setMinWidth(0);
        }

        void load(Telemetry.Flight next, List<Integer> displayRows) {
            flight = next;
            indices = displayRows;
            selected = 0;
            redraw();
        }

        void select(int index) {
            selected = index;
            redraw();
        }

        @Override
        protected void layoutChildren() {
            canvas.setWidth(getWidth());
            canvas.setHeight(getHeight());
            redraw();
        }

        final void redraw() {
            double width = canvas.getWidth();
            double height = canvas.getHeight();
            if (width <= 0 || height <= 0) {
                return;
            }
            GraphicsContext g = canvas.getGraphicsContext2D();
            g.setFill(BACKGROUND);
            g.fillRect(0, 0, width, height);
            g.setFont(CAPTION);
            paint(g, width, height);
        }

        abstract void paint(GraphicsContext g, double width, double height);
    }

    static final class Trajectory extends Plot {
        private Plane plane = Plane.XY;
        private double zoom = 1;

        Trajectory() {
            setMinHeight(210);
            setAccessibleText("Earth-centered trajectory projection");
        }

        void setPlane(Plane value) {
            plane = value;
            redraw();
        }

        void setZoom(double value) {
            zoom = value;
            redraw();
        }

        @Override
        void paint(GraphicsContext g, double width, double height) {
            double cx = width / 2;
            double cy = height / 2;
            double extent = flight == null ? 16_000 : flight.extent();
            double scale = 0.42 * Math.min(width, height) * zoom / extent;
            double gridStep = niceStep(68 / scale);
            drawGrid(g, width, height, cx, cy, scale * gridStep);
            drawEarth(g, cx, cy, scale * Telemetry.EARTH_RADIUS_KM);

            if (flight != null) {
                path(g, cx, cy, scale, flight.rows().size() - 1, FUTURE, 1.25);
                path(g, cx, cy, scale, selected, TEAL, 2.25);
                for (int event : flight.events()) {
                    Telemetry.Sample row = flight.rows().get(event);
                    if (row.kind().maneuverEvent()) {
                        dot(g, x(row, cx, scale), y(row, cy, scale), 4, AMBER, BACKGROUND);
                    }
                }
                Telemetry.Sample row = flight.rows().get(selected);
                dot(g, x(row, cx, scale), y(row, cy, scale), 6, row.kind() == Telemetry.Kind.IMPACT ? IMPACT : TEAL,
                        Color.WHITE);
            }

            g.setFill(MUTED);
            g.fillText("+" + "XYZ".charAt(plane.horizontal), width - 31, cy - 8);
            g.fillText("+" + "XYZ".charAt(plane.vertical), cx + 8, 17);
            g.fillText("Grid " + String.format(java.util.Locale.ROOT, "%,.0f km", gridStep), 14, height - 14);
        }

        private static void drawGrid(GraphicsContext g, double w, double h, double cx, double cy, double pixels) {
            g.setLineWidth(1);
            g.setStroke(GRID);
            for (double x = cx % pixels; x < w; x += pixels) {
                g.strokeLine(x, 0, x, h);
            }
            for (double y = cy % pixels; y < h; y += pixels) {
                g.strokeLine(0, y, w, y);
            }
            g.setStroke(AXIS);
            g.strokeLine(cx, 0, cx, h);
            g.strokeLine(0, cy, w, cy);
        }

        private static void drawEarth(GraphicsContext g, double cx, double cy, double radius) {
            g.setFill(new RadialGradient(
                    215, 0.24, 0.5, 0.5, 0.64, true, CycleMethod.NO_CYCLE,
                    new Stop(0, Color.web("#254c68")), new Stop(0.68, Color.web("#173550")),
                    new Stop(1, Color.web("#101e30"))));
            g.fillOval(cx - radius, cy - radius, 2 * radius, 2 * radius);
            g.setStroke(Color.web("#427292"));
            g.setLineWidth(1.1);
            g.strokeOval(cx - radius, cy - radius, 2 * radius, 2 * radius);
            if (radius > 22) {
                g.setFill(Color.web("#9db9cc"));
                g.fillText("EARTH", cx - 17, cy + 4);
            }
        }

        private void path(GraphicsContext g, double cx, double cy, double scale, int end, Color color, double weight) {
            List<Telemetry.Sample> rows = flight.rows();
            Telemetry.Sample first = rows.get(0);
            g.setStroke(color);
            g.setLineWidth(weight);
            g.beginPath();
            g.moveTo(x(first, cx, scale), y(first, cy, scale));
            for (int index : indices) {
                if (index > end) {
                    break;
                }
                Telemetry.Sample row = rows.get(index);
                g.lineTo(x(row, cx, scale), y(row, cy, scale));
            }
            Telemetry.Sample last = rows.get(end);
            g.lineTo(x(last, cx, scale), y(last, cy, scale));
            g.stroke();
        }

        private double x(Telemetry.Sample row, double center, double scale) {
            return center + Plane.coordinate(row, plane.horizontal) * scale;
        }

        private double y(Telemetry.Sample row, double center, double scale) {
            return center - Plane.coordinate(row, plane.vertical) * scale;
        }
    }

    static final class Altitude extends Plot {
        Altitude() {
            setMinHeight(98);
            setPrefHeight(106);
            setMaxHeight(106);
            setAccessibleText("Altitude profile over elapsed mission time");
        }

        @Override
        void paint(GraphicsContext g, double width, double height) {
            double left = 79;
            double right = Math.max(left + 1, width - 18);
            double top = 12;
            double bottom = height - 23;
            g.setStroke(GRID);
            g.setLineWidth(1);
            for (int tick = 0; tick <= 2; tick++) {
                double y = top + (bottom - top) * tick / 2;
                g.strokeLine(left, y, right, y);
            }
            if (flight == null) {
                return;
            }
            double spread = Math.max(1, flight.maxAltitude() - flight.minAltitude());
            double high = flight.maxAltitude() + spread * 0.05;
            double low = flight.minAltitude() - spread * 0.05;
            double endTime = flight.last().time();
            double duration = endTime > 0 ? endTime : 1;
            g.setFill(MUTED);
            g.fillText(formatAxis(high), 9, top + 4);
            g.fillText(formatAxis(low), 9, bottom + 4);
            g.fillText("0 s", left, height - 7);
            String end = endTime > 0 && endTime < 1
                    ? String.format(java.util.Locale.ROOT, "%.3g s", endTime)
                    : String.format(java.util.Locale.ROOT, "%,.0f s", endTime);
            g.fillText(end, right - end.length() * 7, height - 7);

            List<Telemetry.Sample> rows = flight.rows();
            g.setStroke(TEAL);
            g.setLineWidth(1.7);
            g.beginPath();
            g.moveTo(left, altitudeY(rows.get(0), high, low, top, bottom));
            for (int index : indices) {
                Telemetry.Sample row = rows.get(index);
                double x = left + (right - left) * row.time() / duration;
                g.lineTo(x, altitudeY(row, high, low, top, bottom));
            }
            g.stroke();
            Telemetry.Sample current = rows.get(selected);
            double x = left + (right - left) * current.time() / duration;
            g.setStroke(FUTURE);
            g.setLineWidth(1);
            g.strokeLine(x, top, x, bottom);
            dot(g, x, altitudeY(current, high, low, top, bottom), 3.5, TEAL, Color.WHITE);
        }

        private static double altitudeY(Telemetry.Sample row, double high, double low, double top, double bottom) {
            return top + (high - row.altitude()) / (high - low) * (bottom - top);
        }

        private static String formatAxis(double value) {
            return Math.abs(value) < 1e6
                    ? String.format(java.util.Locale.ROOT, "%,.0f km", value)
                    : String.format(java.util.Locale.ROOT, "%.1e", value);
        }
    }

    private static void dot(GraphicsContext g, double x, double y, double radius, Color ring, Color fill) {
        g.setFill(ring);
        g.fillOval(x - radius, y - radius, 2 * radius, 2 * radius);
        g.setFill(fill);
        double inner = radius * 0.47;
        g.fillOval(x - inner, y - inner, 2 * inner, 2 * inner);
    }

    private static double niceStep(double value) {
        double power = Math.pow(10, Math.floor(Math.log10(value)));
        double normalized = value / power;
        double factor = normalized < 2 ? 1 : normalized < 5 ? 2 : 5;
        return factor * power;
    }
}
