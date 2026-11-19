"""Adaptive integration, spacecraft maneuvers, and streamed telemetry."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from math import exp, hypot, ulp
from typing import Literal

import numpy as np
from scipy.integrate import DOP853, DenseOutput
from scipy.optimize import brentq

from .mechanics import (
    Array,
    OrbitalElements,
    _cross,
    _dynamics,
    _orbital_elements,
    _specific_energy,
)
from .mission import (
    EARTH_RADIUS_KM,
    STANDARD_GRAVITY_KM_S2,
    FiniteBurn,
    Impulse,
    Mission,
    State,
)

SampleKind = Literal["sample", "pre_burn", "post_burn", "burn_start", "burn_end", "complete", "impact"]
_MAX_ACCEPTED_STEPS = 100_000


class SimulationError(RuntimeError):
    """The mission could not be propagated as specified."""


@dataclass(frozen=True, slots=True)
class Sample:
    time_s: float
    kind: SampleKind
    state: State
    altitude_km: float
    speed_km_s: float
    specific_energy_km2_s2: float
    elements: OrbitalElements
    mass_kg: float | None


@dataclass(slots=True)
class _Boundary:
    cutoff: bool = False
    impulse: Impulse | None = None
    ignition: FiniteBurn | None = None


@dataclass(frozen=True, slots=True)
class _ThrustArc:
    burn: FiniteBurn
    initial_mass_kg: float
    flow_kg_s: float

    def mass_at(self, time: float) -> float:
        return self.initial_mass_kg - self.flow_kg_s * (time - self.burn.start_s)

    def dynamics(self, gravity: Callable[[float, Array], Array]) -> Callable[[float, Array], Array]:
        newtons = np.array(self.burn.thrust_n)

        def accelerated(time: float, state: Array) -> Array:
            derivative = gravity(time, state)
            thrust = _inertial_vector(newtons, self.burn.frame, state)
            # Newtons divided by kilograms give m/s². The integrator uses kilometres.
            derivative[3:] += thrust / (1000 * self.mass_at(time))
            return derivative

        return accelerated


def _schedule(mission: Mission) -> dict[float, _Boundary]:
    boundaries = {mission.duration_s: _Boundary()}
    for burn in mission.finite_burns:
        boundaries.setdefault(burn.start_s, _Boundary()).ignition = burn
        boundaries.setdefault(burn.end_s, _Boundary()).cutoff = True
    for impulse in mission.impulses:
        boundaries.setdefault(impulse.time_s, _Boundary()).impulse = impulse
    return boundaries


def _altitude(state: Array) -> float:
    return hypot(*state[:3]) - EARTH_RADIUS_KM


def _radial_motion(state: Array) -> float:
    return float(state[:3] @ state[3:])


def _impact_time(
    start: float, initial: Array, end: float, final: Array, dense: DenseOutput, crossed_surface: bool,
) -> float | None:
    def state_at(time: float) -> Array:
        # Use the accepted states to preserve the root bracket at floating-point endpoints.
        if time == start:
            return initial
        if time == end:
            return final
        return dense(time)

    def altitude_at(time: float) -> float:
        return _altitude(state_at(time))

    if crossed_surface:
        return brentq(altitude_at, start, end, xtol=1e-9)

    # Check the in-step periapsis for a shallow crossing with both endpoints above ground.
    periapsis_time = brentq(lambda time: _radial_motion(state_at(time)), start, end, xtol=1e-9)
    if altitude_at(periapsis_time) <= 0:
        return brentq(altitude_at, start, periapsis_time, xtol=1e-9)
    return None


def _inertial_vector(components: Array, frame: str, state: Array) -> Array:
    if frame == "ECI":
        return components
    position, velocity = state[:3], state[3:]
    radial = position / hypot(*position)
    normal = _cross(radial, velocity)
    norm = hypot(*normal)
    if norm <= 1e-10 * hypot(*velocity):
        raise SimulationError("RTN burn requires a non-radial state")
    normal /= norm
    transverse = _cross(normal, radial)
    return components[0] * radial + components[1] * transverse + components[2] * normal


def _sample(time: float, kind: SampleKind, state: Array, j2: bool, mass: float | None) -> Sample:
    return Sample(
        time,
        kind,
        State(tuple(map(float, state[:3])), tuple(map(float, state[3:]))),
        _altitude(state),
        hypot(*state[3:]),
        _specific_energy(state, j2),
        _orbital_elements(state),
        mass,
    )


def _same_time(left: float, right: float) -> bool:
    return abs(left - right) <= 4 * max(ulp(left), ulp(right))


def propagate(mission: Mission) -> Iterator[Sample]:
    """Yield chronological trajectory samples and exact maneuver boundary rows."""
    j2 = mission.gravity == "j2"
    dynamics = _dynamics(j2)
    state = np.array((*mission.initial_state.position_km, *mission.initial_state.velocity_km_s))
    spacecraft = mission.spacecraft
    mass = spacecraft.initial_mass_kg if spacecraft is not None else None
    config = mission.integrator
    absolute_tolerance = [config.position_tolerance_km] * 3 + [config.velocity_tolerance_km_s] * 3
    boundaries = _schedule(mission)
    arc = None
    time = 0.0
    cadence_index = 1
    accepted_steps = 0
    if 0.0 not in boundaries:
        yield _sample(time, "sample", state, j2, mass)

    for boundary, events in sorted(boundaries.items()):
        if time < boundary:
            solver = DOP853(
                dynamics if arc is None else arc.dynamics(dynamics), time, state, boundary,
                rtol=config.relative_tolerance,
                atol=absolute_tolerance, max_step=config.max_step_s,
            )
            while solver.status == "running":
                if accepted_steps >= _MAX_ACCEPTED_STEPS:
                    raise SimulationError("adaptive integration exceeded the accepted step limit")
                start, initial = solver.t, solver.y
                # Keep fast inbound trajectories from stepping through the gravity singularity.
                transit_limit = 0.5 * hypot(*initial[:3]) / max(hypot(*initial[3:]), 1e-12)
                solver.max_step = min(config.max_step_s, transit_limit)
                message = solver.step()
                if solver.status == "failed":
                    raise SimulationError(f"integration failed near t={start:g} s: {message}")
                accepted_steps += 1
                dense = None
                impact = None
                crossed_surface = _altitude(solver.y) <= 0
                if crossed_surface or _radial_motion(initial) < 0 < _radial_motion(solver.y):
                    dense = solver.dense_output()
                    impact = _impact_time(start, initial, solver.t, solver.y, dense, crossed_surface)
                hard_stop = impact if impact is not None else boundary
                cadence = cadence_index * mission.sample_interval_s
                while cadence <= solver.t and cadence < hard_stop and not _same_time(cadence, hard_stop):
                    if dense is None:
                        dense = solver.dense_output()
                    current_mass = mass if arc is None else arc.mass_at(cadence)
                    yield _sample(cadence, "sample", dense(cadence), j2, current_mass)
                    cadence_index += 1
                    cadence = cadence_index * mission.sample_interval_s
                if impact is not None and dense is not None:
                    current_mass = mass if arc is None else arc.mass_at(impact)
                    yield _sample(impact, "impact", dense(impact), j2, current_mass)
                    return
            time, state = solver.t, solver.y

        # Boundary events replace a regular sample at the same timestamp.
        if _same_time(cadence_index * mission.sample_interval_s, time):
            cadence_index += 1
        if events.cutoff and arc is not None:
            mass = arc.mass_at(time)
            arc = None
            yield _sample(time, "burn_end", state, j2, mass)

        impulse = events.impulse
        if impulse is not None:
            delta_v = _inertial_vector(np.array(impulse.delta_v_km_s), impulse.frame, state)
            post_burn_mass = mass
            if spacecraft is not None and mass is not None:
                exhaust_velocity = STANDARD_GRAVITY_KM_S2 * spacecraft.isp_s
                post_burn_mass = mass * exp(-hypot(*impulse.delta_v_km_s) / exhaust_velocity)
                if post_burn_mass < spacecraft.dry_mass_kg:
                    raise SimulationError(f"burn at t={time:g} s would reduce mass below dry mass")
            yield _sample(time, "pre_burn", state, j2, mass)
            state[3:] += delta_v
            mass = post_burn_mass
            yield _sample(time, "post_burn", state, j2, mass)

        burn = events.ignition
        if burn is not None and spacecraft is not None and mass is not None:
            flow = hypot(*burn.thrust_n) / (1000 * STANDARD_GRAVITY_KM_S2 * spacecraft.isp_s)
            arc = _ThrustArc(burn, mass, flow)
            if arc.mass_at(burn.end_s) < spacecraft.dry_mass_kg:
                raise SimulationError(f"finite burn at t={time:g} s would reduce mass below dry mass")
            yield _sample(time, "burn_start", state, j2, mass)

        if time == mission.duration_s:
            yield _sample(time, "complete", state, j2, mass)
            return
