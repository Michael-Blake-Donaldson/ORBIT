"""Export a mission's eclipse phases as elapsed-time CSV."""

import argparse
import csv
import sys
from pathlib import Path

from .errors import SimulationError
from .files import atomic_text_output
from .illumination import NOMINAL_SOLAR_RADIUS_KM, eclipse_windows, solar_samples
from .plan import load_mission


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mission", type=Path, help="version 1 JSON mission containing a solar ephemeris")
    parser.add_argument("output", type=Path, help="new eclipse CSV path")
    parser.add_argument("--sun-name", default="Sun", help="third-body name for the solar ephemeris")
    parser.add_argument("--sun-radius-km", type=float, default=NOMINAL_SOLAR_RADIUS_KM)
    parser.add_argument("--format", choices=("windows", "samples"), default="windows")
    args = parser.parse_args(argv)
    try:
        mission = load_mission(args.mission)
        with atomic_text_output(args.output) as file:
            writer = csv.writer(file, lineterminator="\n")
            if args.format == "windows":
                windows = eclipse_windows(mission, args.sun_name, args.sun_radius_km)
                writer.writerow(("kind", "start_s", "end_s", "duration_s"))
                writer.writerows((row.kind, row.start_s, row.end_s, row.duration_s) for row in windows)
                summary = f"{len(windows)} eclipse phases"
            else:
                writer.writerow(("time_s", "kind", "visible_solar_fraction"))
                count = 0
                for row in solar_samples(mission, args.sun_name, args.sun_radius_km):
                    writer.writerow((row.time_s, row.kind, row.visible_fraction))
                    count += 1
                summary = f"{count} solar-visibility samples"
    except (OSError, ValueError, SimulationError) as exc:
        print(f"ORBIT eclipse error: {str(exc)[:320]!r}", file=sys.stderr)
        return 1
    print(f"Exported {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
