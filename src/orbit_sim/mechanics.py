"""Earth gravity and osculating Keplerian elements in the inertial frame."""

from dataclasses import dataclass
from math import atan2, degrees, hypot

import numpy as np
from numpy.typing import NDArray

from .mission import EARTH_J2, EARTH_MU_KM3_S2, EARTH_RADIUS_KM, State

Array = NDArray[np.float64]
_J2_FACTOR = 1.5 * EARTH_J2 * EARTH_MU_KM3_S2 * EARTH_RADIUS_KM**2
_ELEMENT_EPSILON = 1e-10


def _dynamics(include_j2: bool):
    def derivative(_time: float, state: Array) -> Array:
        x, y, z, vx, vy, vz = state
        radius = hypot(x, y, z)
        central = -EARTH_MU_KM3_S2 / radius**3
        equatorial = polar = central
        if include_j2:
            scale = _J2_FACTOR / radius**5
            latitude_term = 5 * (z / radius)**2
            equatorial += scale * (latitude_term - 1)
            polar += scale * (latitude_term - 3)
        return np.array((vx, vy, vz, equatorial * x, equatorial * y, polar * z))

    return derivative


def _specific_energy(state: Array, include_j2: bool) -> float:
    position, velocity = state[:3], state[3:]
    radius = hypot(*position)
    potential = -EARTH_MU_KM3_S2 / radius
    if include_j2:
        legendre_p2 = (3 * (position[2] / radius)**2 - 1) / 2
        potential *= 1 - EARTH_J2 * (EARTH_RADIUS_KM / radius)**2 * legendre_p2
    return float(velocity @ velocity / 2 + potential)


@dataclass(frozen=True, slots=True)
class OrbitalElements:
    semi_major_axis_km: float | None
    eccentricity: float
    inclination_deg: float | None
    raan_deg: float | None
    argument_of_periapsis_deg: float | None
    true_anomaly_deg: float | None


def _cross(left: Array, right: Array) -> Array:
    # These vectors always have three components, so no axis or broadcast handling is needed.
    x, y, z = left
    a, b, c = right
    return np.array((y * c - z * b, z * a - x * c, x * b - y * a))


def _oriented_angle(start: Array, end: Array, normal: Array) -> float:
    # atan2 preserves the quadrant and behaves well near zero and 180 degrees.
    return degrees(atan2(_cross(start, end) @ normal, start @ end)) % 360


def orbital_elements(state: State) -> OrbitalElements:
    """Return two-body osculating elements, with None for singular angles."""
    return _orbital_elements(np.array((*state.position_km, *state.velocity_km_s)))


def _orbital_elements(state: Array) -> OrbitalElements:
    position, velocity = state[:3], state[3:]
    radius = hypot(*position)
    if radius == 0:
        raise ValueError("orbital elements require a nonzero position")
    momentum = _cross(position, velocity)
    momentum_norm = hypot(*momentum)
    eccentricity_vector = _cross(velocity, momentum) / EARTH_MU_KM3_S2 - position / radius
    eccentricity = hypot(*eccentricity_vector)
    inverse_axis = 2 / radius - (velocity @ velocity) / EARTH_MU_KM3_S2
    axis = None if abs(inverse_axis * radius) < _ELEMENT_EPSILON else float(1 / inverse_axis)
    if momentum_norm <= _ELEMENT_EPSILON * radius * hypot(*velocity):
        return OrbitalElements(axis, eccentricity, None, None, None, None)

    normal = momentum / momentum_norm
    node = np.array((-normal[1], normal[0], 0.0))
    inclination = degrees(atan2(hypot(*node), normal[2]))
    inclined = hypot(*node) > _ELEMENT_EPSILON
    eccentric = eccentricity > _ELEMENT_EPSILON
    raan = degrees(atan2(node[1], node[0])) % 360 if inclined else None
    periapsis = _oriented_angle(node, eccentricity_vector, normal) if inclined and eccentric else None
    anomaly = _oriented_angle(eccentricity_vector, position, normal) if eccentric else None
    return OrbitalElements(axis, eccentricity, inclination, raan, periapsis, anomaly)
