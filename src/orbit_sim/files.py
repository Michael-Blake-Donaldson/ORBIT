"""Bounded UTF-8 input and exclusive publication for local artifacts."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TextIO


def read_text(path: Path, limit: int, description: str) -> str:
    if not path.is_file():
        raise ValueError(f"{description} must be a regular file")
    with path.open("rb") as file:
        raw = file.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{description} exceeds {limit // 1024} KiB")
    return raw.decode("utf-8")


def _unique_json_object(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON property {key[:80]!r}")
        result[key] = value
    return result


def _reject_constant(constant: str) -> None:
    raise ValueError(f"invalid JSON number {constant}")


def read_json_object(path: Path, limit: int, description: str) -> dict[str, object]:
    try:
        values = json.loads(
            read_text(path, limit, description),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_constant,
        )
    except RecursionError as exc:
        raise ValueError(f"{description} is nested too deeply") from exc
    if not isinstance(values, dict):
        raise ValueError(f"{description} must be a JSON object")  # noqa: TRY004 - JSON schema violation
    return values


@contextmanager
def atomic_text_output(destination: Path) -> Iterator[TextIO]:
    if os.path.lexists(destination):
        raise FileExistsError(f"output already exists: {destination}")
    with TemporaryDirectory(prefix=".orbit-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "artifact"
        with staged.open("x", encoding="utf-8", newline="") as file:
            yield file
            file.flush()
            os.fsync(file.fileno())
        # The link publishes the closed file and fails if another writer won the race.
        os.link(staged, destination)
