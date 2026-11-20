"""Export solar generation and battery energy as a mission CSV."""

import argparse
import csv
import sys
from dataclasses import fields
from pathlib import Path

from .errors import SimulationError
from .files import atomic_text_output
from .plan import load_mission
from .power import PowerSample, mission_power_budget

_COLUMNS = tuple(field.name for field in fields(PowerSample))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mission", type=Path, help="ORBIT mission containing power_system and a Sun ephemeris")
    parser.add_argument("output", type=Path, help="new power budget CSV path")
    args = parser.parse_args(argv)
    try:
        mission = load_mission(args.mission)
        with atomic_text_output(args.output) as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(_COLUMNS)
            for row in mission_power_budget(mission):
                writer.writerow(getattr(row, name) for name in _COLUMNS)
    except (OSError, ValueError, SimulationError) as exc:
        print(f"ORBIT power budget error: {str(exc)[:320]!r}", file=sys.stderr)
        return 1
    print(f"Battery at end: {row.battery_wh:.3f} Wh. Unserved load: {row.unserved_wh:.3f} Wh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
