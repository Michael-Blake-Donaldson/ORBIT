"""Private comparison worker for two verified saved mission plans."""

import argparse
import csv
import io
import json
from math import ceil
from pathlib import Path

from .cli import _engine_report
from .compare import _HEADER, _csv_row
from .comparison import _comparison_interval, compare_trajectories
from .errors import SimulationError
from .files import atomic_text_output
from .plan import load_mission


def _compare(baseline: Path, candidate: Path, max_rows: int) -> dict[str, object]:
    reference, variant = load_mission(baseline), load_mission(candidate)
    interval = _comparison_interval(reference, variant)
    if ceil(reference.duration_s / interval) + 1 > max_rows:
        raise ValueError(f"The shared cadence exceeds the {max_rows:,}-row comparison limit.")

    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(_HEADER)
    count = 0
    final = None
    for row in compare_trajectories(reference, variant, interval):
        if count == max_rows:
            raise SimulationError("The comparison exceeded its row limit.")
        writer.writerow(_csv_row(row))
        final = row
        count += 1
    if final is None:
        raise SimulationError("The runs have no shared samples.")
    orbit, source, python, numpy, scipy = _engine_report().split("|")
    return {
        "csv": stream.getvalue(), "rows": count, "interval_s": interval, "duration_s": reference.duration_s,
        "impact_limited": final.time_s < reference.duration_s or "impact" in (final.reference_kind, final.candidate_kind),
        "engine": {"orbit_version": orbit, "source_sha256": source, "python_version": python,
                   "numpy_version": numpy, "scipy_version": scipy},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("max_rows", type=int, choices=range(1, 4001), metavar="MAX_ROWS")
    args = parser.parse_args()
    status = 0
    try:
        reply = _compare(args.baseline, args.candidate, args.max_rows)
    except (ValueError, SimulationError) as error:
        reply = {"message": str(error)[:240]}
        status = 3
    with atomic_text_output(args.output) as file:
        json.dump(reply, file, allow_nan=False, separators=(",", ":"))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
