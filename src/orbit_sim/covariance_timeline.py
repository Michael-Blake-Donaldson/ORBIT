"""Export the linearized ECI covariance timeline for an ORBIT mission."""

import argparse
import csv
import sys
from math import hypot, sqrt
from pathlib import Path

from .errors import SimulationError
from .files import atomic_text_output
from .plan import load_mission
from .sensitivity import propagate_covariance
from .uncertainty import _STATE_COLUMNS, load_initial_uncertainty

_TRIANGLE = tuple((i, j) for i in range(len(_STATE_COLUMNS)) for j in range(i + 1))
_CSV_HEADER = (
    "time_s", "kind", "position_rms_km", "velocity_rms_km_s",
    *(f"cov_{_STATE_COLUMNS[i]}__{_STATE_COLUMNS[j]}" for i, j in _TRIANGLE),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mission", type=Path, help="ORBIT mission plan")
    parser.add_argument("uncertainty", type=Path, help="dispersion configuration providing the initial covariance")
    parser.add_argument("output", type=Path, help="new covariance CSV path")
    args = parser.parse_args(argv)
    try:
        mission = load_mission(args.mission)
        uncertainty = load_initial_uncertainty(args.uncertainty)
        count = 0
        with atomic_text_output(args.output) as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(_CSV_HEADER)
            for count, row in enumerate(propagate_covariance(mission, uncertainty.covariance_eci), 1):
                covariance = row.covariance_eci
                diagonal = tuple(sqrt(covariance[i][i]) for i in range(len(_STATE_COLUMNS)))
                writer.writerow((
                    row.sample.time_s, row.sample.kind, hypot(*diagonal[:3]), hypot(*diagonal[3:]),
                    *(covariance[i][j] for i, j in _TRIANGLE),
                ))
    except (OSError, ValueError, SimulationError) as exc:
        print(f"ORBIT covariance error: {str(exc)[:320]!r}", file=sys.stderr)
        return 1
    print(f"Exported {count} covariance samples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
