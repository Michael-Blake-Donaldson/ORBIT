"""Import an offline JPL Horizons vector table into an ORBIT mission plan."""

import argparse
import csv
import json
import sys
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from .files import atomic_text_output, read_text
from .mission import MAX_EPHEMERIS_POINTS, EphemerisPoint, ThirdBody
from .plan import _MAX_PLAN_BYTES, _parse_mission, _read_plan_values

_MAX_TABLE_BYTES = 512 * 1024
_SECONDS_PER_DAY = 86_400
_COLUMNS = ["JDTDB", "Calendar Date (TDB)", "X", "Y", "Z", "VX", "VY", "VZ"]
_METADATA = {
    "Center-site name": "BODY CENTER",
    "Reference frame": "ICRF",
    "Output units": "KM-S",
    "Output type": "GEOMETRIC cartesian states",
    "Output format": "2 (position and velocity)",
}


def _strip_trailing_comma(values: list[str]) -> list[str]:
    return values[:-1] if values and not values[-1].strip() else values


def _validate_header(header: str) -> None:
    fields = {}
    has_columns = False
    for line in header.splitlines():
        name, separator, value = line.partition(":")
        if separator:
            fields[name.strip()] = value.strip()
        if _strip_trailing_comma([field.strip() for field in line.split(",")]) == _COLUMNS:
            has_columns = True
    center = fields.get("Center body name", "").split()
    if center[:2] != ["Earth", "(399)"]:
        raise ValueError("Horizons vectors must be centered on Earth (399)")
    for field, required in _METADATA.items():
        if fields.get(field) != required:
            raise ValueError(f"Horizons {field} must be {required}")
    if not has_columns:
        raise ValueError("Horizons must provide CSV position and velocity columns with Julian dates in TDB")


def _julian_date(value: str) -> Decimal:
    try:
        date = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid Horizons Julian date") from exc
    if not date.is_finite() or not 1_000_000 <= date <= 4_000_000:
        raise ValueError("Horizons Julian date must be finite and within the supported range")
    return date


def _read_vectors(path: Path) -> tuple[Decimal, tuple[EphemerisPoint, ...]]:
    raw = read_text(path, _MAX_TABLE_BYTES, "Horizons export")
    header, start, tail = raw.partition("$$SOE")
    table, end, remaining = tail.partition("$$EOE")
    if not start or not end or "$$SOE" in table or "$$SOE" in remaining:
        raise ValueError("Horizons export must contain one complete vector table")
    _validate_header(header)

    epoch = None
    points = []
    for row in csv.reader(table.strip().splitlines(), skipinitialspace=True, strict=True):
        if len(points) == MAX_EPHEMERIS_POINTS:
            raise ValueError(f"Horizons ephemeris exceeds the {MAX_EPHEMERIS_POINTS}-point limit")
        row = _strip_trailing_comma(row)
        if len(row) != len(_COLUMNS):
            raise ValueError("Horizons rows must include Julian date, calendar date, and six vector components")
        date = _julian_date(row[0])
        if epoch is None:
            epoch = date
        # Horizons prints Julian dates at limited precision. Preserve the millisecond grid.
        time_s = round(float((date - epoch) * _SECONDS_PER_DAY), 3)
        point = EphemerisPoint(time_s, tuple(map(float, row[2:5])), tuple(map(float, row[5:8])))
        points.append(point)
    if epoch is None:
        raise ValueError("Horizons ephemeris contains no rows")
    return epoch, tuple(points)


def _import_plan(
    source: Path, plan: Path, destination: Path, name: str, mu: float, minimum_separation: float,
) -> int:
    epoch, points = _read_vectors(source)
    body = ThirdBody(name, mu, points, minimum_separation)
    values = _read_plan_values(plan)
    mission = _parse_mission(values)
    if mission.epoch_jd_tdb is None:
        if mission.third_bodies:
            raise ValueError("set epoch_jd_tdb before importing into a plan that already has third bodies")
        values["epoch_jd_tdb"] = float(epoch)
    else:
        offset = abs((epoch - Decimal(str(mission.epoch_jd_tdb))) * _SECONDS_PER_DAY)
        if offset > Decimal("0.0001"):
            raise ValueError("Horizons must start at the mission's epoch_jd_tdb")
    entries = cast("list[dict[str, object]]", values.setdefault("third_bodies", []))
    entry = asdict(body)
    entry["ephemeris"] = list(entry["ephemeris"])
    entries.append(entry)
    _parse_mission(values)
    encoded = json.dumps(values, indent=2, allow_nan=False) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_PLAN_BYTES:
        raise ValueError("imported mission exceeds the 256 KiB plan limit")
    with atomic_text_output(destination) as file:
        file.write(encoded)
    return len(points)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vectors", type=Path, help="Horizons text export containing an ICRF Cartesian vector table")
    parser.add_argument("mission", type=Path, help="existing ORBIT mission plan")
    parser.add_argument("output", type=Path, help="new mission plan path")
    parser.add_argument("--name", required=True, help="unique name for the perturbing body")
    parser.add_argument("--mu", required=True, type=float, help="body gravitational parameter in km^3/s^2")
    parser.add_argument("--minimum-separation-km", type=float, default=1.0, help="force exclusion radius")
    args = parser.parse_args(argv)
    try:
        count = _import_plan(args.vectors, args.mission, args.output, args.name, args.mu, args.minimum_separation_km)
    except (OSError, ValueError, csv.Error) as exc:
        print(f"ORBIT Horizons import error: {str(exc)[:320]!r}", file=sys.stderr)
        return 1
    print(f"Imported {count} Horizons ephemeris points")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
