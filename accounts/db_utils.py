from __future__ import annotations

import time
from typing import Callable, TypeVar

from django.db.utils import OperationalError

T = TypeVar("T")


def run_with_retry(func: Callable[[], T], *, max_attempts: int = 5, base_delay: float = 0.05) -> T:
    """
    Run a DB write and retry it on SQLite's "database is locked" error.

    SQLite only allows one writer at a time. Two requests that both try to
    write at nearly the same instant (e.g. a double-submitted form) can hit
    this even with a busy_timeout configured, so retry the write itself a
    few times with a short backoff rather than failing the whole request.
    """
    attempt = 0
    while True:
        try:
            return func()
        except OperationalError as exc:
            attempt += 1
            if "database is locked" not in str(exc).lower() or attempt >= max_attempts:
                raise
            time.sleep(base_delay * attempt)
