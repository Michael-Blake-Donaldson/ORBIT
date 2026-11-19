"""Adaptive integration, impulsive maneuvers, and streamed telemetry."""

from collections.abc import Iterator
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
from .mission import EARTH_RADIUS_KM, STANDARD_GRAVITY_KM_S2, Impulse, Mission, State

SampleKind = Literal["sample", "pre_burn", "post_burn", "complete", "impact"]
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


def _apply_impulse(state: Array, impulse: Impulse) -> Array:
    delta_v = np.array(impulse.delta_v_km_s)
    if impulse.frame == "RTN":
        position, velocity = state[:3], state[3:]
        radial = position / hypot(*position)
        normal = _cross(radial, velocity)
        norm = hypot(*normal)
        if norm <= 1e-10 * hypot(*velocity):
            raise SimulationError("RTN burn requires a non-radial pre-burn state")
        normal /= norm
        transverse = _cross(normal, radial)
        delta_v = delta_v[0] * radial + delta_v[1] * transverse + delta_v[2] * normal
    result = state.copy()
    result[3:] += delta_v
    return result


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
    """Yield chronological telemetry, with two rows at each applied burn."""
    j2 = mission.gravity == "j2"
    dynamics = _dynamics(j2)
    state = np.array((*mission.initial_state.position_km, *mission.initial_state.velocity_km_s))
    spacecraft = mission.spacecraft
    mass = spacecraft.initial_mass_kg if spacecraft is not None else None
    config = mission.integrator
    absolute_tolerance = [config.position_tolerance_km] * 3 + [config.velocity_tolerance_km_s] * 3
    time = 0.0
    cadence_index = 1
    accepted_steps = 0
    if not mission.impulses or mission.impulses[0].time_s > 0:
        yield _sample(time, "sample", state, j2, mass)

    for impulse in (*mission.impulses, None):
        boundary = impulse.time_s if impulse is not None else mission.duration_s
        if time < boundary:
            solver = DOP853(
                dynamics, time, state, boundary, rtol=config.relative_tolerance,
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
                    yield _sample(cadence, "sample", dense(cadence), j2, mass)
                    cadence_index += 1
                    cadence = cadence_index * mission.sample_interval_s
                if impact is not None and dense is not None:
                    yield _sample(impact, "impact", dense(impact), j2, mass)
                    return
            time, state = solver.t, solver.y

        if impulse is None:
            yield _sample(time, "complete", state, j2, mass)
            return

        # When a burn coincides with the cadence, its two samples replace the regular row.
        if _same_time(cadence_index * mission.sample_interval_s, time):
            cadence_index += 1
        burned_state = _apply_impulse(state, impulse)
        post_burn_mass = mass
        if spacecraft is not None and mass is not None:
            exhaust_velocity = STANDARD_GRAVITY_KM_S2 * spacecraft.isp_s
            post_burn_mass = mass * exp(-hypot(*impulse.delta_v_km_s) / exhaust_velocity)
            if post_burn_mass < spacecraft.dry_mass_kg:
                raise SimulationError(f"burn at t={time:g} s would reduce mass below dry mass")
        yield _sample(time, "pre_burn", state, j2, mass)
        state, mass = burned_state, post_burn_mass
        yield _sample(time, "post_burn", state, j2, mass)
        if time == mission.duration_s:
            yield _sample(time, "complete", state, j2, mass)
            return
