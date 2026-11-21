"""Launch a packaged service with a credential read from its mounted secret."""

import os
import re
import sys
from http.client import HTTPConnection, HTTPException
from pathlib import Path

_PYTHON = "/opt/venv/bin/python"
_TOKEN_PATH = Path("/run/secrets/orbit_job_token")


def _credential() -> str:
    with _TOKEN_PATH.open("rb") as file:
        raw = file.read(131)
    if len(raw) > 130:
        raise ValueError("The job token secret is too large.")
    token = raw.removesuffix(b"\n").removesuffix(b"\r")
    if re.fullmatch(rb"[A-Za-z0-9_-]{32,128}", token) is None:
        raise ValueError("The job token secret is invalid.")
    return token.decode("ascii")


def _check(port: int, path: str, headers: dict[str, str]) -> int:
    connection = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path, headers=headers)
        return int(connection.getresponse().status != 200)
    except (OSError, HTTPException):
        return 1
    finally:
        connection.close()


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    mode = sys.argv[1]
    if mode == "check-workspace":
        return _check(8765, "/readyz", {})
    try:
        token = _credential()
    except (OSError, ValueError):
        print("Mount a readable, valid orbit_job_token secret.", file=sys.stderr)
        return 2
    if mode == "check-controller":
        return _check(8766, "/health", {"Authorization": f"Bearer {token}"})
    if mode == "controller":
        command = ["/opt/java/bin/java", "-XX:MaxRAMPercentage=25", "-jar", "/opt/orbit/controller.jar",
                   "--serve", _PYTHON, "8766", "/var/lib/orbit"]
    elif mode == "workspace":
        require_alb = os.environ.get("ORBIT_REQUIRE_ALB", "1")
        if require_alb not in ("0", "1"):
            print("Set ORBIT_REQUIRE_ALB to 0 or 1.", file=sys.stderr)
            return 2
        command = [_PYTHON, "-m", "orbit_sim.web", "--job-port", "8766"]
        if require_alb == "1":
            command.append("--auth-alb")
    else:
        return 2
    os.execve(command[0], command, dict(os.environ, ORBIT_JOB_TOKEN=token))


if __name__ == "__main__":
    raise SystemExit(main())
