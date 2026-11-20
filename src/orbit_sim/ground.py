"""Rigid Earth rotation, WGS 84 geodetic coordinates, and station look angles."""

from dataclasses import dataclass
from math import atan2, copysign, cos, degrees, hypot, radians, sin, sqrt

from .mission import EARTH_RADIUS_KM, EARTH_ROTATION_RAD_S, Vector, number, vector

_FLATTENING = 1 / 298.257223563
_ECCENTRICITY_SQUARED = _FLATTENING * (2 - _FLATTENING)
_POLAR_RADIUS_KM = EARTH_RADIUS_KM * (1 - _FLATTENING)
_ANGLE_TOLERANCE = 1e-14


@dataclass(frozen=True, slots=True)
class GeodeticPosition:
    latitude_deg: float
    longitude_deg: float | None
    height_km: float


@dataclass(frozen=True, slots=True)
class GroundStation:
    latitude_deg: float
    longitude_deg: float
    height_km: float = 0.0
    minimum_elevation_deg: float = 0.0

    def __post_init__(self) -> None:
        for field, lower, upper in (
            ("latitude_deg", -90, 90),
            ("longitude_deg", -180, 180),
            ("height_km", -1, 100),
            ("minimum_elevation_deg", 0, 90),
        ):
            object.__setattr__(self, field, number(getattr(self, field), field, lower, upper))


@dataclass(frozen=True, slots=True)
class LookAngles:
    azimuth_deg: float | None
    elevation_deg: float | None
    slant_range_km: float
    visible: bool


def _curvature_radius(latitude: float) -> float:
    return EARTH_RADIUS_KM / sqrt(1 - _ECCENTRICITY_SQUARED * sin(latitude)**2)


def eci_to_ecef(position_km: Vector, time_s: float, greenwich_angle_deg_at_epoch: float) -> Vector:
    """Rotate about the shared north axis using the epoch Greenwich angle measured eastward."""
    x, y, z = vector(position_km, "ECI position_km", 1e12)
    time = number(time_s, "time_s", 0, 2_592_000)
    angle = number(greenwich_angle_deg_at_epoch, "Greenwich angle", -360, 360)
    theta = radians(angle) + time * EARTH_ROTATION_RAD_S
    c, s = cos(theta), sin(theta)
    return c * x + s * y, -s * x + c * y, z


def ecef_to_geodetic(position_km: Vector) -> GeodeticPosition:
    """Return WGS 84 latitude, east longitude, and height along the ellipsoid normal."""
    x, y, z = vector(position_km, "ECEF position_km", 2e12)
    horizontal = hypot(x, y)
    distance = hypot(horizontal, z)
    if distance < _POLAR_RADIUS_KM / 2:
        raise ValueError("geodetic conversion requires a point outside Earth's deep interior")
    if horizontal <= distance * _ANGLE_TOLERANCE:
        return GeodeticPosition(copysign(90.0, z), None, abs(z) - _POLAR_RADIUS_KM)

    latitude = atan2(z, horizontal * (1 - _ECCENTRICITY_SQUARED))
    for _ in range(12):
        updated = atan2(z + _ECCENTRICITY_SQUARED * _curvature_radius(latitude) * sin(latitude), horizontal)
        if abs(updated - latitude) <= _ANGLE_TOLERANCE:
            latitude = updated
            break
        latitude = updated
    else:
        raise ValueError("geodetic latitude failed to converge")
    # Project the displacement onto the ellipsoid normal. Avoid division by cos(latitude).
    support = EARTH_RADIUS_KM * sqrt(1 - _ECCENTRICITY_SQUARED * sin(latitude)**2)
    height = horizontal * cos(latitude) + z * sin(latitude) - support
    longitude = degrees(atan2(y, x))
    return GeodeticPosition(degrees(latitude), -180.0 if longitude == 180 else longitude, height)


def look_angles(position_ecef_km: Vector, station: GroundStation) -> LookAngles:
    """Measure geometric range, elevation, and clockwise azimuth from true north."""
    x, y, z = vector(position_ecef_km, "ECEF position_km", 2e12)
    latitude, longitude = radians(station.latitude_deg), radians(station.longitude_deg)
    sin_lat, cos_lat = sin(latitude), cos(latitude)
    sin_lon, cos_lon = sin(longitude), cos(longitude)
    radius = _curvature_radius(latitude)
    equatorial = (radius + station.height_km) * cos_lat
    dx = x - equatorial * cos_lon
    dy = y - equatorial * sin_lon
    dz = z - (radius * (1 - _ECCENTRICITY_SQUARED) + station.height_km) * sin_lat
    meridian = cos_lon * dx + sin_lon * dy
    east = -sin_lon * dx + cos_lon * dy
    north = -sin_lat * meridian + cos_lat * dz
    up = cos_lat * meridian + sin_lat * dz
    horizontal = hypot(east, north)
    distance = hypot(horizontal, up)
    if distance <= 1e-9:
        return LookAngles(None, None, distance, False)
    elevation = degrees(atan2(up, horizontal))
    azimuth = None
    if horizontal > distance * _ANGLE_TOLERANCE:
        clockwise = degrees(atan2(east, north)) % 360
        azimuth = 0.0 if clockwise == 360 else clockwise
    return LookAngles(azimuth, elevation, distance, elevation >= station.minimum_elevation_deg)
