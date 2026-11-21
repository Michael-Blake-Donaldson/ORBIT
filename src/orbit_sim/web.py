"""HTTP adapter for the ORBIT browser workspace, served on loopback."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable
from http.client import HTTPConnection, HTTPException
from importlib.resources import files
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from typing import TYPE_CHECKING, Any

from .files import parse_json_object
from .plan import _MAX_PLAN_BYTES, _parse_mission

if TYPE_CHECKING:
    from .alb_identity import AlbIdentity

MAX_WEB_SAMPLES = 4000
_MAX_CSV_BYTES = 4 * 1024 * 1024
_TIMEOUT_S = 15
_JOB_PATH = re.compile(r"/api/jobs/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:/telemetry)?")
_JOB_ERRORS = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
               409: "Conflict", 413: "Content Too Large", 415: "Unsupported Media Type",
               500: "Internal Server Error", 503: "Service Unavailable"}
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
    "/workspace.mjs": ("workspace.mjs", "text/javascript; charset=utf-8"),
    "/plots.mjs": ("plots.mjs", "text/javascript; charset=utf-8"),
    "/mission.mjs": ("mission.mjs", "text/javascript; charset=utf-8"),
    "/api/examples/hohmann": ("missions/hohmann.json", "application/json"),
    "/api/examples/finite-burn": ("missions/finite-burn.json", "application/json"),
    "/api/examples/j2-precession": ("missions/j2-precession.json", "application/json"),
}
_SECURITY_HEADERS = [
    ("Cache-Control", "no-store"),
    ("Content-Security-Policy", (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
]
_Reply = tuple[str, str, bytes]
_StartResponse = Callable[[str, list[tuple[str, str]]], Any]


def _json(status: str, value: dict[str, object]) -> _Reply:
    return status, "application/json", json.dumps(value).encode("utf-8")


def _error(status: str, message: str, field: str = "plan") -> _Reply:
    return _json(status, {"message": message, "field": field})


def _validated_request(environ: dict[str, Any]) -> bytes | _Reply:
    if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
        return _error("415 Unsupported Media Type", "Send a UTF-8 JSON mission plan.")
    length = environ.get("CONTENT_LENGTH", "")
    if not length or len(length) > 9 or not length.isascii() or not length.isdecimal():
        return _error("400 Bad Request", "A valid request length is required.")
    if int(length) > _MAX_PLAN_BYTES:
        return _error("413 Content Too Large", "Mission plan exceeds 256 KiB.")
    body = environ["wsgi.input"].read(int(length))
    if len(body) != int(length):
        return _error("400 Bad Request", "The request ended before the complete plan was received.")
    try:
        mission = _parse_mission(parse_json_object(body.decode("utf-8"), "mission plan"))
    except (ValueError, TypeError) as exc:
        return _error("422 Unprocessable Content", str(exc)[:240])

    rows = ceil(mission.duration_s / mission.sample_interval_s) + 1 + 2 * (
        len(mission.impulses) + len(mission.finite_burns)
    )
    if rows > MAX_WEB_SAMPLES:
        return _error("422 Unprocessable Content", f"Increase the sample interval to stay under {MAX_WEB_SAMPLES:,} rows.",
                      "sample_interval_s")
    return body


def _mission_request(environ: dict[str, Any], run: bool, slots: BoundedSemaphore) -> _Reply:
    body = _validated_request(environ)
    if not isinstance(body, bytes):
        return body
    if not run:
        return _json("200 OK", {"valid": True})
    if not slots.acquire(blocking=False):
        return _error("503 Service Unavailable", "The workspace is busy. Try again shortly.")
    try:
        return _run_mission(body)
    finally:
        slots.release()


def _run_mission(body: bytes) -> _Reply:
    with TemporaryDirectory(prefix="orbit-web-") as directory:
        plan, telemetry = (Path(directory) / name for name in ("plan.json", "telemetry.csv"))
        plan.write_bytes(body)
        log_path = Path(directory) / "diagnostic.txt"
        with log_path.open("wb") as diagnostics, subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", "-m", "orbit_sim", "--", str(plan), str(telemetry)],
            cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=diagnostics,
            start_new_session=os.name != "nt",
        ) as worker:
            try:
                exit_status = worker.wait(timeout=_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                _stop_worker(worker)
                return _error("422 Unprocessable Content", "Simulation exceeded the workspace time limit.")
        if exit_status != 0:
            with log_path.open("rb") as diagnostics:
                message = diagnostics.read(400).decode("utf-8", errors="replace").strip()
            if message.startswith("ORBIT error: "):
                return _error("422 Unprocessable Content", message.removeprefix("ORBIT error: "))
            return _error("500 Internal Server Error", "The simulation worker failed.")
        with telemetry.open("rb") as file:
            csv_bytes = file.read(_MAX_CSV_BYTES + 1)
        if len(csv_bytes) > _MAX_CSV_BYTES or csv_bytes.count(b"\n") > MAX_WEB_SAMPLES + 1:
            return _error("422 Unprocessable Content", "The result exceeds the workspace telemetry limit.",
                          "sample_interval_s")
        return "200 OK", "text/csv; charset=utf-8", csv_bytes


def _stop_worker(worker: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        # The Windows virtual-environment executable can launch a separate interpreter.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(worker.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3, check=False, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        finally:
            if worker.poll() is None:
                worker.kill()
    else:
        try:
            os.killpg(worker.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class JobClient:
    """Forward job calls to a fixed, authenticated loopback service."""

    def __init__(self, port: int, token: str):
        if not 1024 <= port <= 65535 or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
            raise ValueError("Use a valid job port and set ORBIT_JOB_TOKEN.")
        self.port = port
        self.token = token

    def forward(
        self, method: str, path: str, body: bytes | None = None, key: str = "", *, owner: str = "local-preview",
    ) -> _Reply:
        client = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Authorization": f"Bearer {self.token}", "X-Orbit-Owner": owner}
        if body is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        try:
            client.request(method, path, body, headers)
            response = client.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type", "").split(";", 1)[0]
            if status == 200 and path.endswith("/telemetry") and content_type == "text/csv":
                result = response.read(_MAX_CSV_BYTES + 1)
                if len(result) <= _MAX_CSV_BYTES:
                    return "200 OK", "text/csv; charset=utf-8", result
            elif content_type == "application/json" and status in (200, 202, *_JOB_ERRORS):
                result = response.read(4097)
                if len(result) <= 4096:
                    reason = {200: "OK", 202: "Accepted", **_JOB_ERRORS}[status]
                    return f"{status} {reason}", "application/json", result
        except (OSError, HTTPException):
            pass
        finally:
            client.close()
        return _error("502 Bad Gateway", "The mission job service is unavailable.")


def create_app(
    port: int, jobs: JobClient | None = None, identity: "AlbIdentity | None" = None,
) -> Callable[[dict[str, Any], _StartResponse], Iterable[bytes]]:
    """Build the preview or authenticated gateway for the mission workspace."""
    authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
    security_headers = _SECURITY_HEADERS.copy()
    if identity is not None:
        if jobs is None:
            raise ValueError("Hosted authentication requires the Java mission job service.")
        from .alb_identity import IdentityUnavailable, InvalidIdentity

        authorities = {identity.config.host}
        security_headers.append(("Strict-Transport-Security", "max-age=31536000"))
    assets = files("orbit_sim").joinpath("web_assets")
    slots = BoundedSemaphore(2)

    def route(environ: dict[str, Any]) -> _Reply:
        path, method = environ["PATH_INFO"], environ["REQUEST_METHOD"]
        if method == "GET" and path == "/healthz":
            return _json("200 OK", {"status": "alive"})
        host = environ.get("HTTP_HOST", "").lower()
        if host not in authorities:
            return _error("403 Forbidden", "Open this workspace through its configured address.")
        owner = "local-preview"
        expected_origin = f"http://{host}"
        if identity is not None:
            expected_origin = identity.config.origin
            try:
                owner = identity.owner(environ.get("HTTP_X_AMZN_OIDC_DATA", ""))
            except InvalidIdentity:
                return _error("401 Unauthorized", "Sign in to use the mission workspace.")
            except IdentityUnavailable:
                return _error("503 Service Unavailable", "Sign-in verification is temporarily unavailable.")
        if method == "GET" and path in _ASSETS:
            name, content_type = _ASSETS[path]
            return "200 OK", content_type, assets.joinpath(name).read_bytes()
        if method == "GET" and path == "/api/capabilities":
            return _json("200 OK", {"mission_jobs": jobs is not None})
        if path in ("/api/validate", "/api/run", "/api/jobs") or _JOB_PATH.fullmatch(path):
            origin = environ.get("HTTP_ORIGIN")
            required = origin is not None or (identity is not None and method in ("POST", "DELETE"))
            if (required and origin != expected_origin) or environ.get("HTTP_SEC_FETCH_SITE") == "cross-site":
                return _error("403 Forbidden", "Only same-origin workspace requests are accepted.")
        if method == "POST" and path in ("/api/validate", "/api/run"):
            if path == "/api/run" and jobs is not None:
                return _error("409 Conflict", "Submit through the mission job service in queued mode.")
            return _mission_request(environ, path == "/api/run", slots)
        if jobs is not None and method == "POST" and path == "/api/jobs":
            key = environ.get("HTTP_IDEMPOTENCY_KEY", "")
            if re.fullmatch(r"[A-Za-z0-9_-]{16,80}", key) is None:
                return _error("400 Bad Request", "Provide a valid idempotency key.")
            body = _validated_request(environ)
            return jobs.forward("POST", "/jobs", body, key, owner=owner) if isinstance(body, bytes) else body
        if jobs is not None and _JOB_PATH.fullmatch(path) and (
            method == "GET" or (method == "DELETE" and not path.endswith("/telemetry"))
        ):
            return jobs.forward(method, path.removeprefix("/api"), owner=owner)
        return _error("404 Not Found", "Workspace route not found.")

    def application(environ: dict[str, Any], start_response: _StartResponse) -> Iterable[bytes]:
        try:
            status, content_type, body = route(environ)
        except OSError:
            status, content_type, body = _error("500 Internal Server Error", "Workspace resources are unavailable.")
        start_response(status, [*security_headers, ("Content-Type", content_type), ("Content-Length", str(len(body)))])
        return [body]

    return application


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--job-port", type=int, help="enable queued runs through the Java controller")
    parser.add_argument("--auth-alb", action="store_true", help="require verified AWS load balancer sign-in")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        from waitress import serve
    except ImportError:
        parser.exit(1, "Install the ORBIT web extra before serving the workspace.\n")
    jobs = None
    if args.job_port is not None:
        try:
            jobs = JobClient(args.job_port, os.environ.get("ORBIT_JOB_TOKEN", ""))
        except ValueError as exc:
            parser.error(str(exc))
    identity = None
    if args.auth_alb:
        if jobs is None:
            parser.error("--auth-alb requires --job-port")
        try:
            from .alb_identity import AlbConfig, AlbIdentity
        except ImportError:
            parser.exit(1, "Install the ORBIT hosted extra before enabling ALB sign-in.\n")
        try:
            identity = AlbIdentity(AlbConfig.from_environment(os.environ))
        except ValueError as exc:
            parser.error(str(exc))
    print(f"ORBIT workspace at http://127.0.0.1:{args.port}", flush=True)
    serve(create_app(args.port, jobs, identity), host="127.0.0.1", port=args.port, threads=4, connection_limit=32,
          channel_timeout=20, max_request_body_size=_MAX_PLAN_BYTES,
          max_request_header_size=32_768 if identity is not None else 8192)


if __name__ == "__main__":
    main()
