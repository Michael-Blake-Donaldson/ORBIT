"""Read a bounded, versioned JSON mission plan."""

import json
from dataclasses import fields
from pathlib import Path
from typing import TypeVar

from .mechanics import state_from_keplerian
from .mission import (
    AtmosphericDrag,
    FiniteBurn,
    Impulse,
    Integrator,
    KeplerianOrbit,
    Mission,
    Spacecraft,
    State,
)

_MAX_PLAN_BYTES = 256 * 1024
_Config = TypeVar("_Config", AtmosphericDrag, FiniteBurn, Impulse, Integrator, KeplerianOrbit, Mission, Spacecraft, State)


def _unique_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate mission property {key[:80]!r}")
        result[key] = value
    return result


def _reject_constant(constant: str) -> None:
    raise ValueError(f"invalid JSON number {constant}")


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


def load_mission(path: str | Path) -> Mission:
    """Load a version 1 mission from a UTF-8 JSON file."""
    source = Path(path)
    if not source.is_file():
        raise ValueError("mission plan must be a regular file")
    with source.open("rb") as file:
        raw = file.read(_MAX_PLAN_BYTES + 1)
    if len(raw) > _MAX_PLAN_BYTES:
        raise ValueError("mission plan exceeds 256 KiB")
    try:
        values = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except RecursionError as exc:
        raise ValueError("mission plan is nested too deeply") from exc
    if not isinstance(values, dict):
        raise ValueError("mission plan must be a JSON object")  # noqa: TRY004 - JSON schema violation
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
    return _construct(Mission, values)
