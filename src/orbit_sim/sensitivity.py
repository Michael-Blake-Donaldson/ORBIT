"""Linearized sensitivity to the initial Cartesian position and velocity."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from math import hypot

import numpy as np
from scipy.integrate import DOP853, DenseOutput

from .ephemeris import _position_interpolant, _relative_geometry
from .errors import SimulationError
from .mechanics import _J2_FACTOR, Array
from .mission import EARTH_MU_KM3_S2, Mission
from .propagation import Sample, _bounded_propagate

_IDENTITY_3 = np.eye(3)
_Z_AXIS = np.array([0.0, 0.0, 1.0])
_STM_ATOL = 1e-12
_MAX_STM_STEPS = 100_000


@dataclass(frozen=True, slots=True)
class StateTransition:
    """Derivative of this sample's Cartesian state with respect to the initial state."""

    sample: Sample
    matrix_eci: tuple[tuple[float, ...], ...]


def _point_mass_gradient(displacement: Array, distance: float, mu: float) -> Array:
    unit = displacement / distance
    return (mu / distance**3) * (3 * np.outer(unit, unit) - _IDENTITY_3)


def _j2_gradient(position: Array) -> Array:
    radius = hypot(*position)
    unit = position / radius
    latitude = unit[2]
    factors = 5 * latitude**2 - np.array([1.0, 1.0, 3.0])
    # Differentiate r^-5, the latitude term, and each Cartesian multiplier.
    latitude_gradient = (10 * latitude / radius) * (_Z_AXIS - latitude * unit)
    return (_J2_FACTOR / radius**5) * (
        np.diag(factors) + np.outer(position, latitude_gradient) - 5 * np.outer(factors * unit, unit)
    )


def _gravity_gradient(mission: Mission) -> Callable[[float, Array], Array]:
    include_j2 = mission.gravity == "j2"
    fields = tuple((body, _position_interpolant(body)) for body in mission.third_bodies)

    def gradient(time: float, position: Array) -> Array:
        result = _point_mass_gradient(position, hypot(*position), EARTH_MU_KM3_S2)
        if include_j2:
            result += _j2_gradient(position)
        for body, position_at in fields:
            # The indirect acceleration of Earth is independent of spacecraft state.
            displacement, distance, _ = _relative_geometry(position, position_at(time), body)
            result += _point_mass_gradient(displacement, distance, body.mu_km3_s2)
        return result

    return gradient


class _VariationalSolver:
    def __init__(self, mission: Mission) -> None:
        self.gravity_gradient = _gravity_gradient(mission)
        self.rtol = mission.integrator.relative_tolerance
        self.matrix = np.eye(6)
        self.time = 0.0
        self.steps = 0
        self.dense_steps: list[DenseOutput] = []

    def advance(self, start: float, end: float, nominal: DenseOutput) -> None:
        def derivative(time: float, flat: Array) -> Array:
            matrix = flat.reshape(6, 6)
            gravity = self.gravity_gradient(time, nominal(time)[:3])
            # A = [[0, I], [da/dr, 0]] and dPhi/dt = A Phi.
            return np.vstack((matrix[3:], gravity @ matrix[:3])).reshape(-1)

        # Integrate over one accepted nominal step. This keeps dense interpolation
        # bounded to a single trajectory step and restarts at force model knots.
        solver = DOP853(
            derivative, start, self.matrix.reshape(-1), end,
            first_step=end - start, rtol=self.rtol, atol=_STM_ATOL,
        )
        self.dense_steps.clear()
        while solver.status == "running":
            if self.steps >= _MAX_STM_STEPS:
                raise SimulationError("state transition integration exceeded the accepted step limit")
            message = solver.step()
            if solver.status == "failed":
                raise SimulationError(f"state transition integration failed near t={solver.t:g} s: {message}")
            self.steps += 1
            self.dense_steps.append(solver.dense_output())
        self.matrix = solver.y.reshape(6, 6)
        self.time = end

    def at(self, time: float) -> Array:
        if time == self.time:
            return self.matrix
        for dense in self.dense_steps:
            if dense.t_old <= time <= dense.t:
                return dense(time).reshape(6, 6)
        raise SimulationError(f"state transition interpolation has no state at t={time:g} s")


def propagate_state_transition(mission: Mission) -> Iterator[StateTransition]:
    """Stream fixed-epoch ECI sensitivities in the order x, y, z, vx, vy, vz."""
    if mission.drag is not None or mission.finite_burns or any(burn.frame != "ECI" for burn in mission.impulses):
        raise ValueError("state transition propagation supports conservative gravity and fixed ECI impulses")
    variational = _VariationalSolver(mission)
    for sample in _bounded_propagate(mission, variational.advance):
        # A prescribed inertial delta-v has identity Jacobian across the impulse.
        matrix = tuple(tuple(map(float, row)) for row in variational.at(sample.time_s))
        yield StateTransition(sample, matrix)
