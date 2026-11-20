"""Ephemeris interpolation and Earth-relative point-mass gravity."""

from collections.abc import Callable
from math import hypot

from scipy.interpolate import CubicHermiteSpline

from .errors import SimulationError
from .mechanics import Array
from .mission import EARTH_RADIUS_KM, ThirdBody


def _position_interpolant(body: ThirdBody) -> CubicHermiteSpline:
    return CubicHermiteSpline(
        [row.time_s for row in body.ephemeris],
        [row.position_km for row in body.ephemeris],
        [row.velocity_km_s for row in body.ephemeris],
        extrapolate=False,
    )


def _relative_acceleration(position: Array, body_position: Array, body: ThirdBody) -> Array:
    earth_distance = hypot(*body_position)
    if not earth_distance > EARTH_RADIUS_KM:
        raise SimulationError(f"ephemeris for {body.name} is outside its domain or intersects Earth")
    displacement = body_position - position
    distance = hypot(*displacement)
    if distance <= body.minimum_separation_km:
        raise SimulationError(f"spacecraft entered the force exclusion radius of {body.name}")

    mu = body.mu_km3_s2
    if hypot(*position) >= earth_distance / 2:
        return (mu / distance**3) * displacement - (mu / earth_distance**3) * body_position

    # Battin's form preserves the small tidal residual without subtracting near-equal forces.
    q = float(position @ (position - 2 * body_position)) / earth_distance**2
    residual = q * (3 + q * (3 + q)) / (1 + (1 + q)**1.5)
    return (-mu / distance**3) * (position + residual * body_position)


def _with_third_bodies(
    gravity: Callable[[float, Array], Array], bodies: tuple[ThirdBody, ...],
) -> Callable[[float, Array], Array]:
    if not bodies:
        return gravity
    fields = tuple((body, _position_interpolant(body)) for body in bodies)

    def combined(time: float, state: Array) -> Array:
        derivative = gravity(time, state)
        for body, position_at in fields:
            derivative[3:] += _relative_acceleration(state[:3], position_at(time), body)
        return derivative

    return combined
