"""Export a mission to a CCSDS Orbit Ephemeris Message (OEM 3.0, KVN)."""

import argparse
import sys
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import inf, nextafter
from pathlib import Path
from typing import TextIO

from scipy.integrate import DenseOutput

from .errors import SimulationError
from .files import atomic_text_output
from .mission import Mission
from .plan import load_mission
from .propagation import _bounded_propagate

_FRAMES = ("ICRF", "EME2000")
_NS_PER_SECOND = 1_000_000_000
_NS_PER_DAY = 86_400 * _NS_PER_SECOND
_J2000 = datetime(2000, 1, 1, 12)


def _format_tdb(nanoseconds: int) -> str:
    seconds, fraction = divmod(nanoseconds, _NS_PER_SECOND)
    try:
        date = _J2000 + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError("mission epoch is outside the OEM calendar range") from exc
    return f"{date.isoformat(timespec='seconds')}.{fraction:09d}"


def _validate_metadata_text(value: str, field: str) -> None:
    if not 1 <= len(value) <= 128 or not value.isascii() or not value.isprintable() or value != value.strip():
        raise ValueError(f"{field} must have 1 to 128 printable ASCII characters without surrounding spaces")
    if "=" in value:
        raise ValueError(f"{field} cannot contain '='")


@dataclass(frozen=True, slots=True)
class _Metadata:
    frame: str
    object_name: str
    object_id: str
    originator: str

    def __post_init__(self) -> None:
        if self.frame not in _FRAMES:
            raise ValueError("OEM frame must be ICRF or EME2000")
        _validate_metadata_text(self.object_name, "OBJECT_NAME")
        _validate_metadata_text(self.object_id, "OBJECT_ID")
        _validate_metadata_text(self.originator, "ORIGINATOR")


def _export_oem(file: TextIO, mission: Mission, metadata: _Metadata) -> tuple[int, int]:
    if mission.epoch_jd_tdb is None:
        raise ValueError("OEM export requires epoch_jd_tdb")
    origin_ns = round((Decimal(str(mission.epoch_jd_tdb)) - Decimal(2451545)) * _NS_PER_DAY)
    _format_tdb(origin_ns)
    created = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    file.write(f"CCSDS_OEM_VERS = 3.0\nCREATION_DATE = {created}\nORIGINATOR = {metadata.originator}\n")

    rows: list[str] = []
    segments = count = 0
    last_ns = first_ns = None
    last_time = None
    last_step: tuple[float, float, DenseOutput] | None = None

    def observe_step(start: float, end: float, dense: DenseOutput) -> None:
        nonlocal last_step
        last_step = start, end, dense

    def append(elapsed: float, components: tuple[float, ...]) -> None:
        nonlocal first_ns, last_ns, last_time
        epoch_ns = origin_ns + round(elapsed * _NS_PER_SECOND)
        if last_ns is not None and epoch_ns <= last_ns:
            raise SimulationError("trajectory timestamps collide at OEM nanosecond resolution")
        if first_ns is None:
            first_ns = epoch_ns
        last_ns, last_time = epoch_ns, elapsed
        values = " ".join(format(value, ".17g") for value in components)
        rows.append(f"{_format_tdb(epoch_ns)} {values}\n")

    def flush() -> None:
        nonlocal segments, count, first_ns
        if first_ns is None or last_ns is None:
            return
        file.write(
            "\nMETA_START\n"
            f"OBJECT_NAME = {metadata.object_name}\nOBJECT_ID = {metadata.object_id}\n"
            f"CENTER_NAME = EARTH\nREF_FRAME = {metadata.frame}\nTIME_SYSTEM = TDB\n"
            f"START_TIME = {_format_tdb(first_ns)}\nSTOP_TIME = {_format_tdb(last_ns)}\n"
            "META_STOP\n\n"
        )
        file.writelines(rows)
        segments += 1
        count += len(rows)
        rows.clear()
        first_ns = None

    # Regular samples plus maneuver and terminal boundaries make up the ephemeris.
    impulse_times = {impulse.time_s for impulse in mission.impulses}
    observer = observe_step if impulse_times else None
    for sample in _bounded_propagate(replace(mission, track_apsides=False), observer):
        elapsed = sample.time_s
        if sample.kind == "burn_end" and elapsed in impulse_times:
            # The pre-burn sample below represents this cutoff on the earlier segment.
            continue
        if sample.kind == "pre_burn":
            # Retain a left-hand state evaluated just before the impulse. Distinct time tags
            # keep the consecutive blocks disjoint under CCSDS 502.0-B-3 section 5.2.4.4.
            if last_step is not None and last_time is not None:
                start, end, dense = last_step
                left = max(start, nextafter(elapsed - 1 / _NS_PER_SECOND, -inf))
                if (
                    last_time < left < end == elapsed
                    and round(last_time * _NS_PER_SECOND)
                    < round(left * _NS_PER_SECOND)
                    < round(elapsed * _NS_PER_SECOND)
                ):
                    append(left, tuple(map(float, dense(left)[:6])))
            continue
        if sample.kind == "post_burn":
            flush()
        elif sample.time_s == last_time:
            # A finite-burn cutoff may be coincident with mission completion.
            continue
        append(elapsed, (*sample.state.position_km, *sample.state.velocity_km_s))
    flush()
    return count, segments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mission", type=Path, help="ORBIT mission with epoch_jd_tdb")
    parser.add_argument("output", type=Path, help="new OEM path")
    parser.add_argument("--frame", required=True, choices=_FRAMES, help="inertial axes already used by the mission")
    parser.add_argument("--object-name", default="UNKNOWN")
    parser.add_argument("--object-id", default="UNKNOWN")
    parser.add_argument("--originator", default="ORBIT")
    args = parser.parse_args(argv)
    try:
        metadata = _Metadata(args.frame, args.object_name, args.object_id, args.originator)
        mission = load_mission(args.mission)
        with atomic_text_output(args.output) as file:
            count, segments = _export_oem(file, mission, metadata)
    except (OSError, ValueError, SimulationError) as exc:
        print(f"ORBIT OEM error: {str(exc)[:320]!r}", file=sys.stderr)
        return 1
    print(f"Exported {count} ephemeris records in {segments} segment(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
