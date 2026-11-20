import unittest
from dataclasses import replace

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
