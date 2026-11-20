"""Export an ORBIT mission's sampled ground track and station look angles."""

import argparse
import csv
import json
import sys
from collections.abc import Iterable, Iterator
from math import copysign
from pathlib import Path

from .access import access_windows
from .errors import SimulationError
from .files import atomic_text_output
from .ground import (
    GeodeticPosition,
    GroundStation,
    ecef_to_geodetic,
    eci_to_ecef,
    look_angles,
)
from .mission import Mission, Vector
from .plan import load_mission
from .propagation import Sample, propagate

_Coordinate = tuple[float, float]
_HEADER = (
    "time_s", "kind", "subpoint_latitude_deg", "subpoint_longitude_deg", "ellipsoid_height_km",
    "station_azimuth_deg", "station_elevation_deg", "station_slant_range_km", "station_visible",
)


def _ground_samples(mission: Mission, angle: float) -> Iterator[tuple[Sample, Vector, GeodeticPosition]]:
    for sample in propagate(mission):
        position = eci_to_ecef(sample.state.position_km, sample.time_s, angle)
        yield sample, position, ecef_to_geodetic(position)


def _write_csv(mission: Mission, angle: float, station: GroundStation | None, destination: Path) -> int:
    count = 0
    with atomic_text_output(destination) as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(_HEADER)
        for sample, ecef, point in _ground_samples(mission, angle):
            angles = look_angles(ecef, station) if station is not None else None
            writer.writerow((
                sample.time_s, sample.kind, point.latitude_deg, point.longitude_deg, point.height_km,
                angles.azimuth_deg if angles is not None else None,
                angles.elevation_deg if angles is not None else None,
                angles.slant_range_km if angles is not None else None,
                int(angles.visible) if angles is not None else None,
            ))
            count += 1
    return count


def _track_segments(points: Iterable[GeodeticPosition]) -> Iterator[list[_Coordinate]]:
    line: list[_Coordinate] = []
    mirrored_start = False
    for point in points:
        if point.longitude_deg is None:
            if line and (len(line) > 1 or not mirrored_start):
                yield line
            line = []
            mirrored_start = False
            continue
        longitude = -180.0 if point.longitude_deg == 180 else point.longitude_deg
        coordinate = (longitude, point.latitude_deg)
        if line:
            previous_lon, previous_lat = line[-1]
            delta = longitude - previous_lon
            if abs(delta) > 180:
                # Unwrap this edge, then put its boundary point on both sides of the seam.
                edge = copysign(180, -delta)
                unwrapped = longitude + 2 * edge
                portion = (edge - previous_lon) / (unwrapped - previous_lon)
                latitude = previous_lat + portion * (point.latitude_deg - previous_lat)
                crossing = (edge, latitude)
                if line[-1] != crossing:
                    line.append(crossing)
                if len(line) > 1:
                    yield line
                line = [(-edge, latitude)]
                mirrored_start = True
        if not line or line[-1] != coordinate:
            line.append(coordinate)
    if line and (len(line) > 1 or not mirrored_start):
        yield line


def _features(points: Iterable[GeodeticPosition], station: GroundStation | None) -> Iterator[dict[str, object]]:
    for line in _track_segments(points):
        geometry = {"type": "Point", "coordinates": line[0]} if len(line) == 1 else {
            "type": "LineString", "coordinates": line,
        }
        yield {"type": "Feature", "properties": {"kind": "ground_track"}, "geometry": geometry}
    if station is not None:
        yield {
            "type": "Feature",
            "properties": {
                "kind": "ground_station", "height_km": station.height_km,
                "minimum_elevation_deg": station.minimum_elevation_deg,
            },
            "geometry": {"type": "Point", "coordinates": [station.longitude_deg, station.latitude_deg]},
        }


def _write_geojson(mission: Mission, angle: float, station: GroundStation | None, destination: Path) -> int:
    points = (point for _, _, point in _ground_samples(mission, angle))
    count = 0
    with atomic_text_output(destination) as file:
        file.write('{"type":"FeatureCollection","features":[')
        for feature in _features(points, station):
            file.write(",\n" if count else "\n")
            json.dump(feature, file, allow_nan=False, separators=(",", ":"))
            count += 1
        file.write("\n]}\n")
    return count


def _write_access(mission: Mission, angle: float, station: GroundStation, destination: Path) -> int:
    with atomic_text_output(destination) as file:
        windows = access_windows(mission, station, angle)
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(("start_s", "end_s", "duration_s"))
        writer.writerows((window.start_s, window.end_s, window.duration_s) for window in windows)
    return len(windows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mission", type=Path, help="version 1 JSON mission plan")
    parser.add_argument("output", type=Path, help="new ground-track output path")
    parser.add_argument("--greenwich-angle-deg", type=float, required=True, help="eastward ECI angle of Greenwich at t=0")
    parser.add_argument("--format", choices=("csv", "geojson", "access"), default="csv")
    parser.add_argument("--station", type=float, nargs=2, metavar=("LAT_DEG", "LON_DEG"), help="ground station position")
    parser.add_argument("--station-height-km", type=float, help="station ellipsoidal height, default 0")
    parser.add_argument("--min-elevation-deg", type=float, help="geometric visibility mask, default 0")
    args = parser.parse_args(argv)
    if args.format == "access" and args.station is None:
        parser.error("--format access requires --station")
    if args.station is None and (args.station_height_km is not None or args.min_elevation_deg is not None):
        parser.error("--station-height-km and --min-elevation-deg require --station")
    try:
        mission = load_mission(args.mission)
        station = None
        if args.station is not None:
            height = 0 if args.station_height_km is None else args.station_height_km
            mask = 0 if args.min_elevation_deg is None else args.min_elevation_deg
            station = GroundStation(*args.station, height_km=height, minimum_elevation_deg=mask)
        if args.format == "csv":
            count = _write_csv(mission, args.greenwich_angle_deg, station, args.output)
            print(f"Exported {count} ground-track rows")
        elif args.format == "geojson":
            count = _write_geojson(mission, args.greenwich_angle_deg, station, args.output)
            print(f"Exported {count} GeoJSON features")
        else:
            assert station is not None  # Validated by the argument parser.
            count = _write_access(mission, args.greenwich_angle_deg, station, args.output)
            print(f"Exported {count} station access windows")
    except (OSError, ValueError, SimulationError) as exc:
        print(f"ORBIT ground-track error: {str(exc)[:320]!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
