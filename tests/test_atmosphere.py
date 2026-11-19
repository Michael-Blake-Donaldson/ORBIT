import math
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from orbit_sim import (
    AtmosphericDrag,
    FiniteBurn,
    Mission,
    Spacecraft,
    State,
    load_mission,
    propagate,
)
from orbit_sim.mechanics import _drag_acceleration
from orbit_sim.mission import EARTH_MU_KM3_S2 as MU
from orbit_sim.mission import EARTH_RADIUS_KM as RADIUS

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "orbital_decay.json"


class AtmosphereTests(unittest.TestCase):
    def test_drag_magnitude_and_direction_use_rotating_air_and_si_units(self):
        drag = AtmosphericDrag(300, 2e-11, 55, 4, 2.2)
        radius = RADIUS + 400
        rotation_speed_km_s = 0.00007292115 * radius
        air_density = 2e-11 * math.exp(-100 / 55)
        for speed in (7.6, -7.6, rotation_speed_km_s):
            with self.subTest(speed=speed):
                position_velocity = np.array([radius, 0, 0, 0, speed, 0])
                relative_speed_m_s = (speed - rotation_speed_km_s) * 1000
                expected_m_s2 = -air_density * 2.2 * 4 * abs(relative_speed_m_s) * relative_speed_m_s / (2 * 100)
                observed = _drag_acceleration(position_velocity, drag, 100)
                np.testing.assert_allclose(observed, (0, expected_m_s2 / 1000, 0), rtol=1e-12, atol=1e-18)

    def test_circular_decay_matches_first_order_energy_change(self):
        mission = load_mission(EXAMPLE)
        rows = list(propagate(mission))
        radius = RADIUS + 400
        speed = math.sqrt(MU / radius)
        relative_speed_m_s = 1000 * (speed - 0.00007292115 * radius)
        density = 2e-11 * math.exp(-100 / 55)
        deceleration_km_s2 = 0.5 * density * 2.2 * 4 / 100 * relative_speed_m_s**2 / 1000
        expected_axis_loss = 2 * radius**2 / MU * speed * deceleration_km_s2 * mission.duration_s
        actual_axis_loss = radius - rows[-1].elements.semi_major_axis_km
        self.assertLess(abs(actual_axis_loss / expected_axis_loss - 1), 0.03)
        self.assertLess(rows[-1].specific_energy_km2_s2, rows[0].specific_energy_km2_s2)
        self.assertTrue(all(row.mass_kg == 100 for row in rows))

        undamped = replace(mission, drag=None, sample_interval_s=mission.duration_s)
        _, final = propagate(undamped)
        self.assertAlmostEqual(final.elements.semi_major_axis_km, radius, places=6)

    def test_drag_and_finite_burn_apply_during_the_same_segment(self):
        state = State((RADIUS + 400, 0, 0), (0, 7.67, 0))
        vehicle = Spacecraft(100, 30, 300)
        finite_burn = FiniteBurn(0, 30, (0, 3000, 0))
        mission = Mission(state, 30, 10, spacecraft=vehicle, finite_burns=(finite_burn,))
        drag = AtmosphericDrag(400, 1e-9, 55, 4, 2.2)
        with_drag = list(propagate(replace(mission, drag=drag)))
        no_drag = list(propagate(mission))
        self.assertEqual(with_drag[-1].mass_kg, no_drag[-1].mass_kg)
        self.assertLess(with_drag[-1].speed_km_s, no_drag[-1].speed_km_s)
        self.assertEqual([row.kind for row in with_drag], ["burn_start", "sample", "sample", "burn_end", "complete"])

    def test_drag_requires_mass_and_positive_parameters(self):
        state = State((RADIUS + 400, 0, 0), (0, 7.67, 0))
        with self.assertRaisesRegex(ValueError, "requires spacecraft mass"):
            Mission(state, 30, 10, drag=AtmosphericDrag(400, 1e-9, 55, 4))
        with self.assertRaises(ValueError):
            AtmosphericDrag(400, 1e-9, 0, 4)
