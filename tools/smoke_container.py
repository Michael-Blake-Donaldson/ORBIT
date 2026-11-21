"""Exercise the packaged loopback workspace and verify recovery after a restart."""

import argparse
import csv
import io
import json
import re
import secrets
import subprocess
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from time import monotonic, sleep

_MAX_RESULT = 4 * 1024 * 1024
_ROOT = Path(__file__).resolve().parents[1]


def _request(
    path: str, method: str = "GET", body: bytes | None = None, key: str | None = None,
) -> tuple[int, bytes]:
    connection = HTTPConnection("127.0.0.1", 8765, timeout=5)
    headers = {"Origin": "http://127.0.0.1:8765"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["Idempotency-Key"] = key
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        content = response.read(_MAX_RESULT + 1)
        if len(content) > _MAX_RESULT:
            raise RuntimeError("Workspace response exceeds the smoke-test limit.")
        return response.status, content
    finally:
        connection.close()


def _require_status(actual: int, expected: int, operation: str) -> None:
    if actual != expected:
        raise RuntimeError(f"{operation} returned HTTP {actual}; expected {expected}.")


def _job(plan: bytes, key: str) -> dict:
    code, data = _request("/api/jobs", "POST", plan, key)
    _require_status(code, 202, "job submission")
    return json.loads(data)


def _wait_job(path: str, expected: str) -> None:
    deadline = monotonic() + 35
    while monotonic() < deadline:
        try:
            code, data = _request(path)
        except (OSError, HTTPException):
            code = 502
        if code == 200:
            state = json.loads(data)["state"]
            if state == expected:
                return
            if state in ("complete", "failed", "cancelled"):
                raise RuntimeError(f"Job reached {state}; expected {expected}.")
        elif code not in (502, 503):
            _require_status(code, 200, "job status")
        sleep(0.2)
    raise RuntimeError(f"Job did not reach {expected} before the smoke-test deadline.")


def _restart_controller(project: str) -> None:
    subprocess.run(
        ["docker", "compose", "-p", project, "-f", "deploy/compose.yaml", "-f", "deploy/compose.preview.yaml",
         "restart", "controller"],
        cwd=_ROOT, timeout=45, check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart-controller", metavar="COMPOSE_PROJECT")
    args = parser.parse_args()
    if args.restart_controller and re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.restart_controller) is None:
        parser.error("supply the exact name of the preview Compose project")

    _require_status(_request("/readyz")[0], 200, "workspace readiness")
    code, page = _request("/")
    _require_status(code, 200, "workspace")
    if b'id="plan-form"' not in page:
        raise SystemExit("The deployed workspace is missing its mission form.")
    _require_status(_request("/jobs.mjs")[0], 200, "job session module")
    code, plan = _request("/api/examples/hohmann")
    _require_status(code, 200, "Hohmann example")
    key = secrets.token_urlsafe()
    original = _job(plan, key)
    path = f"/api/jobs/{original['id']}"
    if _job(plan, key)["id"] != original["id"]:
        raise SystemExit("A repeated submission started a different job.")
    _wait_job(path, "complete")
    code, csv_bytes = _request(f"{path}/telemetry")
    _require_status(code, 200, "telemetry download")
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8"))))
    final = rows[-1]
    if (len(rows) != 244 or final["kind"] != "complete" or float(final["time_s"]) != 7200
            or abs(float(final["mass_kg"]) - 572.35) > 0.02):
        raise SystemExit("Packaged Hohmann telemetry differs from the reference.")
    print(f"Packaged Hohmann completed with {len(rows)} samples and {float(final['mass_kg']):.2f} kg.")

    if args.restart_controller:
        _restart_controller(args.restart_controller)
        _wait_job(path, "complete")
        if _job(plan, key)["id"] != original["id"] or _request(f"{path}/telemetry") != (200, csv_bytes):
            raise SystemExit("Controller restart changed the job identity or telemetry.")
        print("Controller restart preserved the job identity and exact CSV.")

    long_plan = json.loads(plan)
    long_plan.update(duration_s=12000, integrator={"max_step_s": 0.25})
    active = _job(json.dumps(long_plan).encode(), secrets.token_urlsafe())
    cancel_path = f"/api/jobs/{active['id']}"
    _require_status(_request(cancel_path, "DELETE")[0], 200, "job cancellation")
    _wait_job(cancel_path, "cancelled")
    _require_status(_request(f"{cancel_path}/telemetry")[0], 409, "cancelled telemetry access")
    print("Cancelled packaged job produced no downloadable telemetry.")


if __name__ == "__main__":
    main()
