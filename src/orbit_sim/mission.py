"""Validated input contracts and simulation limits."""

from dataclasses import dataclass
from math import ceil, hypot

EARTH_MU_KM3_S2 = 398600.4418
EARTH_RADIUS_KM = 6378.137
EARTH_J2 = 1.08262982131e-3
EARTH_ROTATION_RAD_S = 7.292115e-5
STANDARD_GRAVITY_KM_S2 = 0.00980665
MAX_SAMPLES = 100_000
MAX_THIRD_BODIES = 8
MAX_EPHEMERIS_POINTS = 1024

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
class EphemerisPoint:
    """An Earth-relative body state at elapsed mission time."""

    time_s: float
    position_km: Vector
    velocity_km_s: Vector

    def __post_init__(self) -> None:
        position = vector(self.position_km, "ephemeris position_km", 1e12)
        if hypot(*position) <= EARTH_RADIUS_KM:
            raise ValueError("third-body ephemeris points must be outside Earth")
        object.__setattr__(self, "time_s", number(self.time_s, "ephemeris time_s", 0, 2_592_000))
        object.__setattr__(self, "position_km", position)
        object.__setattr__(self, "velocity_km_s", vector(self.velocity_km_s, "ephemeris velocity_km_s", 1e3))


@dataclass(frozen=True, slots=True)
class ThirdBody:
    name: str
    mu_km3_s2: float
    ephemeris: tuple[EphemerisPoint, ...]
    minimum_separation_km: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 48 or not self.name.isprintable():
            raise ValueError("third-body name must contain 1 to 48 printable characters")
        name = self.name.strip()
        if not name:
            raise ValueError("third-body name cannot be blank")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "mu_km3_s2", number(self.mu_km3_s2, "mu_km3_s2", 1e-12, 1e14))
        minimum = number(self.minimum_separation_km, "minimum_separation_km", 1, 1e7)
        object.__setattr__(self, "minimum_separation_km", minimum)
        if not isinstance(self.ephemeris, (tuple, list)) or not 2 <= len(self.ephemeris) <= MAX_EPHEMERIS_POINTS:
            raise ValueError(f"third-body ephemeris must contain 2 to {MAX_EPHEMERIS_POINTS} points")
        points = tuple(self.ephemeris)
        previous = -1.0
        for point in points:
            if not isinstance(point, EphemerisPoint):
                raise TypeError("ephemeris must contain EphemerisPoint values")
            if point.time_s < previous + 0.001:
                raise ValueError("ephemeris points must be ordered and at least 0.001 s apart")
            previous = point.time_s
        object.__setattr__(self, "ephemeris", points)


@dataclass(frozen=True, slots=True)
class KeplerianOrbit:
    """Conic initial conditions using true anomaly and periapsis radius."""

    periapsis_radius_km: float
    eccentricity: float
    inclination_deg: float = 0.0
    raan_deg: float = 0.0
    argument_of_periapsis_deg: float = 0.0
    true_anomaly_deg: float = 0.0

    def __post_init__(self) -> None:
        for field, low, high in (
            ("periapsis_radius_km", 1e-3, 1e10),
            ("eccentricity", 0, 1e4),
            ("inclination_deg", 0, 180),
            ("raan_deg", -360, 360),
            ("argument_of_periapsis_deg", -360, 360),
            ("true_anomaly_deg", -360, 360),
        ):
            object.__setattr__(self, field, number(getattr(self, field), field, low, high))


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
class FiniteBurn:
    start_s: float
    end_s: float
    thrust_n: Vector
    frame: str = "ECI"

    def __post_init__(self) -> None:
        start = number(self.start_s, "start_s", 0, 2_592_000)
        end = number(self.end_s, "end_s", 0, 2_592_000)
        if end < start + 0.001:
            raise ValueError("finite burn must last at least 0.001 s")
        thrust = vector(self.thrust_n, "thrust_n", 1e7)
        number(hypot(*thrust), "thrust magnitude (N)", 1e-9, 1e7)
        object.__setattr__(self, "start_s", start)
        object.__setattr__(self, "end_s", end)
        object.__setattr__(self, "thrust_n", thrust)
        if self.frame not in ("ECI", "RTN"):
            raise ValueError("finite burn frame must be ECI or RTN")


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
class AtmosphericDrag:
    """Local exponential atmosphere and a constant effective drag area."""

    reference_altitude_km: float
    reference_density_kg_m3: float
    scale_height_km: float
    area_m2: float
    coefficient: float = 2.2

    def __post_init__(self) -> None:
        for field, low, high in (
            ("reference_altitude_km", 100, 2000),
            ("reference_density_kg_m3", 1e-20, 1e-4),
            ("scale_height_km", 1, 1000),
            ("area_m2", 1e-6, 1e4),
            ("coefficient", 1e-3, 10),
        ):
            object.__setattr__(self, field, number(getattr(self, field), field, low, high))


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
    finite_burns: tuple[FiniteBurn, ...] = ()
    drag: AtmosphericDrag | None = None
    track_apsides: bool = False
    third_bodies: tuple[ThirdBody, ...] = ()
    epoch_jd_tdb: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.initial_state, State):
            raise TypeError("initial_state must be a State")
        number(hypot(*self.initial_state.velocity_km_s), "initial speed (km/s)", 0, 1000)
        radius = hypot(*self.initial_state.position_km)
        if not EARTH_RADIUS_KM < radius <= 1e10:
            raise ValueError("initial position must be above the spherical Earth surface and within 1e10 km")
        if not isinstance(self.integrator, Integrator):
            raise TypeError("integrator must be an Integrator")
        if self.spacecraft is not None and not isinstance(self.spacecraft, Spacecraft):
            raise TypeError("spacecraft must be a Spacecraft")
        if self.drag is not None and not isinstance(self.drag, AtmosphericDrag):
            raise TypeError("drag must be an AtmosphericDrag")
        if self.drag is not None and self.spacecraft is None:
            raise ValueError("atmospheric drag requires spacecraft mass")
        if type(self.track_apsides) is not bool:
            raise TypeError("track_apsides must be a boolean")
        if self.gravity not in ("point_mass", "j2"):
            raise ValueError("gravity must be point_mass or j2")
        if self.epoch_jd_tdb is not None:
            epoch = number(self.epoch_jd_tdb, "epoch_jd_tdb", 1_000_000, 4_000_000)
            object.__setattr__(self, "epoch_jd_tdb", epoch)

        duration = number(self.duration_s, "duration_s", 1e-3, 2_592_000)
        interval = number(self.sample_interval_s, "sample_interval_s", 1e-3, 2_592_000)
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "sample_interval_s", interval)
        object.__setattr__(self, "third_bodies", self._validated_third_bodies(duration))

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
        burns = self._validated_finite_burns(duration, impulses)
        object.__setattr__(self, "finite_burns", burns)
        if ceil(duration / interval) + 1 + 2 * (len(impulses) + len(burns)) > MAX_SAMPLES:
            raise ValueError(f"mission exceeds the {MAX_SAMPLES} telemetry row budget")
        if ceil(duration / self.integrator.max_step_s) > 50_000:
            raise ValueError("mission exceeds the integration step budget for the chosen max_step_s")

    def _validated_third_bodies(self, duration: float) -> tuple[ThirdBody, ...]:
        if not isinstance(self.third_bodies, (tuple, list)) or len(self.third_bodies) > MAX_THIRD_BODIES:
            raise ValueError(f"mission must contain at most {MAX_THIRD_BODIES} third bodies")
        bodies = tuple(self.third_bodies)
        names = set()
        for body in bodies:
            if not isinstance(body, ThirdBody):
                raise TypeError("third_bodies must contain ThirdBody values")
            name = body.name.casefold()
            if name in names:
                raise ValueError("third-body names must be unique")
            names.add(name)
            if body.ephemeris[0].time_s != 0 or body.ephemeris[-1].time_s < duration:
                raise ValueError(f"ephemeris for {body.name} must cover the entire mission from t=0")
        return bodies

    def _validated_finite_burns(self, duration: float, impulses: tuple[Impulse, ...]) -> tuple[FiniteBurn, ...]:
        if not isinstance(self.finite_burns, (tuple, list)) or len(self.finite_burns) + len(impulses) > 1000:
            raise ValueError("mission must contain at most 1000 maneuvers")
        burns = tuple(self.finite_burns)
        if burns and self.spacecraft is None:
            raise ValueError("finite burns require spacecraft mass and specific impulse")
        previous_end = 0.0
        impulse_index = 0
        for burn in burns:
            if not isinstance(burn, FiniteBurn):
                raise TypeError("finite_burns must contain FiniteBurn values")
            if burn.start_s < previous_end or burn.end_s > duration:
                raise ValueError("finite burns must be ordered, nonoverlapping, and within the mission")
            while impulse_index < len(impulses) and impulses[impulse_index].time_s <= burn.start_s:
                impulse_index += 1
            if impulse_index < len(impulses) and impulses[impulse_index].time_s < burn.end_s:
                raise ValueError("an impulse cannot occur inside a finite burn")
            previous_end = burn.end_s
        return burns
