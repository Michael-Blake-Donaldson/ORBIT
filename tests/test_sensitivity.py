import csv
import math
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from orbit_sim import (
    AtmosphericDrag,
    FiniteBurn,
    Impulse,
    Mission,
    Spacecraft,
    State,
    load_initial_uncertainty,
    load_mission,
    propagate,
    propagate_covariance,
    propagate_state_transition,
)
from orbit_sim.mechanics import _drag_acceleration
from orbit_sim.mission import EARTH_MU_KM3_S2, EARTH_RADIUS_KM, EARTH_ROTATION_RAD_S
from orbit_sim.sensitivity import _drag_partials

_INITIAL = State((7000, 0, 0), (0, 7.5, 0))
_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _vector(state):
    return np.asarray((*state.position_km, *state.velocity_km_s))


def _centered_state_difference(mission, perturbation):
    nominal = _vector(mission.initial_state)
    endpoints = []
    for delta in (perturbation, -perturbation):
        initial = nominal + delta
        mission_variant = replace(
            mission, initial_state=State(tuple(map(float, initial[:3])), tuple(map(float, initial[3:]))),
        )
        endpoints.append(_vector(list(propagate(mission_variant))[-1].state))
    return (endpoints[0] - endpoints[1]) / 2


class StateTransitionTests(unittest.TestCase):
    def test_circular_orbit_matches_closed_form_clohessy_wiltshire(self):
        radius = 7000
        frequency = math.sqrt(EARTH_MU_KM3_S2 / radius**3)
        initial = State((radius, 0, 0), (0, radius * frequency, 0))
        duration = 1620
        mission = Mission(initial, duration, 73)
        transitions = list(propagate_state_transition(mission))
        self.assertEqual([row.sample for row in transitions], list(propagate(mission)))
        np.testing.assert_array_equal(transitions[0].matrix_eci, np.eye(6))

        angle = frequency * duration
        sine, cosine = math.sin(angle), math.cos(angle)
        cw = np.array([
            [4 - 3 * cosine, 0, 0, sine / frequency, 2 * (1 - cosine) / frequency, 0],
            [6 * (sine - angle), 1, 0, -2 * (1 - cosine) / frequency, (4 * sine - 3 * angle) / frequency, 0],
            [0, 0, cosine, 0, 0, sine / frequency],
            [3 * frequency * sine, 0, 0, cosine, 2 * sine, 0],
            [6 * frequency * (cosine - 1), 0, 0, -2 * sine, 4 * cosine - 3, 0],
            [0, 0, -frequency * sine, 0, 0, cosine],
        ])
        rotation = np.array([[cosine, -sine, 0], [sine, cosine, 0], [0, 0, 1]])
        omega = frequency * np.array([[0, -1, 0], [1, 0, 0], [0, 0, 0]])
        start_rtn_to_eci = np.block([[np.eye(3), np.zeros((3, 3))], [omega, np.eye(3)]])
        end_rtn_to_eci = np.block([[rotation, np.zeros((3, 3))], [rotation @ omega, rotation]])
        actual = np.linalg.solve(end_rtn_to_eci, np.asarray(transitions[-1].matrix_eci) @ start_rtn_to_eci)
        np.testing.assert_allclose(actual, cw, rtol=1e-8, atol=3e-8)

    def test_j2_and_moon_match_central_differences(self):
        mission = load_mission(_EXAMPLES / "lunar_perturbation.json")
        mission = replace(mission, gravity="j2", sample_interval_s=mission.duration_s)
        matrix = np.asarray(list(propagate_state_transition(mission))[-1].matrix_eci)
        steps = (0.03, 0.03, 0.03, 1e-6, 1e-6, 1e-6)
        for column, step in enumerate(steps):
            offset = step * np.eye(6)[column]
            expected = _centered_state_difference(mission, offset)
            np.testing.assert_allclose(matrix @ offset, expected, rtol=1e-5, atol=8e-9)

    def test_fixed_inertial_velocity_jump_preserves_instantaneous_derivative(self):
        mission = Mission(_INITIAL, 100, 15, impulses=(Impulse(48.5, (0, 0.2, 0)),))
        rows = list(propagate_state_transition(mission))
        before, after = [row for row in rows if row.sample.kind in ("pre_burn", "post_burn")]
        self.assertEqual(before.matrix_eci, after.matrix_eci)
        self.assertNotEqual(before.sample.state.velocity_km_s, after.sample.state.velocity_km_s)
        offset = np.array([0.01, -0.03, 0.04, 1e-5, 5e-5, -2e-5])
        np.testing.assert_allclose(
            np.asarray(rows[-1].matrix_eci) @ offset, _centered_state_difference(mission, offset),
            rtol=1e-6, atol=2e-9,
        )

    def test_surface_impact_retains_sensitivity_at_nominal_impact_epoch(self):
        mission = Mission(State((6400, 0, 0), (-1, 0, 0)), 100, 5)
        terminal = list(propagate_state_transition(mission))[-1]
        self.assertEqual(terminal.sample.kind, "impact")
        self.assertTrue(np.all(np.isfinite(terminal.matrix_eci)))
        self.assertAlmostEqual(np.linalg.det(terminal.matrix_eci), 1.0, places=10)

    def test_rtn_impulses_include_rotation_of_the_commanded_vector(self):
        mission = Mission(
            State((7000, 125, -230), (-0.15, 7.5, 0.8)), 700, 25,
            impulses=(
                Impulse(0, (0.03, -0.1, 0.24), frame="RTN"),
                Impulse(200, (-0.15, 0.07, -0.12), frame="RTN"),
                Impulse(700, (0.02, 0.03, -0.05), frame="RTN"),
            ),
        )
        rows = list(propagate_state_transition(mission))
        np.testing.assert_array_equal(rows[0].matrix_eci, np.eye(6))
        self.assertNotEqual(rows[0].matrix_eci, rows[1].matrix_eci)
        self.assertNotEqual(rows[-3].matrix_eci, rows[-2].matrix_eci)
        self.assertEqual(rows[-2].matrix_eci, rows[-1].matrix_eci)
        for column, step in enumerate((0.03, 0.03, 0.03, 2e-5, 2e-5, 2e-5)):
            offset = step * np.eye(6)[column]
            np.testing.assert_allclose(
                np.asarray(rows[-1].matrix_eci) @ offset, _centered_state_difference(mission, offset),
                rtol=1e-6, atol=2e-9,
            )

    def test_drag_partials_match_force_on_either_side_of_density_cap(self):
        drag = AtmosphericDrag(300, 1e-9, 55, 3, 2.2)
        mass = 50
        for altitude in (250, 350):
            with self.subTest(altitude=altitude):
                state = np.array([EARTH_RADIUS_KM + altitude, 135, -54, 0.73, 7.25, -0.44])
                gradient = np.hstack(_drag_partials(state, drag, mass))
                steps = (0.001, 0.001, 0.001, 1e-5, 1e-5, 1e-5)
                for column, step in enumerate(steps):
                    offset = step * np.eye(6)[column]
                    difference = _drag_acceleration(state + offset, drag, mass) - _drag_acceleration(
                        state - offset, drag, mass,
                    )
                    np.testing.assert_allclose(gradient[:, column], difference / (2 * step), rtol=1e-6, atol=2e-16)

        radius = EARTH_RADIUS_KM + 315
        co_rotating = np.array([radius, 0, 0, 0, EARTH_ROTATION_RAD_S * radius, 0])
        np.testing.assert_array_equal(np.hstack(_drag_partials(co_rotating, drag, mass)), np.zeros((3, 6)))

    def test_mixed_finite_burns_and_drag_follow_mass_and_force_switches(self):
        mission = Mission(
            State((6740, 80, 100), (-0.05, 7.65, 0.65)), 1250, 31,
            gravity="j2", drag=AtmosphericDrag(325, 3e-9, 60, 8, 2.3),
            spacecraft=Spacecraft(600, 300, 320),
            finite_burns=(
                FiniteBurn(0, 350, (25, 90, -30), "ECI"),
                FiniteBurn(350, 650, (-20, 180, 75), "RTN"),
            ),
            impulses=(Impulse(350, (0.02, 0.12, -0.05), frame="RTN"),),
        )
        rows = list(propagate_state_transition(mission))
        self.assertEqual([row.sample for row in rows], list(propagate(mission)))
        boundary = [row for row in rows if row.sample.time_s == 350]
        self.assertEqual([row.sample.kind for row in boundary], ["burn_end", "pre_burn", "post_burn", "burn_start"])
        self.assertEqual(boundary[0].matrix_eci, boundary[1].matrix_eci)
        self.assertEqual(boundary[2].matrix_eci, boundary[3].matrix_eci)
        self.assertNotEqual(boundary[1].matrix_eci, boundary[2].matrix_eci)
        self.assertEqual(rows[-1].sample.kind, "complete")
        terminal_matrix = np.asarray(rows[-1].matrix_eci)
        for column, step in enumerate((0.015, 0.015, 0.015, 2e-5, 2e-5, 2e-5)):
            offset = step * np.eye(6)[column]
            np.testing.assert_allclose(
                terminal_matrix @ offset, _centered_state_difference(mission, offset), rtol=2e-6, atol=5e-9,
            )

    def test_rank_one_covariance_follows_two_independent_hohmann_trajectories(self):
        mission = load_mission(_EXAMPLES / "hohmann.json")
        offset = np.array([0.02, -0.022, 0.029, 1e-5, 1.5e-5, -2.4e-5])
        initial = np.outer(offset, offset)
        rows = list(propagate_covariance(mission, initial.tolist()))
        np.testing.assert_allclose(rows[0].covariance_eci, initial, rtol=1e-14, atol=1e-20)
        self.assertEqual(rows[0].sample.kind, "pre_burn")
        self.assertEqual(rows[1].sample.kind, "post_burn")
        self.assertNotEqual(rows[1].covariance_eci, rows[0].covariance_eci)
        expected = _centered_state_difference(mission, offset)
        np.testing.assert_allclose(rows[-1].covariance_eci, np.outer(expected, expected), rtol=1e-6, atol=2e-12)

    def test_zero_covariance_and_invalid_covariance(self):
        mission = Mission(_INITIAL, 10, 5)
        zero = np.zeros((6, 6))
        for row in propagate_covariance(mission, zero.tolist()):
            np.testing.assert_array_equal(row.covariance_eci, zero)
        invalid = np.eye(6)
        invalid[1, 2] = invalid[2, 1] = 1.001
        with self.assertRaisesRegex(ValueError, "positive semidefinite"):
            next(propagate_covariance(mission, invalid.tolist()))

    def test_covariance_csv_preserves_burn_boundaries_and_cross_terms(self):
        plan, config = _EXAMPLES / "hohmann.json", _EXAMPLES / "initial_dispersion.json"
        with tempfile.TemporaryDirectory(prefix="orbit covariance & ") as directory:
            output = Path(directory) / "hohmann-covariance.csv"
            command = [
                sys.executable, "-I", "-m", "orbit_sim.covariance_timeline", str(plan), str(config), str(output),
            ]
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            with output.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
        observations = list(propagate_covariance(load_mission(plan), load_initial_uncertainty(config).covariance_eci))
        self.assertEqual(len(rows), len(observations))
        self.assertEqual(len(rows[-1]), 4 + 21)
        self.assertEqual([(row["time_s"], row["kind"]) for row in rows[:2]], [("0.0", "pre_burn"), ("0.0", "post_burn")])
        self.assertEqual(float(rows[-1]["cov_vx_km_s__y_km"]), observations[-1].covariance_eci[3][1])
        expected_rms = np.sqrt(np.trace(np.asarray(observations[-1].covariance_eci)[:3, :3]))
        self.assertAlmostEqual(float(rows[-1]["position_rms_km"]), expected_rms, places=12)
