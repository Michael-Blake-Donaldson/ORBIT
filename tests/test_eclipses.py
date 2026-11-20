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
    solar_visibility_fraction,
    state_from_keplerian,
)
from orbit_sim.illumination import _visible_fraction

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

    def test_visible_solid_angle_matches_spherical_lens_and_symmetry(self):
        earth, solar, separation = 0.85, 0.23, 0.75
        a = math.acos((math.cos(solar) - math.cos(earth) * math.cos(separation))
                      / (math.sin(earth) * math.sin(separation)))
        b = math.acos((math.cos(earth) - math.cos(solar) * math.cos(separation))
                      / (math.sin(solar) * math.sin(separation)))
        c = math.acos((math.cos(separation) - math.cos(earth) * math.cos(solar))
                      / (math.sin(earth) * math.sin(solar)))
        lens = 2 * (math.pi - c - a * math.cos(earth) - b * math.cos(solar))
        reference = 1 - lens / (2 * math.pi * (1 - math.cos(solar)))
        self.assertAlmostEqual(_visible_fraction(earth, solar, separation), reference, places=11)
        for solar_radius in (0.0046, 1e-9):
            self.assertAlmostEqual(_visible_fraction(math.pi / 2, solar_radius, math.pi / 2), 0.5, places=11)

        sun = (_AU_KM, 0, 0)
        self.assertEqual(solar_visibility_fraction((7000, 0, 0), sun), 1)
        self.assertEqual(solar_visibility_fraction((-7000, 0, 0), sun), 0)
        offset = 2.1e6
        earth_angle = math.asin(_EARTH_RADIUS / offset)
        sun_angle = math.asin(_SOLAR_RADIUS / (_AU_KM + offset))
        annular = 1 - (1 - math.cos(earth_angle)) / (1 - math.cos(sun_angle))
        self.assertAlmostEqual(solar_visibility_fraction((-offset, 0, 0), sun), annular, places=10)

    def test_visibility_is_stable_at_floating_point_tangency(self):
        earth, sun = 0.463028453977212, 0.008117151999964295
        inner = earth - sun
        outer = earth + sun
        self.assertEqual(_visible_fraction(earth, sun, inner), 0)
        self.assertLessEqual(_visible_fraction(earth, sun, math.nextafter(inner, math.inf)), 1e-11)
        self.assertGreaterEqual(_visible_fraction(earth, sun, math.nextafter(outer, -math.inf)), 1 - 1e-11)
        self.assertEqual(_visible_fraction(earth, sun, outer), 1)

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

    def test_sample_csv_agrees_with_eclipse_phases(self):
        expected = eclipse_windows(load_mission(EXAMPLE))
        with tempfile.TemporaryDirectory(prefix="orbit visibility & ") as directory:
            output = Path(directory) / "visibility.csv"
            command = [sys.executable, "-I", "-m", "orbit_sim.eclipses", str(EXAMPLE), str(output), "--format", "samples"]
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            with output.open(newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 2161)
        self.assertEqual(rows[-1]["kind"], "complete")
        partial_count = 0
        for row in rows:
            time = float(row["time_s"])
            fraction = float(row["visible_solar_fraction"])
            phase = next((window.kind for window in expected if window.start_s < time < window.end_s), None)
            if phase == "penumbra":
                self.assertTrue(0 < fraction < 1)
                partial_count += 1
            else:
                self.assertEqual(fraction, 0 if phase == "umbra" else 1)
        self.assertGreater(partial_count, 0)
