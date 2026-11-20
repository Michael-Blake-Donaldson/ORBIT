"""Find geometric ground-station passes along the propagated trajectory."""

from dataclasses import dataclass

from .ground import GroundStation, eci_to_ecef, look_angles
from .intervals import TimeInterval, scan_intervals
from .mechanics import Array
from .mission import Mission, number


@dataclass(frozen=True, slots=True)
class AccessWindow(TimeInterval):
    """Elapsed start and end times of a geometric station pass."""


def access_windows(
    mission: Mission, station: GroundStation, greenwich_angle_deg_at_epoch: float,
) -> tuple[AccessWindow, ...]:
    """Locate intervals above the elevation mask, clipped to the actual trajectory."""
    angle = number(greenwich_angle_deg_at_epoch, "Greenwich angle", -360, 360)

    def evaluate(time: float, position: Array) -> tuple[float]:
        ecef = eci_to_ecef(tuple(map(float, position)), time, angle)
        elevation = look_angles(ecef, station).elevation_deg
        return (-180 if elevation is None else elevation - station.minimum_elevation_deg,)

    (windows,) = scan_intervals(mission, evaluate, 1)
    return tuple(AccessWindow(window.start_s, window.end_s) for window in windows)
