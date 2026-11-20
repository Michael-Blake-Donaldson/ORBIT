"""Solar array generation and piecewise-linear battery energy accounting."""

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from math import ceil, hypot

import numpy as np
from scipy.integrate import DenseOutput

from .errors import SimulationError
from .illumination import (
    NOMINAL_SOLAR_RADIUS_KM,
    _disc_geometry,
    _sun_ephemeris,
    _visible_fraction,
    eclipse_windows,
)
from .mechanics import Array
from .mission import MAX_SAMPLES, Mission, PowerSystem, number
from .propagation import _propagate

_AU_KM = 149_597_870.7
_SECONDS_PER_HOUR = 3600


@dataclass(frozen=True, slots=True)
class PowerSample:
    time_s: float
    solar_w: float
    load_w: float
    battery_wh: float
    generated_wh: float
    demand_wh: float
    curtailed_wh: float
    unserved_wh: float
    battery_losses_wh: float


@dataclass(slots=True)
class _Battery:
    charge_wh: float
    curtailed_wh: float = 0.0
    unserved_wh: float = 0.0
    losses_wh: float = 0.0

    def apply(self, net_wh: float, system: PowerSystem) -> None:
        if net_wh >= 0:
            stored = min(system.battery_capacity_wh - self.charge_wh, system.charge_efficiency * net_wh)
            accepted = min(net_wh, stored / system.charge_efficiency)
            self.charge_wh = min(system.battery_capacity_wh, self.charge_wh + stored)
            self.curtailed_wh += net_wh - accepted
            self.losses_wh += accepted - stored
        else:
            draw = min(self.charge_wh, -net_wh / system.discharge_efficiency)
            delivered = min(-net_wh, draw * system.discharge_efficiency)
            self.charge_wh -= draw
            self.unserved_wh += -net_wh - delivered
            self.losses_wh += draw - delivered


def battery_budget(samples: Iterable[tuple[float, float]], system: PowerSystem) -> Iterator[PowerSample]:
    """Integrate ordered (elapsed seconds, available solar watts) at constant load."""
    battery = _Battery(system.initial_charge_wh)
    previous_time = previous_solar = generated = 0.0
    for index, (raw_time, raw_solar) in enumerate(samples):
        time = number(raw_time, "power sample time_s", 0, 2_592_000)
        solar = number(raw_solar, "solar_w", 0, 1e26)
        if (index == 0 and time != 0) or (index > 0 and time <= previous_time):
            raise ValueError("power samples must start at zero and increase strictly")
        hours = (time - previous_time) / _SECONDS_PER_HOUR
        if index > 0:
            generated += 0.5 * (previous_solar + solar) * hours
            left, right = previous_solar - system.load_w, solar - system.load_w
            if left * right < 0:
                # Capacity clipping is order-dependent when the power changes sign.
                first = hours * abs(left) / (abs(left) + abs(right))
                battery.apply(0.5 * left * first, system)
                battery.apply(0.5 * right * (hours - first), system)
            else:
                battery.apply(0.5 * (left + right) * hours, system)
        yield PowerSample(
            time, solar, system.load_w, battery.charge_wh, generated, system.load_w * time / _SECONDS_PER_HOUR,
            battery.curtailed_wh, battery.unserved_wh, battery.losses_wh,
        )
        previous_time, previous_solar = time, solar


def _orbital_power_samples(mission: Mission, system: PowerSystem) -> Iterator[tuple[float, float]]:
    interval = system.analysis_interval_s
    if ceil(mission.duration_s / interval) + 1 > MAX_SAMPLES:
        raise ValueError("power analysis interval exceeds the telemetry row budget")
    position_at = _sun_ephemeris(mission, system.sun_name)
    checkpoints = set()
    for window in eclipse_windows(mission, system.sun_name):
        if window.kind == "penumbra":
            pieces = max(2, ceil(window.duration_s / min(interval, 1.0)))
            if len(checkpoints) + pieces > MAX_SAMPLES:
                raise SimulationError("eclipse checkpoints exceed the power analysis row limit")
            checkpoints.update(window.start_s + window.duration_s * index / pieces for index in range(pieces))
        checkpoints.update((window.start_s, window.end_s))
    knots = sorted(checkpoints)
    knot_index = 0
    count = 1
    pending: deque[tuple[float, float]] = deque()

    def available(time: float, position: Array) -> float:
        sun = position_at(time)
        geometry = _disc_geometry(position, sun, NOMINAL_SOLAR_RADIUS_KM)
        exposure = 1.0 if geometry is None else _visible_fraction(*geometry, projected=True)
        return system.array_power_w_at_1_au * (_AU_KM / hypot(*(sun - position)))**2 * exposure

    def observe(start: float, end: float, dense: DenseOutput) -> None:
        nonlocal count, knot_index
        while knot_index < len(knots) and knots[knot_index] <= start:
            knot_index += 1
        left = start
        while left < end:
            target = knots[knot_index] if knot_index < len(knots) else end
            right = min(end, target, left + interval)
            if right == target and knot_index < len(knots):
                knot_index += 1
            count += 1
            if count > MAX_SAMPLES:
                raise SimulationError("power analysis exceeds the telemetry row limit")
            pending.append((right, available(right, dense(right)[:3])))
            left = right

    yield 0.0, available(0.0, np.array(mission.initial_state.position_km))
    diagnostic = replace(mission, sample_interval_s=120.0, track_apsides=False)
    for _ in _propagate(diagnostic, observe):
        while pending:
            yield pending.popleft()


def mission_power_budget(mission: Mission) -> Iterator[PowerSample]:
    """Estimate available solar power and battery charge through a complete mission."""
    system = mission.power_system
    if system is None:
        raise ValueError("mission has no power_system configuration")
    return battery_budget(_orbital_power_samples(mission, system), system)
