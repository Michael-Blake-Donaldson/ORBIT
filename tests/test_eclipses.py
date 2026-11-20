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
    KeplerianOrbit,
    Mission,
    SimulationError,
    State,
    ThirdBody,
    eclipse_windows,
    load_mission,
    state_from_keplerian,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "solar_eclipses.json"
_EARTH_RADIUS = 6378.137
_ORBIT_RADIUS = 7000
_SOLAR_RADIUS = 695700
_AU_KM = 149597870.7
_MEAN_MOTION = math.sqrt(398600.4418 / _ORBIT_RADIUS**3)


def fixed_sun(duration_s, latitude_rad=0, distance_km=_AU_KM):
    position = (distance_km * math.cos(latitude_rad), 0, distance_km * math.sin(latitude_rad))
    # A negligible third-body GM isolates the two-body eclipse reference.
    points = tuple(EphemerisPoint(time, position, (0, 0, 0)) for time in (0, duration_s))
    return ThirdBody("Sun", 1e-12, points)


class EclipseTests(unittest.TestCase):
    def test_full_eclipse_time_matches_external_and_internal_common_tangents(self):
        period = math.tau / _MEAN_MOTION
        state = state_from_keplerian(KeplerianOrbit(_ORBIT_RADIUS, 0))
        mission = Mission(state, period, period, third_bodies=(fixed_sun(period),))
        ingress, total, egress = eclipse_windows(mission)
        self.assertEqual([window.kind for window in (ingress, total, egress)], ["penumbra", "umbra", "penumbra"])

        # The normal's x component follows from the signed distances to the tangent line.
        penumbra_normal_x = (_EARTH_RADIUS + _SOLAR_RADIUS) / _AU_KM
        umbra_normal_x = (_EARTH_RADIUS - _SOLAR_RADIUS) / _AU_KM
        limb_angle = math.acos(_EARTH_RADIUS / _ORBIT_RADIUS)
        penumbra_start = (math.acos(penumbra_normal_x) + limb_angle) / _MEAN_MOTION
        umbra_start = (math.acos(umbra_normal_x) + limb_angle) / _MEAN_MOTION
        self.assertAlmostEqual(ingress.start_s, penumbra_start, places=5)
        self.assertAlmostEqual(total.start_s, umbra_start, places=5)
        self.assertAlmostEqual(total.end_s, period - umbra_start, places=5)
        self.assertAlmostEqual(egress.end_s, period - penumbra_start, places=5)
        self.assertEqual(ingress.end_s, total.start_s)
        self.assertEqual(total.end_s, egress.start_s)
        self.assertEqual(eclipse_windows(replace(mission, sample_interval_s=60)), (ingress, total, egress))

    def test_finds_a_grazing_penumbra_inside_one_dense_step(self):
        period = math.tau / _MEAN_MOTION
        limit = math.asin(_EARTH_RADIUS / _ORBIT_RADIUS) + math.asin((_EARTH_RADIUS + _SOLAR_RADIUS) / _AU_KM)
        sun_latitude = limit - 1e-7
        state = state_from_keplerian(KeplerianOrbit(_ORBIT_RADIUS, 0))
        mission = Mission(state, period, period, third_bodies=(fixed_sun(period, sun_latitude),))
        (window,) = eclipse_windows(mission)
        half_duration = math.acos(math.cos(limit) / math.cos(sun_latitude)) / _MEAN_MOTION
        self.assertEqual(window.kind, "penumbra")
        self.assertAlmostEqual(window.start_s, period / 2 - half_duration, places=4)
        self.assertAlmostEqual(window.end_s, period / 2 + half_duration, places=4)
        self.assertLess(window.duration_s, 2)

    def test_annular_occultation_and_no_eclipse_cases(self):
        body = fixed_sun(60)
        annular = Mission(State((-2.1e6, 0, 0), (0, 0, 0)), 60, 60, third_bodies=(body,))
        (window,) = eclipse_windows(annular)
        self.assertEqual((window.kind, window.start_s, window.end_s), ("antumbra", 0, 60))
        lit = replace(annular, initial_state=State((7000, 0, 0), (0, 7.5, 0)))
        self.assertEqual(eclipse_windows(lit), ())
        with self.assertRaisesRegex(ValueError, "selected Sun ephemeris"):
            eclipse_windows(lit, "Moon")
        with self.assertRaisesRegex(SimulationError, "outside Earth"):
            eclipse_windows(replace(lit, third_bodies=(fixed_sun(60, distance_km=100_000),)))

    def test_horizons_example_and_exclusive_csv_publication(self):
        expected = eclipse_windows(load_mission(EXAMPLE))
        self.assertEqual([window.kind for window in expected], ["penumbra", "umbra", "penumbra"] * 2)
        with tempfile.TemporaryDirectory(prefix="orbit eclipse & ") as directory:
            output = Path(directory) / "eclipses.csv"
            command = [sys.executable, "-I", "-m", "orbit_sim.eclipses", str(EXAMPLE), str(output)]
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            with output.open(newline="", encoding="utf-8") as file:
                csv_rows = list(csv.DictReader(file))
            self.assertEqual([row["kind"] for row in csv_rows], [window.kind for window in expected])
            for csv_row, window in zip(csv_rows, expected, strict=True):
                self.assertEqual(float(csv_row["start_s"]), window.start_s)
                self.assertEqual(float(csv_row["duration_s"]), window.duration_s)
            before = output.read_bytes()
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output.read_bytes(), before)
