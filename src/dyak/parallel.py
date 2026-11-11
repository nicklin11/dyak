"""Bounded, order-preserving fan-out for the stages' independent units (#349).

Everything slow in this tree is an external process or a blocking HTTP call, so threads are
enough: the GIL is released while a unit waits. Results come back in the order the units were
given, whatever order they finished in, so an artefact written from them is identical at any
worker count -- `workers=1` is the sequential run, not an approximation of it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent import futures
from typing import TypeVar

from . import exitcodes as ec
from . import runner
from .settings import FFMPEG_JOBS_ENV, resolve

T = TypeVar("T")
R = TypeVar("R")

#: ffmpeg processes stages 4 and 5 may run at once when `DYAK_FFMPEG_JOBS` is unset. Each unit
#: is one short seek-and-decode of one frame, so a handful saturate a desktop CPU without
#: starving the STT branch that runs beside them.
DEFAULT_FFMPEG_JOBS = 4


def positive_count(raw: object, env_name: str, what: str, default: int) -> int:
    """Parse a worker-count knob, refusing anything that is not a positive whole number.

    Refused, not clamped: `0` is what someone writes to mean "sequential", and a clamp would
    make it look like it worked. Exit 1 names the variable.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = 0
    if value < 1:
        raise runner.DependencyError(
            name="settings",
            code=ec.USAGE,
            message=f"{env_name} is not a positive count: {raw!r}",
            remediation=f"set it to the number of {what} at once (default {default}; 1 = one at a time)",
        )
    return value


def ffmpeg_jobs() -> int:
    """How many per-frame ffmpeg processes stages 4 and 5 may run at once."""
    raw = resolve(FFMPEG_JOBS_ENV, default=str(DEFAULT_FFMPEG_JOBS)).value
    return positive_count(raw, FFMPEG_JOBS_ENV, "ffmpeg processes to run", DEFAULT_FFMPEG_JOBS)


def map_ordered(fn: Callable[[T], R], items: Iterable[T], workers: int) -> list[R]:
    """`[fn(x) for x in items]` with at most `workers` calls in flight.

    The first failure *in input order* is raised, after every started call has finished --
    the same exception the sequential loop would have raised, so a caller's error handling
    does not depend on the worker count.
    """
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        return [fn(item) for item in items]
    with futures.ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        pending = [pool.submit(fn, item) for item in items]
        futures.wait(pending)
    return [future.result() for future in pending]
