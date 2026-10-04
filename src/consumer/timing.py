"""Opt-in, request-scoped wall-clock timings for quota transactions."""

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from fastapi import HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError

_timings: ContextVar[list[tuple[str, float, str]] | None] = ContextVar(
    "quota_timings", default=None
)


@contextmanager
def request_timing(response: Response) -> Iterator[None]:
    """Scope the collector in the sync endpoint's context, including error headers."""
    enabled = os.environ.get("QUOTA_BENCHMARK_TIMING") == "1"
    timings = [] if enabled else None
    token = _timings.set(timings) if enabled else None
    try:
        yield
    except HTTPException as exc:
        if timings:
            exc.headers = {**(exc.headers or {}), "Server-Timing": _header(timings)}
        raise
    else:
        if timings:
            response.headers["Server-Timing"] = _header(timings)
    finally:
        if token is not None:
            _timings.reset(token)


def _header(timings: list[tuple[str, float, str]]) -> str:
    return ", ".join(
        f'{name};dur={duration:.3f};desc="{outcome}"'
        for name, duration, outcome in timings
    )


def time_quota[T](name: str, function: Callable[..., T], *args, **kwargs) -> T:
    """Measure the entire call, including pool/lock waits, commit, and exceptions.

    Descriptions identify call outcomes, not confirmed accounting state: even a
    database error can follow a successful commit with a lost acknowledgement.
    Disabled requests and recovery calls do not read the clock.
    """
    timings = _timings.get()
    if timings is None:
        return function(*args, **kwargs)
    start = perf_counter()
    outcome = "ok"
    try:
        return function(*args, **kwargs)
    except SQLAlchemyError:
        outcome = "db_error"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        timings.append((name, (perf_counter() - start) * 1000, outcome))
