"""Validated input contracts and simulation limits."""

from dataclasses import dataclass
from math import ceil, hypot

EARTH_MU_KM3_S2 = 398600.4418
EARTH_RADIUS_KM = 6378.137
EARTH_J2 = 1.08262982131e-3
STANDARD_GRAVITY_KM_S2 = 0.00980665
MAX_SAMPLES = 100_000

Vector = tuple[float, float, float]


def number(value: object, field: str, low: float, high: float) -> float:
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError(f"{field} must be a finite number in [{low}, {high}]")
    return float(value)


def vector(value: object, field: str, limit: float) -> Vector:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        raise ValueError(f"{field} must contain exactly three numbers")
    x, y, z = (number(component, field, -limit, limit) for component in value)
    return x, y, z


@dataclass(frozen=True, slots=True)
class State:
    position_km: Vector
    velocity_km_s: Vector

    def __post_init__(self) -> None:
        object.__setattr__(self, "position_km", vector(self.position_km, "position_km", 1e12))
        object.__setattr__(self, "velocity_km_s", vector(self.velocity_km_s, "velocity_km_s", 1e5))


@dataclass(frozen=True, slots=True)
class Impulse:
    time_s: float
    delta_v_km_s: Vector
    frame: str = "ECI"

    def __post_init__(self) -> None:
        object.__setattr__(self, "time_s", number(self.time_s, "time_s", 0, 2_592_000))
        object.__setattr__(self, "delta_v_km_s", vector(self.delta_v_km_s, "delta_v_km_s", 10))
        if self.frame not in ("ECI", "RTN"):
            raise ValueError("impulse frame must be ECI or RTN")


@dataclass(frozen=True, slots=True)
class Spacecraft:
    initial_mass_kg: float
    dry_mass_kg: float
    isp_s: float

    def __post_init__(self) -> None:
        initial = number(self.initial_mass_kg, "initial_mass_kg", 1e-3, 1e8)
        object.__setattr__(self, "initial_mass_kg", initial)
        object.__setattr__(self, "dry_mass_kg", number(self.dry_mass_kg, "dry_mass_kg", 1e-3, initial))
        object.__setattr__(self, "isp_s", number(self.isp_s, "isp_s", 1, 1e6))


@dataclass(frozen=True, slots=True)
class Integrator:
    relative_tolerance: float = 1e-11
    position_tolerance_km: float = 1e-8
    velocity_tolerance_km_s: float = 1e-11
    max_step_s: float = 120.0

    def __post_init__(self) -> None:
        for field, low, high in (
            ("relative_tolerance", 1e-13, 1e-6),
            ("position_tolerance_km", 1e-11, 1e-2),
            ("velocity_tolerance_km_s", 1e-14, 1e-5),
            ("max_step_s", 0.25, 120),
        ):
            object.__setattr__(self, field, number(getattr(self, field), field, low, high))


@dataclass(frozen=True, slots=True)
class Mission:
    initial_state: State
    duration_s: float
    sample_interval_s: float
    gravity: str = "point_mass"
    impulses: tuple[Impulse, ...] = ()
    spacecraft: Spacecraft | None = None
    integrator: Integrator = Integrator()

    def __post_init__(self) -> None:
        if not isinstance(self.initial_state, State):
            raise ValueError("initial_state must be a State")
        number(hypot(*self.initial_state.velocity_km_s), "initial speed (km/s)", 0, 1000)
        radius = hypot(*self.initial_state.position_km)
        if not EARTH_RADIUS_KM < radius <= 1e10:
            raise ValueError("initial position must be above the spherical Earth surface and within 1e10 km")
        if not isinstance(self.integrator, Integrator):
            raise ValueError("integrator must be an Integrator")
        if self.spacecraft is not None and not isinstance(self.spacecraft, Spacecraft):
            raise ValueError("spacecraft must be a Spacecraft")
        if self.gravity not in ("point_mass", "j2"):
            raise ValueError("gravity must be point_mass or j2")

        duration = number(self.duration_s, "duration_s", 1e-3, 2_592_000)
        interval = number(self.sample_interval_s, "sample_interval_s", 1e-3, 2_592_000)
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "sample_interval_s", interval)

        if not isinstance(self.impulses, (tuple, list)) or len(self.impulses) > 1000:
            raise ValueError("impulses must contain at most 1000 entries")
        impulses = tuple(self.impulses)
        if not all(isinstance(impulse, Impulse) for impulse in impulses):
            raise ValueError("impulses must contain Impulse values")
        object.__setattr__(self, "impulses", impulses)
        previous_time = -1.0
        for impulse in impulses:
            if impulse.time_s <= previous_time or impulse.time_s > duration:
                raise ValueError("impulse times must be strictly increasing and within the mission")
            previous_time = impulse.time_s
        if ceil(duration / interval) + 1 + 2 * len(impulses) > MAX_SAMPLES:
            raise ValueError(f"mission exceeds the {MAX_SAMPLES} telemetry row budget")
        if ceil(duration / self.integrator.max_step_s) > 50_000:
            raise ValueError("mission exceeds the integration step budget for the chosen max_step_s")
