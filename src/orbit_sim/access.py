"""Find geometric ground-station passes along the propagated trajectory."""

from collections.abc import Callable
from dataclasses import dataclass, replace

from scipy.integrate import DenseOutput
from scipy.optimize import brentq, minimize_scalar

from .errors import SimulationError
from .ground import GroundStation, eci_to_ecef, look_angles
from .mission import MAX_SAMPLES, Mission, number
from .propagation import _propagate

_ROOT_TOLERANCE_S = 1e-7


@dataclass(frozen=True, slots=True)
class AccessWindow:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def _visible_intervals(
    margin: Callable[[float], float], start: float, end: float, left: float, right: float,
) -> tuple[AccessWindow, ...]:
    visible_left, visible_right = left > 0, right > 0
    if visible_left != visible_right:
        cross = brentq(margin, start, end, xtol=_ROOT_TOLERANCE_S)
        return (AccessWindow(start, cross) if visible_left else AccessWindow(cross, end),)

    # Check for a short pass or gap wholly between the endpoints.
    direction = 1 if visible_left else -1
    # Search in step-relative time so the optimizer's precision does not degrade late in a mission.
    extremum = minimize_scalar(
        lambda offset: direction * margin(start + offset), bounds=(0, end - start), method="bounded",
        options={"xatol": _ROOT_TOLERANCE_S, "maxiter": 60},
    )
    if not extremum.success:
        raise SimulationError("ground-station pass search failed to converge")
    if extremum.fun >= 0:
        return (AccessWindow(start, end),) if visible_left else ()
    time = start + extremum.x
    enter = brentq(margin, start, time, xtol=_ROOT_TOLERANCE_S)
    exit_ = brentq(margin, time, end, xtol=_ROOT_TOLERANCE_S)
    if visible_left:
        return AccessWindow(start, enter), AccessWindow(exit_, end)
    return (AccessWindow(enter, exit_),)


def access_windows(
    mission: Mission, station: GroundStation, greenwich_angle_deg_at_epoch: float,
) -> tuple[AccessWindow, ...]:
    """Locate intervals above the elevation mask, clipped to the actual trajectory."""
    angle = number(greenwich_angle_deg_at_epoch, "Greenwich angle", -360, 360)
    windows: list[AccessWindow] = []

    def observe(start: float, end: float, dense: DenseOutput) -> None:
        def margin(time: float) -> float:
            position = tuple(map(float, dense(time)[:3]))
            ecef = eci_to_ecef(position, float(time), angle)
            elevation = look_angles(ecef, station).elevation_deg
            return -180 if elevation is None else elevation - station.minimum_elevation_deg

        midpoint = (start + end) / 2
        left, middle, right = margin(start), margin(midpoint), margin(end)
        for lower, upper, from_margin, to_margin in (
            (start, midpoint, left, middle), (midpoint, end, middle, right),
        ):
            if lower == upper:
                continue
            for interval in _visible_intervals(margin, lower, upper, from_margin, to_margin):
                if interval.duration_s <= 0:
                    continue
                if windows and interval.start_s - windows[-1].end_s <= _ROOT_TOLERANCE_S:
                    windows[-1] = AccessWindow(windows[-1].start_s, interval.end_s)
                else:
                    if len(windows) == MAX_SAMPLES:
                        raise SimulationError("ground-station access exceeds the window limit")
                    windows.append(interval)

    # Orbit integration is unchanged. Skip regular output sampling to keep this analysis independent of cadence.
    diagnostic = replace(mission, sample_interval_s=mission.duration_s, track_apsides=False)
    for _ in _propagate(diagnostic, observe):
        pass
    return tuple(windows)
