import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np

from orbit_sim import (
    KeplerianOrbit,
    Mission,
    load_mission,
    orbital_elements,
    propagate,
    state_from_keplerian,
)

# Reference states from JPL CSPICE conics_c using the same mu and mean anomalies
# converted from the given true anomalies. Units are km and km/s.
# https://naif.jpl.nasa.gov/pub/naif/toolkit_docs/C/cspice/conics_c.html
REFERENCES = (
    (
        KeplerianOrbit(7000, 0.35, 57, 123, 221, 43),
        (3846.296166485030, 1560.048129092582, -6275.624955310288,
         -3.236356371271, 7.377963410415, -2.008122556491),
        (460.549409160620, -7386.372775182225, 5599.961786838543,
         4.576663843225, -0.821282410123, -5.221698731104),
    ),
    (
        KeplerianOrbit(8400, 1.6, 130, 245, 300, -37),
        (6037.886877526894, -1526.216997730452, -7290.184433028999,
         -5.981465574811, -8.420982916846, 2.219268840894),
        (-43810.848537651030, -19404.348916526043, 37546.770140800860,
         -4.515025711744, -0.630829543803, 4.558937620554),
    ),
    (
        KeplerianOrbit(6800, 1, 74, 44, 183),
        (-4816.664440195022, -4787.766967272506, -342.098140385190,
         2.477974897839, -1.750265799387, -10.393826730132),
        (31005.184135194595, 17995.668797609390, -29967.386797046830,
         3.065177829771, 2.433603816050, -1.320556047495),
    ),
)


class KeplerianTests(unittest.TestCase):
    def test_conic_initialization_and_propagation_match_cspice(self):
        for orbit, expected_start, expected_end in REFERENCES:
            with self.subTest(eccentricity=orbit.eccentricity):
                initial = state_from_keplerian(orbit)
                _, final = propagate(Mission(initial, 9000, 9000))
                np.testing.assert_allclose(initial.position_km, expected_start[:3], atol=1e-8, rtol=0)
                np.testing.assert_allclose(initial.velocity_km_s, expected_start[3:], atol=1e-10, rtol=0)
                np.testing.assert_allclose(final.state.position_km, expected_end[:3], atol=3e-7, rtol=0)
                np.testing.assert_allclose(final.state.velocity_km_s, expected_end[3:], atol=5e-10, rtol=0)

    def test_circular_retrograde_initialization_has_undefined_node(self):
        state = state_from_keplerian(KeplerianOrbit(7000, 0, 180, true_anomaly_deg=90))
        np.testing.assert_allclose(state.position_km, (0, -7000, 0), atol=1e-9, rtol=0)
        self.assertLess(state.velocity_km_s[0], 0)
        elements = orbital_elements(state)
        self.assertEqual(elements.inclination_deg, 180)
        self.assertIsNone(elements.raan_deg)

    def test_rejects_out_of_branch_true_anomaly(self):
        for eccentricity, anomaly in ((1, 180), (2, 130), (2, -130)):
            with self.subTest(eccentricity=eccentricity, anomaly=anomaly), self.assertRaisesRegex(
                ValueError, "physical branch",
            ):
                state_from_keplerian(KeplerianOrbit(7000, eccentricity, true_anomaly_deg=anomaly))

    def test_mission_plan_accepts_exactly_one_initialization_format(self):
        path_values = {"schema_version": 1, "duration_s": 30, "sample_interval_s": 10}
        orbit = {"periapsis_radius_km": 7000, "eccentricity": 0.2}
        state = {"position_km": [7000, 0, 0], "velocity_km_s": [0, 8, 0]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mission.json"
            path.write_text(json.dumps(path_values | {"initial_orbit": orbit}), encoding="utf-8")
            self.assertTrue(math.isclose(load_mission(path).initial_state.position_km[0], 7000))
            for initialization in ({}, {"initial_orbit": orbit, "initial_state": state}):
                path.write_text(json.dumps(path_values | initialization), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    load_mission(path)
