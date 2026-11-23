"""Command-line adapter for validated plans and atomic CSV output."""

import argparse
import csv
import sys
from dataclasses import fields
from hashlib import sha256
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from platform import python_version

from .errors import SimulationError
from .files import atomic_text_output
from .mechanics import OrbitalElements
from .mission import MAX_SAMPLES, Mission
from .plan import load_mission
from .propagation import propagate

_ELEMENT_COLUMNS = tuple(field.name for field in fields(OrbitalElements))
_ROW_LIMIT_EXIT = 3
_HEADER = (
    "time_s", "kind", "x_km", "y_km", "z_km", "vx_km_s", "vy_km_s", "vz_km_s",
    "altitude_km", "speed_km_s", "specific_energy_km2_s2", "mass_kg",
    *_ELEMENT_COLUMNS,
)


class _TelemetryLimit(SimulationError):
    """The telemetry writer reached its requested row limit."""


def _engine_report() -> str:
    sources = sorted((item for item in files("orbit_sim").iterdir() if item.name.endswith(".py")), key=lambda item: item.name)
    digest = sha256()
    for source in sources:
        digest.update(source.name.encode("utf-8") + b"\0" + sha256(source.read_bytes()).digest())
    return "|".join((version("orbit-mission-sim"), digest.hexdigest(), python_version(), version("numpy"), version("scipy")))


def _write_telemetry(mission: Mission, destination: Path, max_rows: int = MAX_SAMPLES) -> tuple[int, str]:
    count = 0
    outcome = ""
    with atomic_text_output(destination) as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(_HEADER)
        for sample in propagate(mission):
            if count == max_rows:
                raise _TelemetryLimit(f"mission exceeds the {max_rows} telemetry row budget")
            writer.writerow((
                sample.time_s, sample.kind, *sample.state.position_km, *sample.state.velocity_km_s,
                sample.altitude_km, sample.speed_km_s, sample.specific_energy_km2_s2, sample.mass_kg,
                *(getattr(sample.elements, name) for name in _ELEMENT_COLUMNS),
            ))
            count += 1
            outcome = sample.kind
    return count, outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Propagate one ORBIT mission and write telemetry.")
    parser.add_argument("--max-rows", type=int, default=MAX_SAMPLES, help="maximum telemetry rows, excluding the header")
    parser.add_argument("--engine-report", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("mission", type=Path, help="version 1 JSON mission plan")
    parser.add_argument("output", type=Path, help="new telemetry CSV in an existing directory")
    args = parser.parse_args(argv)
    if not 1 <= args.max_rows <= MAX_SAMPLES:
        parser.error(f"--max-rows must be between 1 and {MAX_SAMPLES}")
    try:
        mission = load_mission(args.mission)
        if args.engine_report is not None:
            with atomic_text_output(args.engine_report) as report:
                report.write(_engine_report() + "\n")
        count, outcome = _write_telemetry(mission, args.output, args.max_rows)
    except (OSError, ValueError, SimulationError) as exc:
        # Escape control characters before sending untrusted input details to a terminal.
        message = str(exc)[:320]
        print(f"ORBIT error: {message!r}", file=sys.stderr)
        return _ROW_LIMIT_EXIT if isinstance(exc, _TelemetryLimit) else 1
    print(f"ORBIT {outcome}: {count} telemetry rows")
    return 0
