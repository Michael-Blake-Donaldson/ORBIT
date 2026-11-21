"""Loopback HTTP adapter for the ORBIT browser workspace."""

import argparse
import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable
from importlib.resources import files
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from typing import Any

from .files import parse_json_object
from .plan import _MAX_PLAN_BYTES, _parse_mission

MAX_WEB_SAMPLES = 4000
_MAX_CSV_BYTES = 4 * 1024 * 1024
_TIMEOUT_S = 15
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


def _mission_request(environ: dict[str, Any], run: bool, slots: BoundedSemaphore) -> _Reply:
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


def create_app(port: int) -> Callable[[dict[str, Any], _StartResponse], Iterable[bytes]]:
    """Build the local workspace app. The preview accepts only loopback authorities."""
    authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
    assets = files("orbit_sim").joinpath("web_assets")
    slots = BoundedSemaphore(2)

    def route(environ: dict[str, Any]) -> _Reply:
        host = environ.get("HTTP_HOST", "").lower()
        if host not in authorities:
            return _error("403 Forbidden", "Open this workspace through its loopback address.")
        path, method = environ["PATH_INFO"], environ["REQUEST_METHOD"]
        if method == "GET" and path in _ASSETS:
            name, content_type = _ASSETS[path]
            return "200 OK", content_type, assets.joinpath(name).read_bytes()
        if method == "POST" and path in ("/api/validate", "/api/run"):
            origin = environ.get("HTTP_ORIGIN")
            if (origin is not None and origin != f"http://{host}") or environ.get("HTTP_SEC_FETCH_SITE") == "cross-site":
                return _error("403 Forbidden", "Only same-origin workspace requests are accepted.")
            return _mission_request(environ, path == "/api/run", slots)
        return _error("404 Not Found", "Workspace route not found.")

    def application(environ: dict[str, Any], start_response: _StartResponse) -> Iterable[bytes]:
        try:
            status, content_type, body = route(environ)
        except OSError:
            status, content_type, body = _error("500 Internal Server Error", "Workspace resources are unavailable.")
        start_response(status, [*_SECURITY_HEADERS, ("Content-Type", content_type), ("Content-Length", str(len(body)))])
        return [body]

    return application


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        from waitress import serve
    except ImportError:
        parser.exit(1, "Install the ORBIT web extra before serving the workspace.\n")
    print(f"ORBIT workspace at http://127.0.0.1:{args.port}", flush=True)
    serve(create_app(args.port), host="127.0.0.1", port=args.port, threads=4, connection_limit=32,
          channel_timeout=20, max_request_body_size=_MAX_PLAN_BYTES, max_request_header_size=8192)


if __name__ == "__main__":
    main()
