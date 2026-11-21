import csv
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orbit_sim.web import JobClient, create_app

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
_HOST = "127.0.0.1:8765"
_CONTROLLER = _EXAMPLES.parent / "controller/target/orbit-controller-0.1.0.jar"
_JAVA = shutil.which("java")


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

    @unittest.skipUnless(_JAVA and _CONTROLLER.is_file(), "build the Java controller to verify queued browser runs")
    def test_queued_browser_run_reuses_the_request_key_and_serves_completed_csv(self):
        with tempfile.TemporaryDirectory(prefix="orbit controller workspace ") as spool:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            token = secrets.token_urlsafe()
            environment = dict(os.environ, ORBIT_JOB_TOKEN=token)
            with subprocess.Popen(
                [_JAVA, "-jar", str(_CONTROLLER), "--serve", sys.executable, str(port), spool],
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ) as service:
                try:
                    jobs = JobClient(port, token)
                    self.wait_for_service(jobs, service)
                    self.app = create_app(8765, jobs)
                    self.assertEqual(json.loads(self.request("/api/capabilities")["body"]), {"mission_jobs": True})
                    plan = self.request("/api/examples/hohmann")["body"]
                    self.assertEqual(self.request("/api/run", plan)["status"], "409 Conflict")
                    key = secrets.token_urlsafe()
                    first = self.request("/api/jobs", plan, HTTP_IDEMPOTENCY_KEY=key)
                    self.assertEqual(first["status"], "202 Accepted", first["body"])
                    identifier = json.loads(first["body"])["id"]
                    second = self.request("/api/jobs", plan, HTTP_IDEMPOTENCY_KEY=key)
                    self.assertEqual(json.loads(second["body"])["id"], identifier)
                    path = f"/api/jobs/{identifier}"
                    status = self.wait_for_job(path)
                    self.assertEqual(status["state"], "complete", status)
                    self.assertEqual(status["rows"], 244)
                    response = self.request(f"{path}/telemetry")
                    self.assertEqual(response["status"], "200 OK", response["body"][:240])
                    self.assertIn(b"7200.0,complete,", response["body"])
                    self.assertEqual(jobs.forward("GET", f"/jobs/{identifier}", owner="other-owner")[0],
                                     "404 Not Found")
                    self.assertEqual(self.request(path, HTTP_X_ORBIT_OWNER="other-owner")["status"], "200 OK")
                    self.assertEqual(self.request(path, HTTP_ORIGIN="http://elsewhere.invalid")["status"],
                                     "403 Forbidden")
                    self.check_browser_cancellation()
                finally:
                    if service.poll() is None:
                        service.terminate()
                    service.wait(timeout=8)

    def wait_for_service(self, jobs, service):
        for _ in range(80):
            if jobs.forward("GET", "/health")[0] == "200 OK":
                return
            if service.poll() is not None:
                break
            time.sleep(0.05)
        self.fail("Java job service did not reach readiness")

    def wait_for_job(self, path):
        for _ in range(160):
            response = self.request(path)
            self.assertEqual(response["status"], "200 OK", response["body"])
            status = json.loads(response["body"])
            if status["state"] in ("complete", "cancelled", "failed"):
                return status
            time.sleep(0.05)
        self.fail("mission job did not reach a terminal state")

    def check_browser_cancellation(self):
        plan = json.loads(self.request("/api/examples/hohmann")["body"])
        plan.update(duration_s=12000, integrator={"max_step_s": 0.25})
        created = self.request("/api/jobs", json.dumps(plan).encode(), HTTP_IDEMPOTENCY_KEY=secrets.token_urlsafe())
        self.assertEqual(created["status"], "202 Accepted", created["body"])
        path = f"/api/jobs/{json.loads(created['body'])['id']}"
        cancel = self.request(path, REQUEST_METHOD="DELETE")
        self.assertEqual(cancel["status"], "200 OK", cancel["body"])
        self.assertEqual(self.wait_for_job(path)["state"], "cancelled")
        self.assertEqual(self.request(f"{path}/telemetry")["status"], "409 Conflict")
