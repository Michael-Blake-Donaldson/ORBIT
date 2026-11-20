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
class _Ledger:
    charge_wh: float
    generated_wh: float = 0.0
    demand_wh: float = 0.0
    curtailed_wh: float = 0.0
    unserved_wh: float = 0.0
    losses_wh: float = 0.0

    def _apply(self, left_w: float, right_w: float, hours: float, system: PowerSystem) -> None:
        net_wh = 0.5 * (left_w + right_w) * hours
        if left_w >= 0 and right_w >= 0:
            chargeable = min(net_wh, _bounded_energy(left_w, right_w, hours, system.max_charge_w))
            room = system.battery_capacity_wh - self.charge_wh
            accepted = min(chargeable, room / system.charge_efficiency)
            stored = min(room, system.charge_efficiency * accepted)
            self.charge_wh = min(system.battery_capacity_wh, self.charge_wh + stored)
            self.curtailed_wh += net_wh - accepted
            self.losses_wh += accepted - stored
        else:
            supply = min(-net_wh, _bounded_energy(-left_w, -right_w, hours, system.max_discharge_w))
            delivered = min(supply, self.charge_wh * system.discharge_efficiency)
            draw = min(self.charge_wh, delivered / system.discharge_efficiency)
            self.charge_wh -= draw
            self.unserved_wh += -net_wh - delivered
            self.losses_wh += draw - delivered

    def advance(self, start_w: float, end_w: float, seconds: float, load_w: float, system: PowerSystem) -> None:
        hours = seconds / _SECONDS_PER_HOUR
        self.generated_wh += 0.5 * (start_w + end_w) * hours
        self.demand_wh += load_w * hours
        left, right = start_w - load_w, end_w - load_w
        if left < 0 < right or right < 0 < left:
            # Capacity clipping is order-dependent when power changes sign.
            first = hours * abs(left) / (abs(left) + abs(right))
            self._apply(left, 0, first, system)
            self._apply(0, right, hours - first, system)
        else:
            self._apply(left, right, hours, system)

    def sample(self, time: float, solar: float, load: float) -> PowerSample:
        return PowerSample(
            time, solar, load, self.charge_wh, self.generated_wh, self.demand_wh,
            self.curtailed_wh, self.unserved_wh, self.losses_wh,
        )


def _bounded_energy(left: float, right: float, hours: float, limit: float | None) -> float:
    """Integrate a nonnegative linear power segment clipped at the bus rate."""
    if limit is None or max(left, right) <= limit:
        return 0.5 * (left + right) * hours
    low, high = min(left, right), max(left, right)
    if low >= limit:
        return limit * hours
    fraction = (limit - low) / (high - low)
    return (0.5 * (low + limit) * fraction + limit * (1 - fraction)) * hours


def battery_budget(samples: Iterable[tuple[float, float]], system: PowerSystem) -> Iterator[PowerSample]:
    """Integrate ordered (elapsed seconds, available solar watts) and scheduled demand."""
    ledger = _Ledger(system.initial_charge_wh)
    previous_time = previous_solar = 0.0
    load = system.load_w
    changes = iter(system.load_changes)
    change = next(changes, None)
    for index, (raw_time, raw_solar) in enumerate(samples):
        time = number(raw_time, "power sample time_s", 0, 2_592_000)
        solar = number(raw_solar, "solar_w", 0, 1e26)
        if (index == 0 and time != 0) or (index > 0 and time <= previous_time):
            raise ValueError("power samples must start at zero and increase strictly")
        if index == 0:
            previous_solar = solar
            yield ledger.sample(time, solar, load)
            continue
        start, start_solar = previous_time, previous_solar
        while change is not None and change.time_s <= time:
            elapsed_fraction = (change.time_s - previous_time) / (time - previous_time)
            boundary_solar = (1 - elapsed_fraction) * previous_solar + elapsed_fraction * solar
            ledger.advance(start_solar, boundary_solar, change.time_s - start, load, system)
            start, start_solar = change.time_s, boundary_solar
            load = change.load_w
            yield ledger.sample(start, start_solar, load)
            change = next(changes, None)
        if start < time:
            ledger.advance(start_solar, solar, time - start, load, system)
            yield ledger.sample(time, solar, load)
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
    for count, sample in enumerate(battery_budget(_orbital_power_samples(mission, system), system), 1):
        if count > MAX_SAMPLES:
            raise SimulationError("load changes exceed the power analysis row limit")
        yield sample
