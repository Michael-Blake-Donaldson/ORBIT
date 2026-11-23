package orbit.viewer;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.function.DoubleConsumer;
import java.util.function.ToDoubleFunction;
import javafx.scene.canvas.Canvas;
import javafx.scene.canvas.GraphicsContext;
import javafx.scene.layout.Region;
import javafx.scene.paint.Color;
import javafx.scene.text.Font;
import javafx.scene.text.TextAlignment;

/** Draw a sampled separation curve on a linear elapsed-time axis. */
final class ComparisonPlot extends Region {
    private static final double LEFT = 78;
    private static final double RIGHT = 28;
    private static final double TOP = 14;
    private static final double BOTTOM = 29;
    private static final Color BACKGROUND = Color.web("#0a121d");
    private static final Color GRID = Color.web("#29394d");
    private static final Color LABEL = Color.web("#aec2d8");
    private static final Font AXIS_FONT = Font.font("Monospaced", 11);

    private final Canvas canvas = new Canvas();
    private final ToDoubleFunction<ComparisonReport.Row> ordinate;
    private final Color accent;
    private ComparisonReport report;
    private ComparisonReport.Row selected;
    private List<ComparisonReport.Row> curve = List.of();
    private double yMax = 1;

    ComparisonPlot(String description, Color accent, ToDoubleFunction<ComparisonReport.Row> ordinate,
                   DoubleConsumer seek) {
        this.ordinate = ordinate;
        this.accent = accent;
        getChildren().add(canvas);
        setMinHeight(115);
        setAccessibleText(description + " over elapsed mission time");
        setOnMousePressed(event -> {
            double width = getWidth() - LEFT - RIGHT;
            if (report != null && width > 0) {
                double fraction = Math.max(0, Math.min(1, (event.getX() - LEFT) / width));
                seek.accept(fraction * report.last().time());
            }
        });
    }

    void load(ComparisonReport result, ComparisonReport.Row peak) {
        report = result;
        selected = null;
        double highest = ordinate.applyAsDouble(peak);
        yMax = highest > 0 ? highest * 1.09 : 1.0;
        rebuildCurve();
        redraw();
    }

    void select(ComparisonReport.Row row) {
        selected = row;
        redraw();
    }

    @Override
    protected void layoutChildren() {
        if (canvas.getWidth() != getWidth() || canvas.getHeight() != getHeight()) {
            canvas.setWidth(getWidth());
            canvas.setHeight(getHeight());
            rebuildCurve();
        }
        redraw();
    }

    private void rebuildCurve() {
        if (report != null) {
            int bins = Math.max(1, Math.min(1200, (int) ((canvas.getWidth() - LEFT - RIGHT) / 3)));
            curve = reduce(report.rows(), bins, ordinate);
        }
    }

    /** Keep the first, last, lowest, and highest samples in each display bucket. */
    static List<ComparisonReport.Row> reduce(List<ComparisonReport.Row> rows, int bins,
                                             ToDoubleFunction<ComparisonReport.Row> value) {
        if (rows.size() <= 4 * bins) {
            return rows;
        }
        double duration = rows.get(rows.size() - 1).time();
        List<ComparisonReport.Row> result = new ArrayList<>(4 * bins);
        int begin = 0;
        while (begin < rows.size()) {
            int low = begin;
            int high = begin;
            int bucket = bucketAt(rows.get(begin).time(), duration, bins);
            int end = begin + 1;
            while (end < rows.size() && bucketAt(rows.get(end).time(), duration, bins) == bucket) {
                double next = value.applyAsDouble(rows.get(end));
                if (next < value.applyAsDouble(rows.get(low))) {
                    low = end;
                }
                if (next > value.applyAsDouble(rows.get(high))) {
                    high = end;
                }
                end++;
            }
            int[] retained = {begin, low, high, end - 1};
            Arrays.sort(retained);
            int previous = -1;
            for (int index : retained) {
                if (index != previous) {
                    result.add(rows.get(index));
                    previous = index;
                }
            }
            begin = end;
        }
        return result;
    }

    private static int bucketAt(double time, double duration, int bins) {
        return Math.min(bins - 1, (int) (time / duration * bins));
    }

    private void redraw() {
        double width = canvas.getWidth();
        double height = canvas.getHeight();
        double plotWidth = width - LEFT - RIGHT;
        double plotHeight = height - TOP - BOTTOM;
        GraphicsContext graphics = canvas.getGraphicsContext2D();
        graphics.setFill(BACKGROUND);
        graphics.fillRect(0, 0, width, height);
        if (plotWidth <= 0 || plotHeight <= 0 || report == null) {
            return;
        }
        double duration = report.last().time();
        drawAxes(graphics, plotWidth, plotHeight, duration);
        graphics.setStroke(accent);
        graphics.setLineWidth(1.7);
        if (curve.size() > 1) {
            graphics.beginPath();
            for (int index = 0; index < curve.size(); index++) {
                ComparisonReport.Row row = curve.get(index);
                double x = xAt(row.time(), duration, plotWidth);
                double y = yAt(row, plotHeight);
                if (index == 0) {
                    graphics.moveTo(x, y);
                } else {
                    graphics.lineTo(x, y);
                }
            }
            graphics.stroke();
        }
        if (selected != null) {
            double x = xAt(selected.time(), duration, plotWidth);
            double y = yAt(selected, plotHeight);
            graphics.setStroke(LABEL);
            graphics.setLineWidth(1);
            graphics.strokeLine(x, TOP, x, TOP + plotHeight);
            graphics.setFill(accent);
            graphics.fillOval(x - 4, y - 4, 8, 8);
        }
    }

    private void drawAxes(GraphicsContext graphics, double width, double height, double duration) {
        graphics.setFont(AXIS_FONT);
        graphics.setStroke(GRID);
        graphics.setFill(LABEL);
        graphics.setLineWidth(1);
        graphics.setTextAlign(TextAlignment.RIGHT);
        for (int index = 0; index <= 4; index++) {
            double fraction = index / 4.0;
            double y = TOP + (1 - fraction) * height;
            graphics.strokeLine(LEFT, y, LEFT + width, y);
            graphics.fillText(formatTick(yMax * fraction), LEFT - 9, y + 4);
        }
        graphics.setTextAlign(TextAlignment.CENTER);
        int columns = duration == 0 ? 0 : 4;
        for (int index = 0; index <= columns; index++) {
            double fraction = index / 4.0;
            double x = LEFT + fraction * width;
            graphics.strokeLine(x, TOP, x, TOP + height);
            boolean milliseconds = duration > 0 && duration < 4;
            graphics.fillText(OrbitViewer.formatTime(fraction * duration, milliseconds),
                    x, TOP + height + 19);
        }
    }

    private static String formatTick(double value) {
        if (value == 0) {
            return "0";
        }
        return String.format(Locale.ROOT, value >= 1e5 || value < 0.01 ? "%.1e" : "%.3g", value);
    }

    private static double xAt(double time, double duration, double width) {
        return LEFT + (duration == 0 ? 0 : time / duration * width);
    }

    private double yAt(ComparisonReport.Row row, double height) {
        return TOP + (1 - ordinate.applyAsDouble(row) / yMax) * height;
    }
}
