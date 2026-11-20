"""Geometric solar eclipse windows for a propagated Earth-orbit mission."""

from dataclasses import dataclass
from math import asin, atan2, hypot
from typing import Literal

from .ephemeris import _position_interpolant
from .errors import SimulationError
from .intervals import TimeInterval, scan_intervals
from .mechanics import Array, _cross
from .mission import EARTH_RADIUS_KM, Mission, number

NOMINAL_SOLAR_RADIUS_KM = 695_700.0
EclipseKind = Literal["penumbra", "umbra", "antumbra"]


@dataclass(frozen=True, slots=True)
class EclipseWindow(TimeInterval):
    kind: EclipseKind


def _eclipse_margins(position: Array, sun_position: Array, sun_radius: float) -> tuple[float, float, float]:
    if not hypot(*sun_position) > EARTH_RADIUS_KM + sun_radius:
        raise SimulationError("solar ephemeris must describe a disc outside Earth")
    to_sun = sun_position - position
    sun_distance = hypot(*to_sun)
    if sun_distance <= sun_radius:
        raise SimulationError("eclipse geometry is undefined inside the solar disc's body")
    earth_distance = hypot(*position)
    if earth_distance >= sun_distance:
        return -1.0, -1.0, -1.0

    # Clamp the Earth angular radius at the surface to absorb impact-root roundoff.
    earth_radius = asin(min(1.0, EARTH_RADIUS_KM / earth_distance))
    solar_radius = asin(sun_radius / sun_distance)
    earth_direction, sun_direction = -position / earth_distance, to_sun / sun_distance
    separation = atan2(hypot(*_cross(earth_direction, sun_direction)), earth_direction @ sun_direction)
    return (
        earth_radius + solar_radius - separation,
        earth_radius - solar_radius - separation,
        solar_radius - earth_radius - separation,
    )


def _eclipse_phases(
    overlaps: tuple[TimeInterval, ...], umbra: tuple[TimeInterval, ...], antumbra: tuple[TimeInterval, ...],
) -> tuple[EclipseWindow, ...]:
    internal = sorted(
        (
            *(EclipseWindow(window.start_s, window.end_s, "umbra") for window in umbra),
            *(EclipseWindow(window.start_s, window.end_s, "antumbra") for window in antumbra),
        ),
        key=lambda interval: interval.start_s,
    )
    phases: list[EclipseWindow] = []
    index = 0
    for overlap in overlaps:
        start = overlap.start_s
        while index < len(internal) and internal[index].start_s < overlap.end_s:
            window = internal[index]
            index += 1
            lo, hi = max(start, window.start_s), min(overlap.end_s, window.end_s)
            if hi <= lo:
                continue
            if start < lo:
                phases.append(EclipseWindow(start, lo, "penumbra"))
            phases.append(EclipseWindow(lo, hi, window.kind))
            start = hi
        if start < overlap.end_s:
            phases.append(EclipseWindow(start, overlap.end_s, "penumbra"))
    return tuple(phases)


def eclipse_windows(
    mission: Mission, sun_name: str = "Sun", sun_radius_km: float = NOMINAL_SOLAR_RADIUS_KM,
) -> tuple[EclipseWindow, ...]:
    """Return consecutive penumbra, umbra, and antumbra intervals in elapsed seconds."""
    radius = number(sun_radius_km, "sun_radius_km", 1, 1e8)
    sun = next((body for body in mission.third_bodies if body.name.casefold() == sun_name.casefold()), None)
    if sun is None:
        raise ValueError("mission must include the selected Sun ephemeris in third_bodies")
    position_at = _position_interpolant(sun)

    def evaluate(time: float, position: Array) -> tuple[float, float, float]:
        return _eclipse_margins(position, position_at(time), radius)

    return _eclipse_phases(*scan_intervals(mission, evaluate, 3))
