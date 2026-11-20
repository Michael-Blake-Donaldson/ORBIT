"""Bounded UTF-8 input and exclusive publication for local artifacts."""

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
