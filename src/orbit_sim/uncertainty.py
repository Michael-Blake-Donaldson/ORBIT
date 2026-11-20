"""Seeded initial-state dispersion and final Cartesian covariance statistics."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
from typing import Literal

import numpy as np

from .errors import SimulationError
from .mechanics import Array
from .mission import Mission, State, number
from .propagation import propagate

Covariance = tuple[tuple[float, ...], ...]
_STATE_DIMENSION = 6
_CORRELATION_TOLERANCE = 1e-12
MAX_TRIALS = 256


def _covariance_factor(matrix: Array) -> Array:
    variance = np.diag(matrix)
    if np.any(variance < 0):
        raise ValueError("initial covariance variances must be nonnegative")
    if not np.allclose(matrix, matrix.T, rtol=_CORRELATION_TOLERANCE, atol=0):
        raise ValueError("initial covariance must be symmetric")
    unperturbed = variance == 0
    if np.any(matrix[unperturbed, :] != 0):
        raise ValueError("a zero-variance state component cannot have nonzero covariance")

    # Validate definiteness in correlation space so velocity scales are not masked
    # by the much larger position variances.
    deviations = np.sqrt(variance)
    safe = np.where(unperturbed, 1, deviations)
    with np.errstate(over="ignore", invalid="ignore"):
        correlation = matrix / safe[:, None] / safe[None, :]
    if np.any(np.abs(correlation) > 1 + _CORRELATION_TOLERANCE):
        raise ValueError("initial covariance is not positive semidefinite")
    values, vectors = np.linalg.eigh(0.5 * (correlation + correlation.T))
    if values[0] < -_CORRELATION_TOLERANCE:
        raise ValueError("initial covariance is not positive semidefinite")
    root = (vectors * np.sqrt(np.maximum(values, 0))) @ vectors.T
    return deviations[:, None] * root


@dataclass(frozen=True, slots=True)
class InitialUncertainty:
    """An ECI covariance over x, y, z, vx, vy, vz, in km and km/s."""

    covariance_eci: Covariance
    trials: int = 32
    seed: int = 0
    _factor: Array = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.trials) is not int or not 1 <= self.trials <= MAX_TRIALS:
            raise ValueError(f"trials must be an integer in [1, {MAX_TRIALS}]")
        if type(self.seed) is not int or not 0 <= self.seed < 2**64:
            raise ValueError("seed must be an unsigned 64-bit integer")
        if not isinstance(self.covariance_eci, (list, tuple)) or len(self.covariance_eci) != _STATE_DIMENSION:
            raise ValueError("initial covariance must be a 6 by 6 matrix")
        rows = []
        for row in self.covariance_eci:
            if not isinstance(row, (list, tuple)) or len(row) != _STATE_DIMENSION:
                raise ValueError("initial covariance must be a 6 by 6 matrix")
            rows.append(tuple(number(value, "covariance_eci", -1e12, 1e12) for value in row))
        immutable = tuple(rows)
        factor = _covariance_factor(np.array(immutable))
        factor.flags.writeable = False
        object.__setattr__(self, "covariance_eci", immutable)
        object.__setattr__(self, "_factor", factor)


@dataclass(frozen=True, slots=True)
class TrialOutcome:
    trial: int
    initial_state: State
    elapsed_s: float
    outcome: Literal["complete", "impact"]
    final_state: State


@dataclass(frozen=True, slots=True)
class EnsembleSummary:
    completed: int
    impacted: int
    final_time_s: float | None
    final_state_mean: State | None
    final_state_covariance_eci: Covariance | None


def _state_vector(state: State) -> Array:
    return np.array((*state.position_km, *state.velocity_km_s))


def _from_vector(vector: Array) -> State:
    return State(tuple(map(float, vector[:3])), tuple(map(float, vector[3:])))


def run_ensemble(mission: Mission, uncertainty: InitialUncertainty) -> Iterator[TrialOutcome]:
    """Propagate independent, seeded initial-state perturbations."""
    rng = np.random.Generator(np.random.PCG64(uncertainty.seed))
    nominal = _state_vector(mission.initial_state)
    configured = replace(mission, sample_interval_s=mission.duration_s, track_apsides=False)
    for trial in range(1, uncertainty.trials + 1):
        terminal = None
        try:
            vec = nominal + uncertainty._factor @ rng.standard_normal(_STATE_DIMENSION)
            initial = _from_vector(vec)
            for terminal in propagate(replace(configured, initial_state=initial)):
                pass
        except (ValueError, SimulationError) as exc:
            raise SimulationError(f"dispersion trial {trial} failed: {exc}") from exc
        if terminal is None or terminal.kind not in ("complete", "impact"):
            raise SimulationError(f"dispersion trial {trial} finished without a terminal state")
        yield TrialOutcome(trial, initial, terminal.time_s, terminal.kind, terminal.state)


def summarize_ensemble(outcomes: Iterable[TrialOutcome]) -> EnsembleSummary:
    """Estimate sample covariance only for trials that reached the mission end time."""
    complete = impacted = 0
    final_time = None
    mean = np.zeros(_STATE_DIMENSION)
    scatter = np.zeros((_STATE_DIMENSION, _STATE_DIMENSION))
    for result in outcomes:
        if result.outcome == "impact":
            impacted += 1
            continue
        if final_time is None:
            final_time = result.elapsed_s
        elif final_time != result.elapsed_s:
            raise ValueError("completed trials must share a final time to estimate covariance")
        complete += 1
        state = _state_vector(result.final_state)
        delta = state - mean
        mean += delta / complete
        scatter += np.outer(delta, state - mean)
    position = _from_vector(mean) if complete else None
    covariance = None
    if complete >= 2:
        estimate = (scatter + scatter.T) / (2 * (complete - 1))
        covariance = tuple(tuple(map(float, row)) for row in estimate)
    return EnsembleSummary(complete, impacted, final_time, position, covariance)
