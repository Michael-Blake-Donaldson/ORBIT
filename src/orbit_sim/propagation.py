"""Adaptive integration, spacecraft maneuvers, and streamed telemetry."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from math import exp, hypot, ulp
from typing import Literal

import numpy as np
from scipy.integrate import DOP853, DenseOutput
from scipy.optimize import brentq

from .ephemeris import _with_third_bodies
from .errors import SimulationError
from .mechanics import (
    Array,
    OrbitalElements,
    _altitude,
    _drag_acceleration,
    _dynamics,
    _orbital_elements,
    _radial_motion,
    _rtn_basis,
    _specific_energy,
)
from .mission import (
    EARTH_MU_KM3_S2,
    MAX_SAMPLES,
    STANDARD_GRAVITY_KM_S2,
    AtmosphericDrag,
    FiniteBurn,
    Impulse,
    Mission,
    State,
)

_Apsis = Literal["periapsis", "apoapsis"]
_StepEvent = tuple[float, Literal["impact"] | _Apsis]
_StepObserver = Callable[[float, float, DenseOutput], None]
SampleKind = Literal[
    "sample", "pre_burn", "post_burn", "burn_start", "burn_end", "periapsis", "apoapsis", "complete", "impact",
]
_MAX_ACCEPTED_STEPS = 100_000


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


def _segment_dynamics(
    gravity: Callable[[float, Array], Array], drag: AtmosphericDrag | None, coast_mass: float | None,
    arc: _ThrustArc | None,
) -> Callable[[float, Array], Array]:
    if coast_mass is None or (drag is None and arc is None):
        return gravity
    newtons = np.array(arc.burn.thrust_n) if arc is not None else None

    def accelerated(time: float, state: Array) -> Array:
        derivative = gravity(time, state)
        mass = coast_mass if arc is None else arc.mass_at(time)
        if drag is not None:
            derivative[3:] += _drag_acceleration(state, drag, mass)
        if arc is not None and newtons is not None:
            thrust = _inertial_vector(newtons, arc.burn.frame, state)
            # Newtons divided by kilograms give m/s². The integrator uses kilometres.
            derivative[3:] += thrust / (1000 * mass)
        return derivative

    return accelerated


def _schedule(mission: Mission) -> dict[float, _Boundary]:
    boundaries = {mission.duration_s: _Boundary()}
    # Restart DOP853 where an ephemeris changes polynomial, without adding a telemetry event.
    for body in mission.third_bodies:
        for point in body.ephemeris[1:]:
            if point.time_s < mission.duration_s:
                boundaries.setdefault(point.time_s, _Boundary())
    for burn in mission.finite_burns:
        boundaries.setdefault(burn.start_s, _Boundary()).ignition = burn
        boundaries.setdefault(burn.end_s, _Boundary()).cutoff = True
    for impulse in mission.impulses:
        boundaries.setdefault(impulse.time_s, _Boundary()).impulse = impulse
    return boundaries


def _step_events(
    start: float,
    initial: Array,
    end: float,
    final: Array,
    dense: DenseOutput,
    crossed_surface: bool,
    apsis: _Apsis | None,
    report_apsides: bool,
    dynamics: Callable[[float, Array], Array],
) -> tuple[_StepEvent, ...]:
    def state_at(time: float) -> Array:
        # Use the accepted states to preserve the root bracket at floating-point endpoints.
        if time == start:
            return initial
        if time == end:
            return final
        return dense(time)

    def altitude_at(time: float) -> float:
        return _altitude(state_at(time))

    if crossed_surface and apsis != "apoapsis":
        return ((brentq(altitude_at, start, end, xtol=1e-9), "impact"),)

    # Check the in-step periapsis for a shallow crossing with both endpoints above ground.
    if apsis is None:
        return ()
    turn_time = brentq(lambda time: _radial_motion(state_at(time)), start, end, xtol=1e-9)
    if apsis == "periapsis" and altitude_at(turn_time) <= 0:
        return ((brentq(altitude_at, start, turn_time, xtol=1e-9), "impact"),)
    events: tuple[_StepEvent, ...] = ()
    if report_apsides and _distinct_apsis(turn_time, state_at(turn_time), dynamics):
        events = ((turn_time, apsis),)
    if crossed_surface:
        # A short outbound coast can turn around and hit Earth during one accepted step.
        events += ((brentq(altitude_at, turn_time, end, xtol=1e-9), "impact"),)
    return events


def _distinct_apsis(time: float, state: Array, dynamics: Callable[[float, Array], Array]) -> bool:
    position, velocity = state[:3], state[3:]
    radius = hypot(*position)
    acceleration = dynamics(time, state)[3:]
    # At a radial turning point, r·v is zero. Compare radial curvature with local gravity.
    curvature = abs(float(velocity @ velocity + position @ acceleration)) / radius
    return curvature > 1e-8 * EARTH_MU_KM3_S2 / radius**2


def _inertial_vector(components: Array, frame: str, state: Array) -> Array:
    if frame == "ECI":
        return components
    basis = _rtn_basis(state[:3], state[3:])
    if basis is None:
        raise SimulationError("RTN burn requires a non-radial state")
    radial, transverse, normal = basis
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
    for count, sample in enumerate(_propagate(mission), start=1):
        if count > MAX_SAMPLES:
            raise SimulationError(f"mission exceeds the {MAX_SAMPLES} telemetry row budget")
        yield sample


def _propagate(mission: Mission, observe_step: _StepObserver | None = None) -> Iterator[Sample]:
    j2 = mission.gravity == "j2"
    dynamics = _with_third_bodies(_dynamics(j2), mission.third_bodies)
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
        visible_boundary = (
            boundary == mission.duration_s or events.cutoff or events.impulse is not None or events.ignition is not None
        )
        if time < boundary:
            report_apsides = mission.track_apsides and arc is None
            segment_dynamics = _segment_dynamics(dynamics, mission.drag, mass, arc)
            solver = DOP853(
                segment_dynamics, time, state, boundary,
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
                step_events = ()
                crossed_surface = _altitude(solver.y) <= 0
                initial_motion, final_motion = _radial_motion(initial), _radial_motion(solver.y)
                apsis = None
                if initial_motion < 0 <= final_motion:
                    apsis = "periapsis"
                elif report_apsides and initial_motion > 0 >= final_motion:
                    apsis = "apoapsis"
                if crossed_surface or apsis is not None:
                    dense = solver.dense_output()
                    step_events = _step_events(
                        start, initial, solver.t, solver.y, dense, crossed_surface, apsis, report_apsides,
                        segment_dynamics,
                    )
                if observe_step is not None:
                    end = step_events[-1][0] if step_events and step_events[-1][1] == "impact" else solver.t
                    if end > start:
                        if dense is None:
                            dense = solver.dense_output()
                        observe_step(float(start), float(end), dense)
                event_index = 0
                while True:
                    cadence = cadence_index * mission.sample_interval_s
                    if event_index < len(step_events) and dense is not None:
                        event_time, kind = step_events[event_index]
                        if kind != "impact" and (
                            _same_time(event_time, time) or (visible_boundary and _same_time(event_time, boundary))
                        ):
                            event_index += 1
                            continue
                        coincident = _same_time(cadence, event_time)
                        if cadence >= event_time or coincident:
                            current_mass = mass if arc is None else arc.mass_at(event_time)
                            yield _sample(event_time, kind, dense(event_time), j2, current_mass)
                            if kind == "impact":
                                return
                            cadence_index += int(coincident)
                            event_index += 1
                            continue
                    if cadence > solver.t or (
                        visible_boundary and (cadence >= boundary or _same_time(cadence, boundary))
                    ):
                        break
                    if dense is None:
                        dense = solver.dense_output()
                    current_mass = mass if arc is None else arc.mass_at(cadence)
                    yield _sample(cadence, "sample", dense(cadence), j2, current_mass)
                    cadence_index += 1
            time, state = solver.t, solver.y

        # Boundary events replace a regular sample at the same timestamp.
        if visible_boundary and _same_time(cadence_index * mission.sample_interval_s, time):
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
