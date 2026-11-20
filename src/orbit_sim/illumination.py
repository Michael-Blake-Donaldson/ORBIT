"""Geometric solar eclipse windows for a propagated Earth-orbit mission."""

from collections.abc import Iterator
from dataclasses import dataclass
from math import acos, asin, atan2, cos, hypot, pi, sin, sqrt
from typing import Literal

import numpy as np
from scipy.integrate import quad
from scipy.interpolate import CubicHermiteSpline

from .ephemeris import _position_interpolant
from .errors import SimulationError
from .intervals import TimeInterval, scan_intervals
from .mechanics import Array, _cross
from .mission import EARTH_RADIUS_KM, Mission, Vector, number, vector
from .propagation import SampleKind, propagate

NOMINAL_SOLAR_RADIUS_KM = 695_700.0
_AREA_TOLERANCE = 1e-11
EclipseKind = Literal["penumbra", "umbra", "antumbra"]


@dataclass(frozen=True, slots=True)
class EclipseWindow(TimeInterval):
    kind: EclipseKind


@dataclass(frozen=True, slots=True)
class SolarSample:
    time_s: float
    kind: SampleKind
    visible_fraction: float


def _disc_geometry(
    position: Array, sun_position: Array, sun_radius: float,
) -> tuple[float, float, float] | None:
    if not hypot(*sun_position) > EARTH_RADIUS_KM + sun_radius:
        raise SimulationError("solar ephemeris must describe a disc outside Earth")
    to_sun = sun_position - position
    sun_distance = hypot(*to_sun)
    if sun_distance <= sun_radius:
        raise SimulationError("eclipse geometry is undefined inside the Sun")
    earth_distance = hypot(*position)
    if earth_distance >= sun_distance:
        return None

    # Clamp the Earth angular radius at the surface to absorb impact-root roundoff.
    earth_radius = asin(min(1.0, EARTH_RADIUS_KM / earth_distance))
    solar_radius = asin(sun_radius / sun_distance)
    earth_direction, sun_direction = -position / earth_distance, to_sun / sun_distance
    separation = atan2(hypot(*_cross(earth_direction, sun_direction)), earth_direction @ sun_direction)
    return earth_radius, solar_radius, separation


def _eclipse_margins(position: Array, sun_position: Array, sun_radius: float) -> tuple[float, float, float]:
    geometry = _disc_geometry(position, sun_position, sun_radius)
    if geometry is None:
        return -1.0, -1.0, -1.0
    earth_radius, solar_radius, separation = geometry
    return (
        earth_radius + solar_radius - separation,
        earth_radius - solar_radius - separation,
        solar_radius - earth_radius - separation,
    )


def _visible_fraction(earth: float, solar: float, separation: float) -> float:
    if separation >= earth + solar:
        return 1.0
    if separation <= earth - solar:
        return 0.0

    solar_half_sine = sin(solar / 2)
    if separation <= solar - earth:
        return 1 - (sin(earth / 2) / solar_half_sine)**2

    # Parameterize solar rings by equal solid-angle area, normalized to [0, 1].
    inner = (sin(abs(earth - separation) / 2) / solar_half_sine)**2
    outer = (sin(min(solar, earth + separation) / 2) / solar_half_sine)**2
    fully_hidden = inner if separation < earth else 0.0
    sin_separation = sin(separation)
    cos_separation = cos(separation)
    # Use the cosine-difference identity when the Earth limb is close to the Sun's center.
    cos_gap = -2 * sin((earth + separation) / 2) * sin((earth - separation) / 2)

    def ring_coverage(area: float) -> float:
        half_sine = solar_half_sine * sqrt(area)
        ring_sine = 2 * half_sine * sqrt(1 - half_sine**2)
        edge = (cos_gap + 2 * cos_separation * half_sine**2) / (sin_separation * ring_sine)
        return acos(max(-1.0, min(1.0, edge))) / pi

    partial = 0.0
    span = outer - inner
    # Coverage per ring is bounded by one. An unresolved strip cannot exceed its area.
    if span > _AREA_TOLERANCE:
        result = quad(
            lambda fraction: ring_coverage(inner + fraction * span), 0, 1,
            epsabs=_AREA_TOLERANCE / span, epsrel=_AREA_TOLERANCE, full_output=1,
        )
        if len(result) != 3:
            raise SimulationError("solar-disc visibility integral failed to converge")
        partial = result[0] * span
    return max(0.0, min(1.0, 1 - fully_hidden - partial))


def solar_visibility_fraction(
    position_km: Vector, sun_position_km: Vector, sun_radius_km: float = NOMINAL_SOLAR_RADIUS_KM,
) -> float:
    """Return the visible fraction of the apparent solar solid angle."""
    position = np.array(vector(position_km, "position_km", 1e12))
    sun = np.array(vector(sun_position_km, "sun_position_km", 1e12))
    radius = number(sun_radius_km, "sun_radius_km", 1, 1e8)
    if hypot(*position) < EARTH_RADIUS_KM:
        raise ValueError("solar visibility requires an observer on or above the spherical Earth surface")
    geometry = _disc_geometry(position, sun, radius)
    return 1.0 if geometry is None else _visible_fraction(*geometry)


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


def _sun_ephemeris(mission: Mission, sun_name: str) -> CubicHermiteSpline:
    name = sun_name.casefold()
    sun = next((body for body in mission.third_bodies if body.name.casefold() == name), None)
    if sun is None:
        raise ValueError("mission must include the selected Sun ephemeris in third_bodies")
    return _position_interpolant(sun)


def eclipse_windows(
    mission: Mission, sun_name: str = "Sun", sun_radius_km: float = NOMINAL_SOLAR_RADIUS_KM,
) -> tuple[EclipseWindow, ...]:
    """Return consecutive penumbra, umbra, and antumbra intervals in elapsed seconds."""
    radius = number(sun_radius_km, "sun_radius_km", 1, 1e8)
    position_at = _sun_ephemeris(mission, sun_name)

    def evaluate(time: float, position: Array) -> tuple[float, float, float]:
        return _eclipse_margins(position, position_at(time), radius)

    return _eclipse_phases(*scan_intervals(mission, evaluate, 3))


def solar_samples(
    mission: Mission, sun_name: str = "Sun", sun_radius_km: float = NOMINAL_SOLAR_RADIUS_KM,
) -> Iterator[SolarSample]:
    """Report the visible solar-disc fraction at the mission's telemetry times."""
    radius = number(sun_radius_km, "sun_radius_km", 1, 1e8)
    position_at = _sun_ephemeris(mission, sun_name)
    for sample in propagate(mission):
        geometry = _disc_geometry(np.array(sample.state.position_km), position_at(sample.time_s), radius)
        fraction = 1.0 if geometry is None else _visible_fraction(*geometry)
        yield SolarSample(sample.time_s, sample.kind, fraction)
