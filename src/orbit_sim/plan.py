"""Read a bounded, versioned JSON mission plan."""

from dataclasses import fields
from pathlib import Path
from typing import TypeVar

from .files import read_json_object
from .mechanics import state_from_keplerian
from .mission import (
    MAX_EPHEMERIS_POINTS,
    MAX_THIRD_BODIES,
    AtmosphericDrag,
    EphemerisPoint,
    FiniteBurn,
    Impulse,
    Integrator,
    KeplerianOrbit,
    LoadChange,
    Mission,
    PowerSystem,
    Spacecraft,
    State,
    ThirdBody,
)

_MAX_PLAN_BYTES = 256 * 1024
_Config = TypeVar(
    "_Config",
    AtmosphericDrag, EphemerisPoint, FiniteBurn, Impulse, Integrator, KeplerianOrbit, LoadChange, Mission, PowerSystem,
    Spacecraft, State,
    ThirdBody,
)


def _construct(cls: type[_Config], value: object) -> _Config:
    if not isinstance(value, dict):
        raise ValueError(f"{cls.__name__} must be an object")  # noqa: TRY004 - JSON schema violation
    unknown = value.keys() - {field.name for field in fields(cls)}
    if unknown:
        raise ValueError(f"unknown {cls.__name__} property {min(unknown)[:80]!r}")
    try:
        return cls(**value)
    except TypeError as exc:
        raise ValueError(f"invalid {cls.__name__} fields: {exc}") from exc


def _load_third_bodies(value: object) -> tuple[ThirdBody, ...]:
    if not isinstance(value, list) or len(value) > MAX_THIRD_BODIES:
        raise ValueError(f"third_bodies must be an array with at most {MAX_THIRD_BODIES} entries")
    bodies = []
    for body in value:
        if not isinstance(body, dict):
            raise ValueError("ThirdBody must be an object")  # noqa: TRY004 - JSON schema violation
        entries = body.get("ephemeris")
        if not isinstance(entries, list) or not 2 <= len(entries) <= MAX_EPHEMERIS_POINTS:
            raise ValueError(f"ephemeris must be an array with 2 to {MAX_EPHEMERIS_POINTS} entries")
        points = tuple(_construct(EphemerisPoint, entry) for entry in entries)
        bodies.append(_construct(ThirdBody, body | {"ephemeris": points}))
    return tuple(bodies)


def _load_power_system(value: object) -> PowerSystem:
    if not isinstance(value, dict) or "load_changes" not in value:
        return _construct(PowerSystem, value)
    changes = value["load_changes"]
    if not isinstance(changes, list) or len(changes) > 1000:
        raise ValueError("load_changes must be an array with at most 1000 entries")
    return _construct(PowerSystem, value | {"load_changes": tuple(_construct(LoadChange, item) for item in changes)})


def load_mission(path: str | Path) -> Mission:
    """Load a version 1 mission from a UTF-8 JSON file."""
    return _parse_mission(_read_plan_values(Path(path)))


def _read_plan_values(path: Path) -> dict[str, object]:
    return read_json_object(path, _MAX_PLAN_BYTES, "mission plan")


def _parse_mission(values: dict[str, object]) -> Mission:
    values = values.copy()
    version = values.pop("schema_version", None)
    if type(version) is not int or version != 1:
        raise ValueError("schema_version must be the integer 1")
    if ("initial_state" in values) == ("initial_orbit" in values):
        raise ValueError("provide exactly one initial_state or initial_orbit")
    if "initial_orbit" in values:
        values["initial_state"] = state_from_keplerian(_construct(KeplerianOrbit, values.pop("initial_orbit")))
    else:
        values["initial_state"] = _construct(State, values["initial_state"])
    if "impulses" in values:
        impulses = values["impulses"]
        if not isinstance(impulses, list) or len(impulses) > 1000:
            raise ValueError("impulses must be an array with at most 1000 entries")
        values["impulses"] = tuple(_construct(Impulse, impulse) for impulse in impulses)
    if "finite_burns" in values:
        burns = values["finite_burns"]
        if not isinstance(burns, list) or len(burns) > 1000:
            raise ValueError("finite_burns must be an array with at most 1000 entries")
        values["finite_burns"] = tuple(_construct(FiniteBurn, burn) for burn in burns)
    if "spacecraft" in values:
        values["spacecraft"] = _construct(Spacecraft, values["spacecraft"])
    if "integrator" in values:
        values["integrator"] = _construct(Integrator, values["integrator"])
    if "drag" in values:
        values["drag"] = _construct(AtmosphericDrag, values["drag"])
    if "third_bodies" in values:
        values["third_bodies"] = _load_third_bodies(values["third_bodies"])
    if "power_system" in values:
        values["power_system"] = _load_power_system(values["power_system"])
    return _construct(Mission, values)
