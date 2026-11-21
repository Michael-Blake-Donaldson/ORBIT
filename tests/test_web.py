import csv
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from orbit_sim.web import create_app

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_HOST = "127.0.0.1:8765"


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(8765)

    def request(self, path, body=None, **headers):
        response = {}

        def start(status, response_headers):
            response.update(status=status, headers=dict(response_headers))

        environ = {
            "PATH_INFO": path,
            "REQUEST_METHOD": "GET" if body is None else "POST",
            "HTTP_HOST": _HOST,
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body or b"")),
            "wsgi.input": io.BytesIO(body or b""),
        }
        response["body"] = b"".join(self.app(environ | headers, start))
        return response

    def test_examples_are_identical_to_the_cli_plans(self):
        for name, source in (
            ("hohmann", "hohmann"), ("finite-burn", "finite_burn"), ("j2-precession", "j2_precession"),
        ):
            with self.subTest(name=name):
                response = self.request(f"/api/examples/{name}")
                self.assertEqual(response["status"], "200 OK")
                self.assertEqual(json.loads(response["body"]), json.loads((_EXAMPLES / f"{source}.json").read_bytes()))

    def test_run_returns_canonical_impulse_rows_and_terminal_state(self):
        plan = self.request("/api/examples/hohmann")["body"]
        self.assertEqual(self.request("/api/validate", plan)["status"], "200 OK")
        result = self.request("/api/run", plan)
        self.assertEqual(result["status"], "200 OK", result["body"])
        self.assertEqual(result["headers"]["Content-Type"], "text/csv; charset=utf-8")
        rows = list(csv.DictReader(io.StringIO(result["body"].decode("utf-8"))))
        self.assertEqual([row["kind"] for row in rows[:2]], ["pre_burn", "post_burn"])
        self.assertEqual(rows[-1]["kind"], "complete")
        self.assertAlmostEqual(float(rows[-1]["mass_kg"]), 572.35, places=2)

    def test_rejects_host_origin_oversize_and_duplicate_keys(self):
        self.assertEqual(self.request("/api/examples/hohmann", HTTP_HOST="other.invalid")["status"], "403 Forbidden")
        self.assertEqual(
            self.request("/api/run", b"{}", HTTP_ORIGIN="https://other.invalid")["status"], "403 Forbidden",
        )
        self.assertEqual(self.request("/api/run", b"{}", CONTENT_LENGTH="262145")["status"], "413 Content Too Large")
        response = self.request("/api/validate", b'{"schema_version": 1, "schema_version": 1}')
        self.assertEqual(response["status"], "422 Unprocessable Content")
        self.assertIn("duplicate JSON property", json.loads(response["body"])["message"])

    def test_web_rows_have_their_own_cadence_budget(self):
        plan = json.loads(self.request("/api/examples/hohmann")["body"])
        plan["sample_interval_s"] = 1
        result = self.request("/api/validate", json.dumps(plan).encode())
        self.assertEqual(result["status"], "422 Unprocessable Content")
        self.assertEqual(json.loads(result["body"])["field"], "sample_interval_s")

    def test_a_late_maneuver_failure_returns_no_telemetry(self):
        plan = json.loads(self.request("/api/examples/hohmann")["body"])
        plan["spacecraft"]["dry_mass_kg"] = 650
        response = self.request("/api/run", json.dumps(plan).encode())
        self.assertEqual(response["status"], "422 Unprocessable Content")
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        self.assertIn("dry mass", json.loads(response["body"])["message"])

    def test_workspace_headers_and_asset_allowlist(self):
        response = self.request("/")
        self.assertEqual(response["status"], "200 OK")
        self.assertIn(b'id="plan-form"', response["body"])
        self.assertIn("frame-ancestors 'none'", response["headers"]["Content-Security-Policy"])
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        self.assertEqual(self.request("/api/examples/lunar")["status"], "404 Not Found")
        self.assertEqual(self.request("/../pyproject.toml")["status"], "404 Not Found")

    def test_a_worker_deadline_returns_an_error_and_releases_the_run_slot(self):
        plan = self.request("/api/examples/hohmann")["body"]
        with patch("orbit_sim.web._TIMEOUT_S", 0.02):
            for _ in range(2):
                response = self.request("/api/run", plan)
                self.assertEqual(response["status"], "422 Unprocessable Content", response["body"])
                self.assertIn("workspace time limit", json.loads(response["body"])["message"])
        self.assertEqual(self.request("/api/run", plan)["status"], "200 OK")
