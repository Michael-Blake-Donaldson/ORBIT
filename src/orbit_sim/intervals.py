"""Locate positive geometry margins along accepted integration steps."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache

from scipy.integrate import DenseOutput
from scipy.optimize import brentq, minimize_scalar

from .errors import SimulationError
from .mechanics import Array
from .mission import MAX_SAMPLES, Mission
from .propagation import _propagate

ROOT_TOLERANCE_S = 1e-7


@dataclass(frozen=True, slots=True)
class TimeInterval:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def _positive_intervals(
    margin: Callable[[float], float], start: float, end: float, left: float, right: float,
) -> tuple[TimeInterval, ...]:
    positive_left, positive_right = left > 0, right > 0
    # An exact endpoint contact cannot establish a crossing inside the step.
    # Match the sign of the other endpoint so the interior extremum is checked.
    if left == 0:
        positive_left = positive_right
    elif right == 0:
        positive_right = positive_left
    if positive_left != positive_right:
        cross = brentq(margin, start, end, xtol=ROOT_TOLERANCE_S)
        return (TimeInterval(start, cross) if positive_left else TimeInterval(cross, end),)

    # Check for a short positive or negative interval wholly between the endpoints.
    direction = 1 if positive_left else -1
    # Step-relative time preserves optimizer precision late in a mission.
    extremum = minimize_scalar(
        lambda offset: direction * margin(start + offset), bounds=(0, end - start), method="bounded",
        options={"xatol": ROOT_TOLERANCE_S, "maxiter": 60},
    )
    if not extremum.success:
        raise SimulationError("geometry interval search failed to converge")
    if extremum.fun >= 0:
        return (TimeInterval(start, end),) if positive_left else ()
    time = start + extremum.x
    enter = brentq(margin, start, time, xtol=ROOT_TOLERANCE_S)
    exit_ = brentq(margin, time, end, xtol=ROOT_TOLERANCE_S)
    if positive_left:
        return TimeInterval(start, enter), TimeInterval(exit_, end)
    return (TimeInterval(enter, exit_),)


def _append_interval(intervals: list[TimeInterval], interval: TimeInterval) -> None:
    if interval.duration_s <= 0:
        return
    if intervals and interval.start_s - intervals[-1].end_s <= ROOT_TOLERANCE_S:
        intervals[-1] = TimeInterval(intervals[-1].start_s, interval.end_s)
        return
    if len(intervals) == MAX_SAMPLES:
        raise SimulationError("geometry search exceeds the interval limit")
    intervals.append(interval)


def scan_intervals(
    mission: Mission, evaluate: Callable[[float, Array], tuple[float, ...]], margin_count: int,
) -> tuple[tuple[TimeInterval, ...], ...]:
    result: list[list[TimeInterval]] = [[] for _ in range(margin_count)]

    def observe(start: float, end: float, dense: DenseOutput) -> None:
        @cache
        def values(time: float) -> tuple[float, ...]:
            return evaluate(float(time), dense(time)[:3])

        midpoint = (start + end) / 2
        left, middle, right = values(start), values(midpoint), values(end)
        for index, intervals in enumerate(result):
            def margin(time: float, column: int = index) -> float:
                return values(time)[column]

            for lower, upper, from_margin, to_margin in (
                (start, midpoint, left[index], middle[index]), (midpoint, end, middle[index], right[index]),
            ):
                if lower < upper:
                    for interval in _positive_intervals(margin, lower, upper, from_margin, to_margin):
                        _append_interval(intervals, interval)

    # Diagnostic output cadence cannot affect an interval search.
    diagnostic = replace(mission, sample_interval_s=mission.duration_s, track_apsides=False)
    for _ in _propagate(diagnostic, observe):
        pass
    return tuple(tuple(intervals) for intervals in result)
