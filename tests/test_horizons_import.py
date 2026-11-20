import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orbit_sim import load_mission
from orbit_sim.horizons import _read_vectors

HEADER = """Center body name: Earth (399) {source: DE441}
Center-site name: BODY CENTER
Output units    : KM-S
Output type     : GEOMETRIC cartesian states
Output format   : 2 (position and velocity)
Reference frame : ICRF
JDTDB, Calendar Date (TDB), X, Y, Z, VX, VY, VZ,
"""
START = "2460676.500000000, A.D. 2025-Jan-01 00:00:00.0000, 152052.36, -307823.63, -166879.89, 0.93, 0.39, 0.21,"
END = "2460676.666666667, A.D. 2025-Jan-01 04:00:00.0000, 165364.46, -301916.90, -163692.15, 0.92, 0.43, 0.23,"
EXPORT = HEADER + "$$SOE\n" + START + "\n" + END + "\n$$EOE\n"
BASE = {
    "schema_version": 1,
    "duration_s": 14400,
    "sample_interval_s": 300,
    "initial_orbit": {"periapsis_radius_km": 42164, "eccentricity": 0},
}


class HorizonsImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="orbit horizons & ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.export = self.root / "vectors.txt"
        self.export.write_text(EXPORT, encoding="utf-8")
        self.plan = self.root / "base.json"
        self.plan.write_text(json.dumps(BASE), encoding="utf-8")
        self.output = self.root / "imported.json"

    def run_import(self, name="Moon", plan=None, output=None):
        command = [
            sys.executable, "-I", "-m", "orbit_sim.horizons", str(self.export), str(plan or self.plan),
            str(output or self.output), "--name", name, "--mu", "4902.800118",
        ]
        return subprocess.run(command, check=False, capture_output=True, text=True, cwd=self.root, timeout=15)

    def test_import_creates_valid_plan_and_preserves_input_and_existing_output(self):
        original = self.plan.read_bytes()
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        mission = load_mission(self.output)
        self.assertEqual(mission.epoch_jd_tdb, 2460676.5)
        self.assertEqual([point.time_s for point in mission.third_bodies[0].ephemeris], [0, 14400])
        self.assertEqual(mission.third_bodies[0].ephemeris[1].velocity_km_s, (0.92, 0.43, 0.23))
        self.assertEqual(self.plan.read_bytes(), original)
        published = self.output.read_bytes()
        result = self.run_import()
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(self.output.read_bytes(), published)

    def test_requires_shared_epoch_when_appending_an_ephemeris(self):
        result = self.run_import()
        self.assertEqual(result.returncode, 0, result.stderr)
        combined = self.root / "combined.json"
        result = self.run_import(name="Second body", plan=self.output, output=combined)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([body.name for body in load_mission(combined).third_bodies], ["Moon", "Second body"])

        shifted = EXPORT.replace("2460676.5", "2460677.5").replace("2460676.666", "2460677.666")
        self.export.write_text(shifted, encoding="utf-8")
        mismatch = self.root / "epoch-mismatch.json"
        result = self.run_import(name="Misaligned body", plan=self.output, output=mismatch)
        self.assertEqual(result.returncode, 1)
        self.assertIn("mission's epoch_jd_tdb", result.stderr.replace("\\'", "'"))
        self.assertFalse(mismatch.exists())

    def test_rejects_wrong_frame_units_time_scale_and_malformed_rows(self):
        replacements = (
            ("Earth (399)", "Mars (499)"),
            ("BODY CENTER", "TOPOCENTRIC"),
            ("ICRF", "Ecliptic of J2000.0"),
            ("KM-S", "AU-D"),
            ("GEOMETRIC", "ASTROMETRIC"),
            ("Calendar Date (TDB)", "Calendar Date (UTC)"),
            ("2460676.666666667", "NaN"),
            ("-301916.90", "nan"),
            ("$$EOE", ""),
        )
        for before, after in replacements:
            with self.subTest(value=before):
                self.export.write_text(EXPORT.replace(before, after), encoding="utf-8")
                with self.assertRaises(ValueError):
                    _read_vectors(self.export)

    def test_does_not_publish_when_coverage_is_too_short(self):
        self.plan.write_text(json.dumps(BASE | {"duration_s": 14_401}), encoding="utf-8")
        result = self.run_import()
        self.assertEqual(result.returncode, 1)
        self.assertIn("entire mission", result.stderr)
        self.assertFalse(self.output.exists())
