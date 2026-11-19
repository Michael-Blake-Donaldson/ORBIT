import math
import unittest
from pathlib import Path
from unittest.mock import patch

from orbit_sim import (
    KeplerianOrbit,
    Mission,
    SimulationError,
    State,
    load_mission,
    propagate,
    state_from_keplerian,
)
from orbit_sim.mission import EARTH_MU_KM3_S2 as MU

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "apsides.json"


class EventTests(unittest.TestCase):
    def test_apsides_are_localized_independently_of_sample_cadence(self):
        mission = load_mission(EXAMPLE)
        rows = list(propagate(mission))
        self.assertEqual(sorted(row.time_s for row in rows), [row.time_s for row in rows])
        apoapsis, periapsis = [row for row in rows if row.kind in {"apoapsis", "periapsis"}]
        axis = 7000 / (1 - 0.2)
        period = math.tau * math.sqrt(axis**3 / MU)
        self.assertEqual(apoapsis.kind, "apoapsis")
        self.assertEqual(periapsis.kind, "periapsis")
        self.assertAlmostEqual(apoapsis.time_s, period / 2, places=6)
        self.assertAlmostEqual(periapsis.time_s, period, places=6)
        self.assertAlmostEqual(math.hypot(*apoapsis.state.position_km), axis * 1.2, places=6)
        self.assertAlmostEqual(math.hypot(*periapsis.state.position_km), 7000, places=6)

    def test_circular_orbit_has_no_numerical_apsides(self):
        state = state_from_keplerian(KeplerianOrbit(7000, 0))
        rows = list(propagate(Mission(state, 24000, 600, track_apsides=True)))
        self.assertTrue(all(row.kind in {"sample", "complete"} for row in rows))

    def test_reports_apoapsis_before_an_impact_with_sparse_sampling(self):
        state = State((6378.139, 0, 0), (0.004, 0, 0))
        rows = list(propagate(Mission(state, 10, 10, track_apsides=True)))
        self.assertEqual([row.kind for row in rows], ["sample", "apoapsis", "impact"])
        self.assertLess(rows[1].time_s, rows[2].time_s)
        self.assertGreater(rows[1].altitude_km, rows[0].altitude_km)
        self.assertAlmostEqual(rows[-1].altitude_km, 0, places=7)

    def test_event_rows_are_subject_to_the_telemetry_budget(self):
        mission = load_mission(EXAMPLE)
        with patch("orbit_sim.propagation.MAX_SAMPLES", 35), self.assertRaisesRegex(
            SimulationError, "telemetry row budget",
        ):
            list(propagate(mission))
