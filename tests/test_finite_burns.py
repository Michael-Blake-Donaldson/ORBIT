import math
import unittest
from pathlib import Path

import numpy as np

from orbit_sim import (
    FiniteBurn,
    Impulse,
    Mission,
    SimulationError,
    Spacecraft,
    State,
    load_mission,
    propagate,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "finite_burn.json"


class FiniteBurnTests(unittest.TestCase):
    def test_constant_eci_thrust_matches_free_space_closed_form(self):
        vehicle = Spacecraft(1000, 900, 320)
        burn = FiniteBurn(0, 60, (0, 1200, 0))
        state = State((1e10, 0, 0), (0, 1, 0))
        timeline = list(propagate(Mission(state, 120, 15, spacecraft=vehicle, finite_burns=(burn,))))
        self.assertEqual(timeline[0].kind, "burn_start")
        self.assertEqual([row.kind for row in timeline if row.time_s == 60], ["burn_end"])
        exhaust_speed_m_s = 9.80665 * 320
        flow = 1200 / exhaust_speed_m_s
        for row in timeline:
            burn_time = min(row.time_s, 60)
            mass = 1000 - flow * burn_time
            log_ratio = math.log(1000 / mass)
            expected_speed = 1 + (exhaust_speed_m_s / 1000) * log_ratio
            expected_y = (
                burn_time + exhaust_speed_m_s / 1000 * (burn_time - mass / flow * log_ratio)
                + (row.time_s - burn_time) * expected_speed
            )
            self.assertAlmostEqual(row.mass_kg, mass, places=10)
            self.assertAlmostEqual(row.state.velocity_km_s[1], expected_speed, places=10)
            self.assertAlmostEqual(row.state.position_km[1], expected_y, places=8)

    def test_radial_rtn_thrust_preserves_angular_momentum(self):
        initial = State((7000, 0, 0), (0, 7, 2))
        mission = Mission(
            initial, 900, 30, spacecraft=Spacecraft(1000, 500, 320),
            finite_burns=(FiniteBurn(100, 700, (300, 0, 0), "RTN"),),
        )
        h0 = np.cross(initial.position_km, initial.velocity_km_s)
        for sample in propagate(mission):
            h = np.cross(sample.state.position_km, sample.state.velocity_km_s)
            np.testing.assert_allclose(h, h0, atol=2e-6, rtol=0)

    def test_mixed_maneuvers_follow_cutoff_impulse_ignition_order(self):
        initial = State((7000, 0, 0), (0, 7.5, 0))
        mission = Mission(
            initial, 100, 25, impulses=(Impulse(50, (0, 0.01, 0)),), spacecraft=Spacecraft(1000, 900, 320),
            finite_burns=(FiniteBurn(0, 50, (0, 100, 0)), FiniteBurn(50, 100, (0, 200, 0))),
        )
        by_time = {}
        for sample in propagate(mission):
            by_time.setdefault(sample.time_s, []).append(sample)
        transition = by_time[50]
        self.assertEqual([row.kind for row in transition], ["burn_end", "pre_burn", "post_burn", "burn_start"])
        self.assertEqual([row.kind for row in by_time[100]], ["burn_end", "complete"])
        self.assertEqual(transition[0].state, transition[1].state)
        self.assertEqual(transition[2].state, transition[3].state)
        self.assertLess(transition[2].mass_kg, transition[1].mass_kg)

    def test_impact_during_active_burn_uses_partial_propellant(self):
        mission = Mission(
            State((6400, 0, 0), (-1, 0, 0)), 100, 5, spacecraft=Spacecraft(1000, 800, 300),
            finite_burns=(FiniteBurn(0, 50, (20, 0, 0)),),
        )
        timeline = list(propagate(mission))
        last = timeline[-1]
        self.assertEqual(last.kind, "impact")
        self.assertLess(last.time_s, 50)
        self.assertNotIn("burn_end", [row.kind for row in timeline])
        self.assertAlmostEqual(last.mass_kg, 1000 - 20 * last.time_s / (9.80665 * 300), places=10)
        self.assertAlmostEqual(last.altitude_km, 0, places=7)

    def test_example_and_dry_mass_guard(self):
        mission = load_mission(EXAMPLE)
        samples = list(propagate(mission))
        end_burn = next(row for row in samples if row.kind == "burn_end")
        self.assertAlmostEqual(end_burn.mass_kg, 1000 - 1500 * 120 / (9.80665 * 320), places=9)
        self.assertGreater(end_burn.elements.semi_major_axis_km, 7300)

        insufficient = Mission(
            mission.initial_state, 100, 50, spacecraft=Spacecraft(100, 99, 300),
            finite_burns=(FiniteBurn(10, 90, (1500, 0, 0)),),
        )
        with self.assertRaisesRegex(SimulationError, "finite burn.*dry mass"):
            list(propagate(insufficient))

    def test_rejects_incompatible_schedules(self):
        initial = State((7000, 0, 0), (0, 7.5, 0))
        burn = FiniteBurn(10, 20, (1, 0, 0))
        vehicle = Spacecraft(100, 50, 300)
        with self.assertRaises(ValueError):
            Mission(initial, 100, 10, finite_burns=(burn,))
        with self.assertRaises(ValueError):
            Mission(initial, 100, 10, spacecraft=vehicle, finite_burns=(burn, FiniteBurn(19, 50, (1, 0, 0))))
        with self.assertRaises(ValueError):
            Mission(initial, 100, 10, spacecraft=vehicle, finite_burns=(burn,), impulses=(Impulse(15, (1, 0, 0)),))
        with self.assertRaises(ValueError):
            FiniteBurn(0, 1, (0, 0, 0))
