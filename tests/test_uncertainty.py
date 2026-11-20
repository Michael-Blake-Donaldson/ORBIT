import csv
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from orbit_sim import (
    InitialUncertainty,
    Mission,
    SimulationError,
    State,
    TrialOutcome,
    propagate,
    run_ensemble,
    summarize_ensemble,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def diagonal(values):
    return tuple(tuple(value if i == j else 0 for j in range(6)) for i, value in enumerate(values))


class UncertaintyTests(unittest.TestCase):
    def test_zero_dispersion_reproduces_nominal_mission_exactly(self):
        mission = Mission(State((7000, 0, 0), (0, 7.5, 0)), 120, 20)
        model = InitialUncertainty(diagonal([0] * 6), trials=3, seed=9)
        nominal = list(propagate(mission))[-1].state
        trials = list(run_ensemble(mission, model))
        self.assertTrue(all(result.final_state == nominal for result in trials))
        report = summarize_ensemble(trials)
        self.assertEqual(report.completed, 3)
        self.assertEqual(report.final_time_s, 120)
        self.assertEqual(report.final_state_mean, nominal)
        self.assertEqual(report.final_state_covariance_eci, diagonal([0] * 6))

    def test_rank_one_gaussian_preserves_correlation_and_seed(self):
        mission = Mission(State((7000, 0, 0), (0, 7.5, 0)), 20, 10)
        deviation = np.array([1, 0, 0, 0.001, 0, 0])
        covariance = tuple(tuple(map(float, row)) for row in np.outer(deviation, deviation))
        model = InitialUncertainty(covariance, trials=5, seed=23)
        trials = list(run_ensemble(mission, model))
        self.assertEqual(trials, list(run_ensemble(mission, model)))
        self.assertNotEqual(trials, list(run_ensemble(mission, replace(model, seed=24))))
        self.assertTrue(any(result.initial_state != mission.initial_state for result in trials))
        for trial in trials:
            self.assertAlmostEqual(
                trial.initial_state.velocity_km_s[0], (trial.initial_state.position_km[0] - 7000) * 0.001, places=14,
            )
            self.assertEqual(trial.initial_state.position_km[1:], (0, 0))

    def test_rejects_invalid_covariance_even_when_position_variance_is_large(self):
        variance = list(map(list, diagonal([1e10, 1, 1, 1e-10, 1e-10, 1e-10])))
        variance[3][4] = variance[4][3] = 2e-10
        with self.assertRaisesRegex(ValueError, "positive semidefinite"):
            InitialUncertainty(variance)
        variance[4][3] = 1e-10
        with self.assertRaisesRegex(ValueError, "symmetric"):
            InitialUncertainty(variance)
        with self.assertRaisesRegex(ValueError, "seed"):
            InitialUncertainty(diagonal([0] * 6), seed=-1)

    def test_covariance_matches_batch_estimate_and_excludes_impact(self):
        initial = State((7000, 0, 0), (0, 7.5, 0))
        states = np.array([[7001, 2, 0, 0, 7.49, 0], [7002, -1, 0, 0.01, 7.51, 0], [6999, 1, 0, 0, 7.5, 0]])
        outcomes = [
            TrialOutcome(index, initial, 120, "complete", State(tuple(map(float, row[:3])), tuple(map(float, row[3:]))))
            for index, row in enumerate(states, 1)
        ]
        impact = TrialOutcome(4, initial, 60, "impact", State((6378.137, 0, 0), (0, 0, 0)))
        summary = summarize_ensemble([*outcomes, impact])
        self.assertEqual((summary.completed, summary.impacted, summary.final_time_s), (3, 1, 120))
        self.assertIsNone(summarize_ensemble([outcomes[0], impact]).final_state_covariance_eci)
        self.assertIsNone(summarize_ensemble([impact]).final_state_mean)
        np.testing.assert_allclose(summary.final_state_covariance_eci, np.cov(states, rowvar=False), atol=1e-12)
        with self.assertRaisesRegex(ValueError, "share a final time"):
            summarize_ensemble([outcomes[0], replace(outcomes[1], elapsed_s=100)])

    def test_invalid_initial_draw_is_identified_and_never_resampled(self):
        initial = State((6378.137001, 0, 0), (0, 7.5, 0))
        mission = Mission(initial, 60, 10)
        uncertainty = InitialUncertainty(diagonal([1] + [0] * 5), seed=4)
        with self.assertRaisesRegex(SimulationError, "trial 1 failed"):
            next(run_ensemble(mission, uncertainty))

    def test_propagated_covariance_matches_free_space_state_transition(self):
        state = State((8e9, 0, 0), (0, 1, 0))
        mission = Mission(state, 300, 300)
        model = InitialUncertainty(diagonal([0, 0.04, 0, 0, 4e-8, 0]), trials=20, seed=8)
        results = list(run_ensemble(mission, model))
        initial = np.array([
            (row.initial_state.position_km[1], row.initial_state.velocity_km_s[1]) for row in results
        ])
        transition = np.array([[1, 300], [0, 1]])
        reference = transition @ np.cov(initial, rowvar=False) @ transition.T
        estimate = np.array(summarize_ensemble(results).final_state_covariance_eci)
        np.testing.assert_allclose(estimate[np.ix_([1, 4], [1, 4])], reference, rtol=2e-10, atol=1e-14)

    def test_cli_summary_matches_trial_csv_and_rejects_duplicate_inputs(self):
        values = json.loads((EXAMPLES / "initial_dispersion.json").read_text(encoding="utf-8"))
        values["trials"] = 3
        with tempfile.TemporaryDirectory(prefix="orbit ensemble & ") as directory:
            location = Path(directory)
            config, summary, trials = (location / name for name in ("config.json", "summary.json", "trials.csv"))
            config.write_text(json.dumps(values), encoding="utf-8")
            command = [
                sys.executable, "-I", "-m", "orbit_sim.ensemble", str(EXAMPLES / "hohmann.json"), str(config),
            ]
            for path, format_ in ((summary, "json"), (trials, "csv")):
                result = subprocess.run(
                    [*command, str(path), "--format", format_],
                    check=False, capture_output=True, text=True, cwd=directory, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(summary.read_text(encoding="utf-8"))
            with trials.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            states = [[float(row[f"final_{name}"]) for name in report["state_order"]] for row in rows]
            self.assertEqual(report["completed"], 3)
            self.assertEqual(report["impacted"], 0)
            np.testing.assert_allclose(report["final_state_covariance_eci"], np.cov(states, rowvar=False), atol=1e-11)
            config.write_text('{"trials":1,"trials":2}', encoding="utf-8")
            rejected = location / "rejected.json"
            result = subprocess.run(
                [*command, str(rejected)], check=False, capture_output=True, text=True, cwd=directory, timeout=15,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("duplicate JSON property", result.stderr)
            self.assertFalse(rejected.exists())
