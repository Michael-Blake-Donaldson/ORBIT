import io
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from orbit_sim import FiniteBurn, Impulse, Mission, Spacecraft, State, propagate
from orbit_sim.oem import _export_oem, _Metadata

_STATE = State((7000, 0, 0), (0, 7.5, 0))
_MANEUVER = (0, 0.1, 0)
_METADATA = _Metadata("ICRF", "UNKNOWN", "UNKNOWN", "ORBIT")
_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _segments(text):
    segments = []
    for block in text.split("META_START\n")[1:]:
        metadata_block, records_block = block.split("META_STOP\n")
        metadata = dict(line.split(" = ", 1) for line in metadata_block.splitlines())
        records = [line.split() for line in records_block.splitlines() if line]
        segments.append((metadata, records))
    return segments


class OemTests(unittest.TestCase):
    def test_tdb_day_rollover_and_state_vectors(self):
        mission = Mission(_STATE, 23, 10, epoch_jd_tdb=2460676.4999)
        file = io.StringIO()
        self.assertEqual(_export_oem(file, mission, _METADATA), (4, 1))
        self.assertRegex(file.getvalue(), r"^CCSDS_OEM_VERS = 3\.0\nCREATION_DATE = [^\n]+Z\n")
        [(metadata, rows)] = _segments(file.getvalue())
        self.assertEqual(metadata["START_TIME"], "2024-12-31T23:59:51.360000000")
        self.assertEqual(metadata["STOP_TIME"], "2025-01-01T00:00:14.360000000")
        self.assertEqual(metadata["TIME_SYSTEM"], "TDB")
        self.assertEqual(metadata["CENTER_NAME"], "EARTH")
        self.assertEqual(metadata["REF_FRAME"], "ICRF")
        for row, sample in zip(rows, propagate(mission), strict=True):
            self.assertEqual(tuple(map(float, row[1:])), (*sample.state.position_km, *sample.state.velocity_km_s))

    def test_impulses_at_start_interior_and_completion(self):
        mission = Mission(
            _STATE, 30, 10, epoch_jd_tdb=2460676.5,
            impulses=tuple(Impulse(time_s, _MANEUVER) for time_s in (0, 12.75, 30)),
        )
        file = io.StringIO()
        self.assertEqual(_export_oem(file, mission, _METADATA), (7, 3))
        blocks = _segments(file.getvalue())
        self.assertEqual([len(rows) for _, rows in blocks], [3, 3, 1])
        timeline = [row[0] for _, rows in blocks for row in rows]
        self.assertEqual(timeline, sorted(set(timeline)))
        self.assertEqual(blocks[0][0]["STOP_TIME"], "2025-01-01T00:00:12.749999999")
        self.assertEqual(blocks[1][0]["START_TIME"], "2025-01-01T00:00:12.750000000")
        self.assertEqual(blocks[2][0]["START_TIME"], "2025-01-01T00:00:30.000000000")
        expected = [sample for sample in propagate(mission) if sample.kind == "post_burn"]
        for (_, rows), sample in zip(blocks, expected, strict=True):
            self.assertEqual(tuple(map(float, rows[0][1:])), (*sample.state.position_km, *sample.state.velocity_km_s))
        difference = np.asarray(blocks[1][1][0][1:], dtype=float) - np.asarray(blocks[0][1][-1][1:], dtype=float)
        np.testing.assert_allclose(difference[:3], 0, atol=1e-8)
        np.testing.assert_allclose(difference[3:], _MANEUVER, atol=1e-10, rtol=0)

    def test_finite_burn_cutoff_keeps_one_terminal_state(self):
        mission = Mission(
            _STATE, 30, 10, epoch_jd_tdb=2451545.0,
            finite_burns=(FiniteBurn(12, 30, (0, 10, 0)),), spacecraft=Spacecraft(100, 80, 300),
        )
        file = io.StringIO()
        self.assertEqual(_export_oem(file, mission, _METADATA), (5, 1))
        [(metadata, rows)] = _segments(file.getvalue())
        self.assertEqual(metadata["STOP_TIME"], "2000-01-01T12:00:30.000000000")
        self.assertEqual(rows[-1][0], metadata["STOP_TIME"])
        expected = list(propagate(mission))[-1].state
        self.assertEqual(tuple(map(float, rows[-1][1:])), (*expected.position_km, *expected.velocity_km_s))

    def test_terminal_impact_shortens_ephemeris_coverage(self):
        mission = Mission(State((6400, 0, 0), (-1, 0, 0)), 100, 5, epoch_jd_tdb=2460676.5)
        file = io.StringIO()
        self.assertEqual(_export_oem(file, mission, _METADATA), (5, 1))
        [(metadata, rows)] = _segments(file.getvalue())
        self.assertEqual(metadata["STOP_TIME"], rows[-1][0])
        self.assertLess(metadata["STOP_TIME"], "2025-01-01T00:00:20")
        expected = list(propagate(mission))[-1]
        self.assertEqual(expected.kind, "impact")
        self.assertEqual(tuple(map(float, rows[-1][1:])), (*expected.state.position_km, *expected.state.velocity_km_s))

    def test_export_rejects_missing_epoch_unrepresentable_epoch_and_unsafe_metadata(self):
        mission = Mission(_STATE, 10, 5)
        for invalid_epoch in (None, 1_000_000):
            with self.subTest(epoch=invalid_epoch), self.assertRaisesRegex(ValueError, "epoch"):
                _export_oem(io.StringIO(), replace(mission, epoch_jd_tdb=invalid_epoch), _METADATA)
        for bad_name in ("ORBIT\nMETA_START", "ORBIT = X", "ORBIT ", "Ω", ""):
            with self.subTest(value=bad_name), self.assertRaisesRegex(ValueError, "OBJECT_NAME"):
                replace(_METADATA, object_name=bad_name)

    def test_cli_exclusive_publication_preserves_existing_file(self):
        with tempfile.TemporaryDirectory(prefix="orbit oem & ") as directory:
            output = Path(directory) / "lunar.oem"
            command = [
                sys.executable, "-I", "-m", "orbit_sim.oem", str(_EXAMPLES / "lunar_perturbation.json"),
                str(output), "--frame", "ICRF",
            ]
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            first = output.read_bytes()
            self.assertEqual(first.count(b"META_START"), 1)
            self.assertIn(b"STOP_TIME = 2025-01-02T00:00:00.000000000", first)
            result = subprocess.run(command, check=False, capture_output=True, text=True, cwd=directory, timeout=15)
            self.assertEqual(result.returncode, 1)
            self.assertIn("output already exists", result.stderr)
            self.assertEqual(output.read_bytes(), first)
