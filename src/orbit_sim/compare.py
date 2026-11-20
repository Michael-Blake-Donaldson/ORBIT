"""Export position and velocity differences between two aligned mission plans."""

import argparse
import csv
import sys
from pathlib import Path

from .comparison import compare_trajectories
from .errors import SimulationError
from .files import atomic_text_output
from .plan import load_mission

_HEADER = (
    "time_s", "reference_kind", "candidate_kind", "position_difference_km", "velocity_difference_km_s",
    "delta_x_km", "delta_y_km", "delta_z_km", "delta_vx_km_s", "delta_vy_km_s", "delta_vz_km_s",
    "delta_r_km", "delta_t_km", "delta_n_km", "delta_vr_km_s", "delta_vt_km_s", "delta_vn_km_s",
)
_UNDEFINED_RTN = (None, None, None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="reference ORBIT mission")
    parser.add_argument("candidate", type=Path, help="candidate ORBIT mission")
    parser.add_argument("output", type=Path, help="new comparison CSV path")
    parser.add_argument("--interval-s", type=float, help="comparison cadence, defaulting to the coarser input cadence")
    args = parser.parse_args(argv)
    try:
        reference, candidate = load_mission(args.reference), load_mission(args.candidate)
        count = 0
        peak = end = 0.0
        with atomic_text_output(args.output) as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(_HEADER)
            for row in compare_trajectories(reference, candidate, args.interval_s):
                difference = row.position_difference_km
                writer.writerow((
                    row.time_s, row.reference_kind, row.candidate_kind, difference, row.velocity_difference_km_s,
                    *row.position_delta_eci_km, *row.velocity_delta_eci_km_s,
                    *(row.position_delta_rtn_km or _UNDEFINED_RTN), *(row.velocity_delta_rtn_km_s or _UNDEFINED_RTN),
                ))
                count += 1
                peak = max(peak, difference)
                end = row.time_s
    except (OSError, ValueError, SimulationError) as exc:
        print(f"ORBIT comparison error: {str(exc)[:320]!r}", file=sys.stderr)
        return 1
    print(f"Compared {count} states through {end:g} s. Peak sampled separation: {peak:.6g} km.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
