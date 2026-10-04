"""One-request diagnostic: run with uvicorn; no changes to accounting code."""

import cProfile
import os
import pstats
import sys
from contextvars import ContextVar
from functools import wraps
from time import perf_counter

os.environ["QUOTA_BENCHMARK_TIMING"] = "0"

from fastapi import Request
from sqlalchemy.orm import Session

from src.consumer import service
from src.consumer.main import app
from src.shared.database import get_session


records = ContextVar("simple_quota_timings", default=None)
commit_profiles = ContextVar("simple_commit_profiles", default=None)


def timed(label, function, *args, **kwargs):
    rows = records.get()
    if rows is None:
        return function(*args, **kwargs)
    start = perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        rows.append((label, (perf_counter() - start) * 1000))


class TimedSession(Session):
    def execute(self, statement, *args, **kwargs):
        return timed(
            f"Python + DB: execute {statement.__visit_name__}",
            super().execute, statement, *args, **kwargs,
        )

    def scalar(self, statement, *args, **kwargs):
        return timed(
            f"Python + DB: {statement.__visit_name__}",
            super().scalar, statement, *args, **kwargs,
        )

    def flush(self, *args, **kwargs):
        return timed("Python + DB: flush", super().flush, *args, **kwargs)


def simple_session(request: Request):
    engine = request.app.state.session_factory.kw["bind"]
    if not getattr(engine, "simple_timing_installed", False):
        dialect = engine.dialect
        execute, commit, ping = dialect.do_execute, dialect.do_commit, dialect.do_ping

        def execute_timed(cursor, statement, parameters, context=None):
            label = "DB call: " + " ".join(statement.split()[:3])
            return timed(label, execute, cursor, statement, parameters, context)

        def commit_timed(connection):
            profiles = commit_profiles.get()
            if profiles is None or os.environ.get("QUOTA_SIMPLE_COMMIT_TRACE") != "1":
                return timed("DB COMMIT", commit, connection)
            # Public libpq tracing + standard-library profiling; no library edits.
            raw = connection.driver_connection
            profiler = cProfile.Profile()
            print(f"COMMIT {len(profiles) + 1}: backend_pid={raw.info.backend_pid} protocol trace BEGIN", file=sys.stderr, flush=True)
            raw.pgconn.trace(sys.stderr.fileno())
            try:
                profiler.enable()
                return timed("DB COMMIT (profiled)", commit, connection)
            finally:
                profiler.disable()
                raw.pgconn.untrace()
                profiles.append(profiler)
                print(f"COMMIT {len(profiles)}: protocol trace END", file=sys.stderr, flush=True)

        dialect.do_execute = execute_timed
        dialect.do_commit = commit_timed
        dialect.do_ping = lambda connection: timed("DB connection ping", ping, connection)
        engine.simple_timing_installed = True
    if os.environ.get("QUOTA_SIMPLE_SERVER_TRACE") == "1":
        # Configure only this diagnostic connection, before admission is timed.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql("SET application_name = 'portcast-simple-commit'")
            connection.exec_driver_sql("SET log_min_duration_statement = 0")
            print(f"SERVER TRACE backend_pid={connection.connection.driver_connection.info.backend_pid}", flush=True)
            for setting in ("synchronous_commit", "fsync", "wal_sync_method", "commit_delay", "synchronous_standby_names"):
                print(f"SERVER SETTING {setting}={connection.exec_driver_sql('SHOW ' + setting).scalar()!r}", flush=True)
    with TimedSession(bind=engine) as session:
        yield session


app.dependency_overrides[get_session] = simple_session

original_reserve = service._reserve_quota_in_transaction
original_admit = service._admit_schedule_operation


@wraps(original_reserve)
def reserve_timed(*args, **kwargs):
    return timed("RESERVE BODY (excludes outer commit)", original_reserve, *args, **kwargs)


@wraps(original_admit)
def admit_timed(*args, **kwargs):
    rows = []
    profiles = []
    token = records.set(rows)
    profile_token = commit_profiles.set(profiles)
    try:
        return timed("ADMISSION TOTAL", original_admit, *args, **kwargs)
    finally:
        records.reset(token)
        commit_profiles.reset(profile_token)
        # Print only after admission, so console I/O is outside its measured time.
        print("SIMPLE TIMINGS — admission only; nested rows must not be added together", flush=True)
        for label, elapsed in rows:
            print(f"{elapsed:9.3f} ms | {label}", flush=True)
        for number, profiler in enumerate(profiles, 1):
            print(f"COMMIT {number}: function profile (seconds; cumulative rows overlap)", flush=True)
            pstats.Stats(profiler, stream=sys.stdout).sort_stats("cumulative").print_stats(20)


service._reserve_quota_in_transaction = reserve_timed
service._admit_schedule_operation = admit_timed
