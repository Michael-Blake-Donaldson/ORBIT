"""Measure propagation and telemetry construction without Python startup or CSV I/O."""

import argparse
import cProfile
import pstats
from statistics import median
from time import perf_counter

from orbit_sim import Mission, load_mission, propagate


def count_telemetry(mission: Mission) -> int:
    return sum(1 for _ in propagate(mission))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mission", help="path to a version 1 JSON mission plan")
    parser.add_argument("--runs", type=int, default=5, help="timed runs after warm-up (1–30)")
    parser.add_argument("--profile", action="store_true", help="show the functions with the most internal time")
    args = parser.parse_args()
    if not 1 <= args.runs <= 30:
        parser.error("--runs must be between 1 and 30")
    mission = load_mission(args.mission)
    rows = count_telemetry(mission)
    timings = []
    for _ in range(args.runs):
        start = perf_counter()
        count_telemetry(mission)
        timings.append(perf_counter() - start)
    elapsed = median(timings)
    print(f"{rows:,} rows, median {elapsed:.4f} s across {args.runs} runs ({rows / elapsed:,.0f} rows/s)")
    if args.profile:
        profiler = cProfile.Profile()
        profiler.runcall(count_telemetry, mission)
        pstats.Stats(profiler).strip_dirs().sort_stats("tottime").print_stats(15)


if __name__ == "__main__":
    main()
