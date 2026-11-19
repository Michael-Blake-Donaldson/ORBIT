"""Command-line adapter for validated plans and atomic CSV output."""

import argparse
import csv
import os
import sys
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory

from .mechanics import OrbitalElements
from .mission import Mission
from .plan import load_mission
from .propagation import SimulationError, propagate

_ELEMENT_COLUMNS = tuple(field.name for field in fields(OrbitalElements))
_HEADER = (
    "time_s", "kind", "x_km", "y_km", "z_km", "vx_km_s", "vy_km_s", "vz_km_s",
    "altitude_km", "speed_km_s", "specific_energy_km2_s2", "mass_kg",
    *_ELEMENT_COLUMNS,
)


def _write_telemetry(mission: Mission, destination: Path) -> tuple[int, str]:
    if os.path.lexists(destination):
        raise FileExistsError(f"output already exists: {destination}")
    count = 0
    outcome = ""
    with TemporaryDirectory(prefix=".orbit-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "telemetry.csv"
        with staged.open("x", encoding="utf-8", newline="") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(_HEADER)
            for sample in propagate(mission):
                writer.writerow((
                    sample.time_s, sample.kind, *sample.state.position_km, *sample.state.velocity_km_s,
                    sample.altitude_km, sample.speed_km_s, sample.specific_energy_km2_s2, sample.mass_kg,
                    *(getattr(sample.elements, name) for name in _ELEMENT_COLUMNS),
                ))
                count += 1
                outcome = sample.kind
            file.flush()
            os.fsync(file.fileno())
        # The link publishes an already closed file and fails if another writer won the race.
        os.link(staged, destination)
    return count, outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Propagate one ORBIT mission and write telemetry.")
    parser.add_argument("mission", type=Path, help="version 1 JSON mission plan")
    parser.add_argument("output", type=Path, help="new telemetry CSV in an existing directory")
    args = parser.parse_args(argv)
    try:
        mission = load_mission(args.mission)
        count, outcome = _write_telemetry(mission, args.output)
    except (OSError, ValueError, SimulationError) as exc:
        # Escape control characters before sending untrusted input details to a terminal.
        message = str(exc)[:320]
        print(f"ORBIT error: {message!r}", file=sys.stderr)
        return 1
    print(f"ORBIT {outcome}: {count} telemetry rows")
    return 0
