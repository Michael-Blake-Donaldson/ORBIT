import csv
import json
import math
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from orbit_sim import (
    Impulse,
    Mission,
    SimulationError,
    Spacecraft,
    State,
    compare_trajectories,
    load_mission,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
INITIAL = State((7000, 0, 0), (0, 7.5, 0))


class ComparisonTests(unittest.TestCase):
    def test_common_grid_handles_non_dividing_duration_and_equal_trajectories(self):
        reference = Mission(INITIAL, 100, 15)
        candidate = replace(reference, sample_interval_s=30)
        rows = list(compare_trajectories(reference, candidate))
        self.assertEqual([row.time_s for row in rows], [0, 30, 60, 90, 100])
        self.assertTrue(all(row.position_difference_km == row.velocity_difference_km_s == 0 for row in rows))

    def test_reference_rotation_projects_signed_position_and_velocity_deltas(self):
        reference = Mission(State((7000, 0, 0), (0, 0, 8)), 100, 30)
        candidate = replace(reference, initial_state=State((7001, 2, 3), (1, 2, 11)))
        row = next(compare_trajectories(reference, candidate))
        self.assertEqual(row.position_delta_eci_km, (1, 2, 3))
        self.assertEqual(row.velocity_delta_eci_km_s, (1, 2, 3))
        self.assertEqual(row.position_delta_rtn_km, (1, 3, -2))
        self.assertEqual(row.velocity_delta_rtn_km_s, (1, 3, -2))
        self.assertAlmostEqual(row.position_difference_km, math.sqrt(14))

    def test_shared_timestamp_compares_the_post_burn_state(self):
        reference = Mission(INITIAL, 37, 15)
        candidate = replace(reference, impulses=(Impulse(15, (0, 0.125, 0)),))
        rows = list(compare_trajectories(reference, candidate))
        self.assertEqual([row.time_s for row in rows], [0, 15, 30, 37])
        burn = rows[1]
        self.assertEqual(burn.candidate_kind, "post_burn")
        self.assertLess(burn.position_difference_km, 1e-8)
        np.testing.assert_allclose(burn.velocity_delta_eci_km_s, (0, 0.125, 0), rtol=0, atol=1e-12)

    def test_impact_limits_overlap_and_singular_rtn_fields_are_blank(self):
        reference = Mission(State((6400, 0, 0), (-1, 0, 0)), 100, 5)
        candidate = Mission(INITIAL, 100, 5)
        rows = list(compare_trajectories(reference, candidate))
        self.assertEqual([row.time_s for row in rows], [0, 5, 10, 15])
        self.assertIsNone(rows[0].position_delta_rtn_km)
        self.assertIsNone(rows[0].velocity_delta_rtn_km_s)
        invalid = replace(candidate, spacecraft=Spacecraft(100, 99, 300), impulses=(Impulse(50, (1, 0, 0)),))
        with self.assertRaises(SimulationError):
            list(compare_trajectories(reference, invalid))

    def test_duration_and_epoch_must_align(self):
        mission = Mission(INITIAL, 100, 5, epoch_jd_tdb=2460676.5)
        with self.assertRaisesRegex(ValueError, "durations"):
            list(compare_trajectories(mission, replace(mission, duration_s=101)))
        with self.assertRaisesRegex(ValueError, "epochs"):
            list(compare_trajectories(mission, replace(mission, epoch_jd_tdb=2460676.6)))

    def test_cli_reports_a_comparison_cut_short_by_impact(self):
        with tempfile.TemporaryDirectory(prefix="orbit compare impact ") as directory:
            root = Path(directory)
            reference, candidate, output = (root / name for name in ("reference.json", "candidate.json", "delta.csv"))
            plan = {"schema_version": 1, "duration_s": 100, "sample_interval_s": 5}
            reference.write_text(json.dumps(plan | {
                "initial_state": {"position_km": [6400, 0, 0], "velocity_km_s": [-1, 0, 0]},
            }), encoding="utf-8")
            candidate.write_text(json.dumps(plan | {
                "initial_state": {"position_km": [7000, 0, 0], "velocity_km_s": [0, 7.5, 0]},
            }), encoding="utf-8")
            command = [
                sys.executable, "-I", "-m", "orbit_sim.compare", str(reference), str(candidate), str(output),
            ]
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=root, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Compared 4 states through 15 s", result.stdout)
            self.assertIn("Surface impact limited the comparison", result.stdout)
            self.assertIn("planned end was 100 s", result.stdout)
            self.assertTrue(output.is_file())

    def test_lunar_csv_matches_known_peak_and_final_state_differences(self):
        reference = EXAMPLES / "lunar_baseline.json"
        candidate = EXAMPLES / "lunar_perturbation.json"
        self.assertGreater(len(load_mission(candidate).third_bodies), len(load_mission(reference).third_bodies))
        with tempfile.TemporaryDirectory(prefix="orbit compare & ") as directory:
            output = Path(directory) / "delta.csv"
            command = [
                sys.executable, "-I", "-m", "orbit_sim.compare", str(reference), str(candidate), str(output),
            ]
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            with output.open(encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 721)
        self.assertEqual(float(rows[-1]["time_s"]), 86_400)
        self.assertAlmostEqual(float(rows[-1]["position_difference_km"]), 12.142288848, places=6)
        self.assertAlmostEqual(max(float(row["position_difference_km"]) for row in rows), 13.2033, places=4)
