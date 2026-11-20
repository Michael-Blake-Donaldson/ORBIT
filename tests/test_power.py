import csv
import math
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from orbit_sim import (
    EphemerisPoint,
    Mission,
    PowerSystem,
    State,
    ThirdBody,
    battery_budget,
    load_mission,
    mission_power_budget,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "solar_eclipses.json"
_AU_KM = 149_597_870.7


class PowerTests(unittest.TestCase):
    def assert_balanced(self, row, initial_charge):
        delivered = row.demand_wh - row.unserved_wh
        consumed = row.battery_wh + delivered + row.curtailed_wh + row.battery_losses_wh
        self.assertTrue(math.isclose(row.generated_wh + initial_charge, consumed, rel_tol=1e-12, abs_tol=1e-10))

    def test_chronological_sign_changes_account_for_full_and_empty_storage(self):
        system = PowerSystem(200, 100, 10, 10)
        _, rising = battery_budget([(0, 0), (3600, 200)], system)
        _, falling = battery_budget([(0, 200), (3600, 0)], system)
        self.assertEqual((rising.battery_wh, rising.curtailed_wh, rising.unserved_wh), (10, 15, 15))
        self.assertEqual((falling.battery_wh, falling.curtailed_wh, falling.unserved_wh), (0, 25, 15))
        self.assert_balanced(rising, 10)
        self.assert_balanced(falling, 10)

    def test_separate_efficiencies_conserve_energy_in_each_interval(self):
        system = PowerSystem(100, 50, 100, 0, charge_efficiency=0.8, discharge_efficiency=0.5)
        samples = list(battery_budget([(0, 100), (3600, 100), (7200, 0), (10_800, 0)], system))
        self.assertEqual(samples[1].battery_wh, 40)
        self.assertEqual(samples[1].battery_losses_wh, 10)
        final = samples[-1]
        self.assertEqual(final.battery_wh, 0)
        self.assertEqual(final.unserved_wh, 37.5)
        for row in samples:
            self.assert_balanced(row, 0)

    def test_rejects_invalid_config_and_nonmonotonic_samples(self):
        with self.assertRaises(ValueError):
            PowerSystem(200, 100, 10, 11)
        system = PowerSystem(200, 100, 10, 10)
        with self.assertRaises(ValueError):
            list(battery_budget([(10, 200)], system))
        with self.assertRaises(ValueError):
            list(battery_budget([(0, 200), (0, 100)], system))

    def test_solar_power_scales_with_spacecraft_to_sun_distance(self):
        state = State((7000, 0, 0), (0, 7.5, 0))
        points = tuple(EphemerisPoint(t, (2 * _AU_KM + 7000, 0, 0), (0, 0, 0)) for t in (0, 10))
        sun = ThirdBody("Sun", 1e-12, points)
        mission = Mission(state, 10, 10, third_bodies=(sun,), power_system=PowerSystem(200, 20, 1, 0))
        self.assertEqual(next(mission_power_budget(mission)).solar_w, 50)

    def test_eclipse_budget_is_independent_of_trajectory_telemetry_cadence(self):
        mission = load_mission(EXAMPLE)
        coarse = replace(mission, sample_interval_s=mission.duration_s)
        rows = list(mission_power_budget(coarse))
        self.assertEqual(rows, list(mission_power_budget(mission)))
        self.assertTrue(any(row.solar_w == 0 for row in rows))
        self.assertTrue(any(0 < row.solar_w < 100 for row in rows))
        self.assertTrue(any(row.battery_wh == 0 for row in rows))
        self.assertEqual(rows[-1].time_s, 10_800)
        for row in rows:
            self.assertGreaterEqual(row.battery_wh, 0)
            self.assertLessEqual(row.battery_wh, 85)
            self.assert_balanced(row, 45)

    def test_cli_exports_a_complete_csv_and_refuses_to_replace_it(self):
        with tempfile.TemporaryDirectory(prefix="orbit power & ") as directory:
            output = Path(directory) / "power.csv"
            command = [sys.executable, "-I", "-m", "orbit_sim.power_budget", str(EXAMPLE), str(output)]
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            with output.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(float(rows[-1]["time_s"]), 10_800)
            self.assertGreater(float(rows[-1]["unserved_wh"]), 0)
            before = output.read_bytes()
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output.read_bytes(), before)
