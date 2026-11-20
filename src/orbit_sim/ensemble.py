"""Export seeded mission dispersion trials or their final covariance summary."""

import argparse
import csv
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .errors import SimulationError
from .files import atomic_text_output, read_json_object
from .plan import load_mission
from .uncertainty import InitialUncertainty, run_ensemble, summarize_ensemble

_STATE_COLUMNS = ("x_km", "y_km", "z_km", "vx_km_s", "vy_km_s", "vz_km_s")
_TRIAL_COLUMNS = (
    "trial", "outcome", "elapsed_s",
    *(f"initial_{name}" for name in _STATE_COLUMNS),
    *(f"final_{name}" for name in _STATE_COLUMNS),
)


def _load_uncertainty(path: Path) -> InitialUncertainty:
    values = read_json_object(path, 16 * 1024, "dispersion configuration")
    try:
        return InitialUncertainty(**values)
    except TypeError as exc:
        raise ValueError(f"invalid dispersion configuration fields: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mission", type=Path, help="base ORBIT mission plan")
    parser.add_argument("uncertainty", type=Path, help="initial-state covariance and seeded trial configuration")
    parser.add_argument("output", type=Path, help="new summary JSON or trial CSV path")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    args = parser.parse_args(argv)
    try:
        mission = load_mission(args.mission)
        uncertainty = _load_uncertainty(args.uncertainty)
        outcomes = run_ensemble(mission, uncertainty)
        with atomic_text_output(args.output) as file:
            if args.format == "json":
                summary = summarize_ensemble(outcomes)
                json.dump(
                    {"seed": uncertainty.seed, "trials": uncertainty.trials, "state_order": _STATE_COLUMNS,
                     **asdict(summary)},
                    file, indent=2, allow_nan=False,
                )
                file.write("\n")
                completed, impacted = summary.completed, summary.impacted
            else:
                writer = csv.writer(file, lineterminator="\n")
                writer.writerow(_TRIAL_COLUMNS)
                completed = impacted = 0
                for trial in outcomes:
                    writer.writerow((
                        trial.trial, trial.outcome, trial.elapsed_s, *trial.initial_state.position_km,
                        *trial.initial_state.velocity_km_s, *trial.final_state.position_km,
                        *trial.final_state.velocity_km_s,
                    ))
                    completed += int(trial.outcome == "complete")
                    impacted += int(trial.outcome == "impact")
    except (OSError, ValueError, SimulationError) as exc:
        print(f"ORBIT dispersion error: {str(exc)[:320]!r}", file=sys.stderr)
        return 1
    print(f"Exported {completed} completed trials and {impacted} impacts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
