import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orbit_sim import GeodeticPosition
from orbit_sim.groundtrack import _track_segments

MISSION = {
    "schema_version": 1,
    "initial_orbit": {"periapsis_radius_km": 7000, "eccentricity": 0},
    "duration_s": 600,
    "sample_interval_s": 120,
}


class GroundTrackTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="orbit ground track & ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.plan = self.root / "mission.json"
        self.plan.write_text(json.dumps(MISSION), encoding="utf-8")

    def launch(self, output, angle="0", options=()):
        return subprocess.run(
            [sys.executable, "-I", "-m", "orbit_sim.groundtrack", str(self.plan), str(output),
             "--greenwich-angle-deg", angle, *options],
            check=False, capture_output=True, text=True, cwd=self.root, timeout=15,
        )

    def test_csv_preserves_timeline_and_reports_station_geometry(self):
        output = self.root / "station.csv"
        result = self.launch(output, options=("--station", "0", "0", "--min-elevation-deg", "10"))
        self.assertEqual(result.returncode, 0, result.stderr)
        with output.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual([float(row["time_s"]) for row in rows], list(range(0, 601, 120)))
        self.assertEqual([row["kind"] for row in rows], ["sample"] * 5 + ["complete"])
        first = rows[0]
        self.assertEqual(float(first["subpoint_latitude_deg"]), 0)
        self.assertEqual(float(first["subpoint_longitude_deg"]), 0)
        self.assertAlmostEqual(float(first["ellipsoid_height_km"]), 621.863, places=8)
        self.assertAlmostEqual(float(first["station_slant_range_km"]), 621.863, places=8)
        self.assertEqual(float(first["station_elevation_deg"]), 90)
        self.assertEqual(first["station_azimuth_deg"], "")
        self.assertEqual(first["station_visible"], "1")
        self.assertEqual(rows[-1]["station_visible"], "0")
        self.assertLess(float(rows[-1]["station_elevation_deg"]), 0)

    def test_geojson_separates_antimeridian_and_includes_station_point(self):
        output = self.root / "map.geojson"
        result = self.launch(output, angle="-176", options=("--format", "geojson", "--station", "0", "176"))
        self.assertEqual(result.returncode, 0, result.stderr)
        collection = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(collection["type"], "FeatureCollection")
        first, second, station = collection["features"]
        self.assertEqual(first["geometry"]["coordinates"][-1], [180, 0])
        self.assertEqual(second["geometry"]["coordinates"][0], [-180, 0])
        self.assertEqual([first["geometry"]["type"], second["geometry"]["type"]], ["LineString", "LineString"])
        self.assertEqual(station["geometry"], {"type": "Point", "coordinates": [176, 0]})
        self.assertEqual(station["properties"]["kind"], "ground_station")

    def test_antimeridian_interpolation_and_pole_break(self):
        points = [
            GeodeticPosition(10, -170, 0),
            GeodeticPosition(30, 175, 0),
            GeodeticPosition(90, None, 0),
            GeodeticPosition(50, 15, 0),
        ]
        pieces = list(_track_segments(points))
        self.assertEqual(pieces[0][0], (-170, 10))
        self.assertAlmostEqual(pieces[0][1][1], 10 + 20 * 10 / 15, places=12)
        self.assertEqual(pieces[0][1][0], -180)
        self.assertEqual(pieces[1][0], (180, pieces[0][1][1]))
        self.assertEqual(pieces[1][1], (175, 30))
        self.assertEqual(pieces[2], [(15, 50)])

    def test_ending_on_the_antimeridian_avoids_a_duplicate_point(self):
        points = [GeodeticPosition(10, 179, 0), GeodeticPosition(11, -180, 0)]
        self.assertEqual(list(_track_segments(points)), [[(179, 10), (180, 11)]])

    def test_access_csv_reports_interpolated_contact_window(self):
        output = self.root / "contact.csv"
        result = self.launch(output, options=("--format", "access", "--station", "0", "0", "--min-elevation-deg", "10"))
        self.assertEqual(result.returncode, 0, result.stderr)
        with output.open(newline="", encoding="utf-8") as file:
            (row,) = list(csv.DictReader(file))
        self.assertEqual(float(row["start_s"]), 0)
        self.assertEqual(row["end_s"], row["duration_s"])
        self.assertLess(float(row["end_s"]), 360)
        self.assertGreater(float(row["end_s"]), 240)

    def test_errors_leave_no_partial_output_and_preserve_existing_files(self):
        existing = self.root / "existing.csv"
        existing.write_text("original", encoding="utf-8")
        result = self.launch(existing)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(existing.read_text(encoding="utf-8"), "original")

        plan = MISSION | {
            "spacecraft": {"initial_mass_kg": 100, "dry_mass_kg": 99, "isp_s": 300},
            "impulses": [{"time_s": 240, "delta_v_km_s": [0, 1, 0]}],
        }
        self.plan.write_text(json.dumps(plan), encoding="utf-8")
        output = self.root / "invalid.geojson"
        result = self.launch(output, options=("--format", "geojson"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("dry mass", result.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob(".orbit-*")))
