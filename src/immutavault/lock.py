from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import fcntl
from typing import Iterator


@contextmanager
def exclusive_lock(path: str) -> Iterator[None]:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
