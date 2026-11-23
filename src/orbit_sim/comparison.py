"""Compare synchronous spacecraft states in ECI and the reference RTN frame."""

from collections.abc import Iterator
from dataclasses import dataclass, replace
from itertools import zip_longest
from math import hypot

import numpy as np

from .errors import SimulationError
from .mechanics import _rtn_basis
from .mission import Mission, Vector
from .propagation import Sample, SampleKind, _same_time, propagate


@dataclass(frozen=True, slots=True)
class TrajectoryDelta:
    time_s: float
    reference_kind: SampleKind
    candidate_kind: SampleKind
    position_delta_eci_km: Vector
    velocity_delta_eci_km_s: Vector
    position_delta_rtn_km: Vector | None
    velocity_delta_rtn_km_s: Vector | None

    @property
    def position_difference_km(self) -> float:
        return hypot(*self.position_delta_eci_km)

    @property
    def velocity_difference_km_s(self) -> float:
        return hypot(*self.velocity_delta_eci_km_s)


def _grid_samples(mission: Mission) -> Iterator[tuple[float, Sample]]:
    pending = None
    for sample in propagate(mission):
        elapsed = sample.time_s
        target = round(elapsed / mission.sample_interval_s) * mission.sample_interval_s
        if _same_time(elapsed, mission.duration_s):
            target = mission.duration_s
        elif not _same_time(elapsed, target):
            continue
        if pending is not None and target != pending[0]:
            yield pending
        # At coincident timestamps, use the final row after all maneuvers.
        pending = target, sample
    if pending is not None:
        yield pending


def compare_trajectories(
    reference: Mission, candidate: Mission, sample_interval_s: float | None = None,
) -> Iterator[TrajectoryDelta]:
    """Stream candidate minus reference differences on their common elapsed-time grid."""
    interval = _comparison_interval(reference, candidate, sample_interval_s)
    reference = replace(reference, sample_interval_s=interval, track_apsides=False)
    candidate = replace(candidate, sample_interval_s=interval, track_apsides=False)
    for grid_reference, grid_candidate in zip_longest(_grid_samples(reference), _grid_samples(candidate)):
        # Still consume both trajectories so a later propagation failure cannot be hidden by an early impact.
        if grid_reference is None or grid_candidate is None:
            continue
        time, baseline = grid_reference
        candidate_time, comparison = grid_candidate
        if time != candidate_time:
            raise SimulationError("trajectory comparison grids diverged")
        position = np.array(baseline.state.position_km)
        velocity = np.array(baseline.state.velocity_km_s)
        dr = np.array(comparison.state.position_km) - position
        dv = np.array(comparison.state.velocity_km_s) - velocity
        basis = _rtn_basis(position, velocity)
        yield TrajectoryDelta(
            time, baseline.kind, comparison.kind, tuple(map(float, dr)), tuple(map(float, dv)),
            tuple(float(axis @ dr) for axis in basis) if basis is not None else None,
            tuple(float(axis @ dv) for axis in basis) if basis is not None else None,
        )


def _comparison_interval(reference: Mission, candidate: Mission, interval: float | None = None) -> float:
    if reference.duration_s != candidate.duration_s:
        raise ValueError("Trajectory comparison requires equal mission durations.")
    if reference.epoch_jd_tdb != candidate.epoch_jd_tdb:
        raise ValueError("Trajectory comparison requires matching mission epochs.")
    return max(reference.sample_interval_s, candidate.sample_interval_s) if interval is None else interval
