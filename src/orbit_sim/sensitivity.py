"""Linearized sensitivity to the initial Cartesian position and velocity."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from math import hypot

import numpy as np
from scipy.integrate import DOP853, DenseOutput

from .ephemeris import _position_interpolant, _relative_geometry
from .errors import SimulationError
from .mechanics import _J2_FACTOR, Array, _cross, _drag_parameters, _rtn_basis
from .mission import (
    EARTH_MU_KM3_S2,
    EARTH_ROTATION_RAD_S,
    AtmosphericDrag,
    Mission,
    Vector,
)
from .propagation import Sample, _bounded_propagate, _ThrustArc
from .uncertainty import Covariance, _validated_covariance

_IDENTITY_3 = np.eye(3)
_Z_AXIS = np.array([0.0, 0.0, 1.0])
_RELATIVE_VELOCITY_POSITION = EARTH_ROTATION_RAD_S * np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 0]])
_STM_ATOL = 1e-12
_MAX_STM_STEPS = 100_000


@dataclass(frozen=True, slots=True)
class StateTransition:
    """Derivative of this sample's Cartesian state with respect to the initial state."""

    sample: Sample
    matrix_eci: tuple[tuple[float, ...], ...]


@dataclass(frozen=True, slots=True)
class CovarianceSample:
    """Covariance of the linearized Cartesian state at a nominal sample epoch."""

    sample: Sample
    covariance_eci: Covariance


def _matrix_tuple(matrix: Array) -> tuple[tuple[float, ...], ...]:
    return tuple(tuple(map(float, row)) for row in matrix)


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


def _cross_matrix(vector: Array) -> Array:
    x, y, z = vector
    return np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])


def _rtn_vector_partials(position: Array, velocity: Array, components: Vector) -> tuple[Array, Array]:
    basis = _rtn_basis(position, velocity)
    if basis is None:
        raise SimulationError("RTN sensitivity requires a non-radial state")
    radial, _, normal = basis
    radial_position = (_IDENTITY_3 - np.outer(radial, radial)) / hypot(*position)
    normal_projection = (_IDENTITY_3 - np.outer(normal, normal)) / hypot(*_cross(radial, velocity))
    normal_position = -normal_projection @ _cross_matrix(velocity) @ radial_position
    normal_velocity = normal_projection @ _cross_matrix(radial)
    dv_radial, dv_transverse, dv_normal = components
    # T = N × R, so dT = -[R]× dN + [N]× dR.
    radial_weight = dv_radial * _IDENTITY_3 + dv_transverse * _cross_matrix(normal)
    normal_weight = dv_normal * _IDENTITY_3 - dv_transverse * _cross_matrix(radial)
    return radial_weight @ radial_position + normal_weight @ normal_position, normal_weight @ normal_velocity


def _drag_partials(state: Array, drag: AtmosphericDrag, mass: float) -> tuple[Array, Array]:
    relative, scale, density_varies = _drag_parameters(state, drag, mass)
    speed = hypot(*relative)
    if speed == 0:
        return np.zeros((3, 3)), np.zeros((3, 3))
    direction = relative / speed
    velocity_partial = (scale * speed) * (_IDENTITY_3 + np.outer(direction, direction))
    position_partial = velocity_partial @ _RELATIVE_VELOCITY_POSITION
    if density_varies:
        position = state[:3]
        density_log_gradient = -position / (drag.scale_height_km * hypot(*position))
        position_partial += np.outer((scale * speed) * relative, density_log_gradient)
    return position_partial, velocity_partial


class _VariationalSolver:
    def __init__(self, mission: Mission) -> None:
        self.gravity_gradient = _gravity_gradient(mission)
        self.drag = mission.drag
        self.spacecraft = mission.spacecraft
        self.mass = self.spacecraft.initial_mass_kg if self.spacecraft is not None else None
        self.burns = {burn.start_s: burn for burn in mission.finite_burns}
        self.arc: _ThrustArc | None = None
        self.rtol = mission.integrator.relative_tolerance
        self.matrix = np.eye(6)
        self.time = 0.0
        self.steps = 0
        self.dense_steps: list[DenseOutput] = []

    def force_boundary(self, sample: Sample) -> None:
        if sample.kind == "burn_start":
            assert self.spacecraft is not None and sample.mass_kg is not None
            self.arc = _ThrustArc.from_burn(self.burns[sample.time_s], sample.mass_kg, self.spacecraft.isp_s)
        elif sample.kind in ("burn_end", "post_burn"):
            self.arc = None
            self.mass = sample.mass_kg

    def acceleration_derivative(self, time: float, state: Array, matrix: Array) -> Array:
        position = state[:3]
        change = self.gravity_gradient(time, position) @ matrix[:3]
        mass = self.arc.mass_at(time) if self.arc is not None else self.mass
        if self.drag is not None and mass is not None:
            position_partial, velocity_partial = _drag_partials(state, self.drag, mass)
            change += position_partial @ matrix[:3] + velocity_partial @ matrix[3:]
        if self.arc is not None and self.arc.burn.frame == "RTN" and mass is not None:
            position_partial, velocity_partial = _rtn_vector_partials(position, state[3:], self.arc.burn.thrust_n)
            change += (position_partial @ matrix[:3] + velocity_partial @ matrix[3:]) / (1000 * mass)
        return change

    def advance(self, start: float, end: float, nominal: DenseOutput) -> None:
        def derivative(time: float, flat: Array) -> Array:
            matrix = flat.reshape(6, 6)
            # A = [[0, I], [da/dr, da/dv]] and dPhi/dt = A Phi.
            return np.vstack((matrix[3:], self.acceleration_derivative(time, nominal(time), matrix))).reshape(-1)

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
    variational = _VariationalSolver(mission)
    rtn_impulses = {burn.time_s: burn.delta_v_km_s for burn in mission.impulses if burn.frame == "RTN"}
    for sample in _bounded_propagate(mission, variational.advance):
        matrix = variational.at(sample.time_s)
        if not np.all(np.isfinite(matrix)):
            raise SimulationError(f"state transition overflowed at t={sample.time_s:g} s")
        yield StateTransition(sample, _matrix_tuple(matrix))
        if sample.kind == "pre_burn" and (delta_v := rtn_impulses.get(sample.time_s)) is not None:
            position = np.asarray(sample.state.position_km)
            velocity = np.asarray(sample.state.velocity_km_s)
            position_partial, velocity_partial = _rtn_vector_partials(position, velocity, delta_v)
            # An RTN command depends on the pre-burn state. An inertial command has identity jump.
            transition = variational.matrix
            transition[3:] += position_partial @ transition[:3] + velocity_partial @ transition[3:]
        variational.force_boundary(sample)


def propagate_covariance(mission: Mission, initial_covariance_eci: Covariance) -> Iterator[CovarianceSample]:
    """Transport an initial ECI covariance through the linearized trajectory."""
    _, factor = _validated_covariance(initial_covariance_eci)
    for row in propagate_state_transition(mission):
        # P = Phi P0 Phi.T = (Phi L)(Phi L).T with P0 = L L.T, also for singular P0.
        transported = np.asarray(row.matrix_eci) @ factor
        covariance = transported @ transported.T
        if not np.all(np.isfinite(covariance)):
            raise SimulationError(f"covariance overflowed at t={row.sample.time_s:g} s")
        yield CovarianceSample(row.sample, _matrix_tuple(covariance))
