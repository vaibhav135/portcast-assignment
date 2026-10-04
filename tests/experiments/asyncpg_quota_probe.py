"""Manual experiment; fresh monthly-only fixtures, not a pytest test or replacement API."""

import argparse
import asyncio
import json
from datetime import timedelta
from time import perf_counter
from uuid import uuid4

import asyncpg
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.consumer.schemas import ScheduleSearchRequest
from src.consumer.service import _admit_schedule_operation
from src.shared.config import get_database_config
from src.shared.database import create_database_engine
from src.shared.quota import finalize_reservation


ROUTE = "Singapore-Rotterdam"
SQL = {
    "lookup": "SELECT * FROM quota_usage_per_request WHERE org_id=$1 "
              "AND feature='sailing-schedule' AND idempotency_key=$2",
    "config": "SELECT unit_cost, lease_duration_sec FROM api_quota_map WHERE feature='sailing-schedule'",
    "claim": "INSERT INTO quota_usage_per_request "
             "(org_id,feature,status,used_from_monthly,used_from_extra,idempotency_key,"
             "request_payload,reserved_at,lease_expires_at) "
             "VALUES ($1,'sailing-schedule','RESERVED',$2,0,$3,$4::jsonb,"
             "clock_timestamp(),clock_timestamp()+$5::interval) "
             "ON CONFLICT ON CONSTRAINT uq_operation_key DO NOTHING RETURNING *",
    "monthly": "SELECT * FROM feature_quota_monthly WHERE org_id=$1 "
               "AND feature='sailing-schedule' FOR UPDATE",
    "clock": "SELECT clock_timestamp()",
    "stamp": "UPDATE quota_usage_per_request SET reserved_at=$1,lease_expires_at=$2 WHERE id=$3",
    "debit": "UPDATE feature_quota_monthly SET units_consumed=units_consumed+$1,"
             "units_remaining=units_remaining-$1 WHERE org_id=$2 AND feature='sailing-schedule' "
             "AND units_remaining >= $1 RETURNING id",
}

# Session-local diagnostic only. Same seven operations, now executed server-side.
FUNCTION_SQL = """
CREATE FUNCTION pg_temp.quota_admit_probe(p_org bigint, p_key uuid, p_payload jsonb)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE
    existing_record record;
    configuration record;
    monthly record;
    operation_id bigint;
    acquired bigint;
    admitted_at timestamptz;
    changed bigint;
BEGIN
    SELECT * INTO existing_record FROM public.quota_usage_per_request
      WHERE org_id=p_org AND feature='sailing-schedule' AND idempotency_key=p_key;
    IF FOUND THEN RAISE EXCEPTION 'Probe requires a fresh key'; END IF;

    SELECT unit_cost, lease_duration_sec INTO configuration FROM public.api_quota_map
      WHERE feature='sailing-schedule';
    IF NOT FOUND THEN RAISE EXCEPTION 'Missing feature configuration'; END IF;
    IF jsonb_array_length(p_payload->'routes') <> 1 THEN
      RAISE EXCEPTION 'Probe requires exactly one route';
    END IF;

    INSERT INTO public.quota_usage_per_request
      (org_id,feature,status,used_from_monthly,used_from_extra,idempotency_key,
       request_payload,reserved_at,lease_expires_at)
      VALUES (p_org,'sailing-schedule','RESERVED',configuration.unit_cost,0,p_key,
        p_payload,clock_timestamp(),clock_timestamp()+make_interval(secs=>configuration.lease_duration_sec))
      ON CONFLICT ON CONSTRAINT uq_operation_key DO NOTHING RETURNING id INTO operation_id;
    IF NOT FOUND THEN RAISE EXCEPTION 'Unexpected key conflict'; END IF;

    SELECT * INTO monthly FROM public.feature_quota_monthly
      WHERE org_id=p_org AND feature='sailing-schedule' FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Missing monthly quota'; END IF;
    SELECT clock_timestamp() INTO admitted_at;
    IF admitted_at >= monthly.resets_on THEN RAISE EXCEPTION 'Current-period fixture required'; END IF;
    IF monthly.units_remaining < configuration.unit_cost THEN
      RAISE EXCEPTION 'Monthly-only capacity required';
    END IF;

    UPDATE public.quota_usage_per_request SET reserved_at=admitted_at,
      lease_expires_at=admitted_at+make_interval(secs=>configuration.lease_duration_sec)
      WHERE id=operation_id;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 1 THEN RAISE EXCEPTION 'Reservation timestamp update failed'; END IF;

    UPDATE public.feature_quota_monthly SET units_consumed=units_consumed+configuration.unit_cost,
      units_remaining=units_remaining-configuration.unit_cost
      WHERE org_id=p_org AND feature='sailing-schedule'
        AND units_remaining >= configuration.unit_cost RETURNING id INTO acquired;
    IF NOT FOUND THEN RAISE EXCEPTION 'Locked balance changed unexpectedly'; END IF;
    RETURN operation_id;
END;
$$;
"""


class PreparedConnection:
    """Run the exact same transaction function through prepared statement handles."""
    def __init__(self, connection):
        self.connection = connection
        self.statements = {}

    async def prepare(self):
        for sql in SQL.values():
            self.statements[sql] = await self.connection.prepare(sql)

    def transaction(self):
        return self.connection.transaction()

    async def fetchrow(self, sql, *args):
        return await self.statements[sql].fetchrow(*args)

    async def fetchval(self, sql, *args):
        return await self.statements[sql].fetchval(*args)

    async def execute(self, sql, *args):
        statement = self.statements[sql]
        await statement.fetch(*args)
        return statement.get_statusmsg()


async def admit_asyncpg(connection, org_id, key):
    """Same seven statements + commit for a fresh, current-month included unit."""
    payload = ScheduleSearchRequest(routes=[ROUTE]).model_dump(mode="json")
    async with connection.transaction():
        existing = await connection.fetchrow(
            SQL["lookup"], org_id, key,
        )
        assert existing is None, "Probe requires a fresh key"
        config = await connection.fetchrow(
            SQL["config"]
        )
        assert config is not None
        units, lease = config["unit_cost"], timedelta(seconds=config["lease_duration_sec"])
        operation = await connection.fetchrow(
            SQL["claim"],
            org_id, units, key, json.dumps(payload), lease,
        )
        assert operation is not None, "Unexpected key conflict in fresh-key probe"
        monthly = await connection.fetchrow(
            SQL["monthly"], org_id,
        )
        now = await connection.fetchval(SQL["clock"])
        assert monthly is not None and now < monthly["resets_on"], "Current-period fixture required"
        assert monthly["units_remaining"] >= units, "Monthly-only capacity required"
        changed = await connection.execute(
            SQL["stamp"],
            now, now + lease, operation["id"],
        )
        assert changed == "UPDATE 1"
        acquired = await connection.fetchval(
            SQL["debit"], units, org_id,
        )
        assert acquired is not None
    return operation["id"]


async def measure_async_orm(connection, org_id, key):
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(bind=connection) as session:
        start = perf_counter()
        operation = await session.run_sync(lambda sync_session: _admit_schedule_operation(
            sync_session, org_id=org_id, idempotency_key=key,
            request=ScheduleSearchRequest(routes=[ROUTE]),
        ))
        return operation.id, (perf_counter() - start) * 1000


async def admit_function(connection, org_id, key):
    payload = ScheduleSearchRequest(routes=[ROUTE]).model_dump(mode="json")
    async with connection.transaction():
        operation_id = await connection.fetchval(
            "SELECT pg_temp.quota_admit_probe($1,$2,$3::jsonb)", org_id, key, json.dumps(payload),
        )
        assert operation_id is not None
    return operation_id


async def main(org_id, prepared_comparison=False, async_orm_comparison=False, function_comparison=False):
    config = get_database_config()
    if config.name != "portcast_load":
        raise ValueError("Requires isolated DB_NAME=portcast_load")
    engine = create_database_engine()
    connection = await asyncpg.connect(
        host=config.host, port=config.port, database=config.name,
        user=config.username, password=config.password.get_secret_value(),
    )
    async_engine = async_orm_connection = None
    try:
        # Both connections stay open. Establishment/checkout is outside the timer.
        with engine.connect() as orm_connection:
            name = orm_connection.scalar(text("SELECT name FROM org WHERE id=:id"), {"id": org_id})
            if not name or not name.startswith("quota-load-"):
                raise ValueError("Probe requires a benchmark-owned organization")
            print(json.dumps({"asyncpg_version": asyncpg.__version__, "sql_statements": 7,
                              "commit_included": True, "checkout_timed": False,
                              "orm_log_min_duration_statement": orm_connection.scalar(text("SHOW log_min_duration_statement")),
                              "asyncpg_log_min_duration_statement": await connection.fetchval("SHOW log_min_duration_statement")}), flush=True)
            orm_connection.rollback()
            if function_comparison:
                durations = {"asyncpg": [], "postgres_function": []}
            elif async_orm_comparison:
                from sqlalchemy.ext.asyncio import create_async_engine

                async_engine = create_async_engine(config.database_url.set(drivername="postgresql+asyncpg"))
                async_orm_connection = await async_engine.connect()
                logging = await async_orm_connection.scalar(text("SHOW log_min_duration_statement"))
                await async_orm_connection.rollback()
                print(json.dumps({"async_orm_log_min_duration_statement": logging,
                                  "async_orm_method": "AsyncSession.run_sync(existing admission)"}), flush=True)
                durations = {"asyncpg": [], "sqlalchemy_async_orm": []}
            else:
                durations = ({"asyncpg": [], "asyncpg_prepared": []} if prepared_comparison
                             else {"orm": [], "asyncpg": []})
            for implementation in durations:
                target = connection
                if implementation == "postgres_function":
                    start = perf_counter()
                    await connection.execute(FUNCTION_SQL)
                    print(json.dumps({"function_create_ms": round((perf_counter() - start) * 1000, 3),
                                      "function_scope": "pg_temp: removed at disconnect",
                                      "client_admission_calls": 1, "server_operations": 7}), flush=True)
                elif implementation == "asyncpg_prepared":
                    target = PreparedConnection(connection)
                    start = perf_counter()
                    await target.prepare()
                    print(json.dumps({"prepare_ms": round((perf_counter() - start) * 1000, 3),
                                      "prepared_statements": len(target.statements)}), flush=True)
                for index in range(3):
                    key = uuid4()
                    with Session(bind=orm_connection) as session:
                        start = perf_counter()
                        if implementation == "orm":
                            operation = _admit_schedule_operation(
                                session, org_id=org_id, idempotency_key=key,
                                request=ScheduleSearchRequest(routes=[ROUTE]),
                            )
                            operation_id = operation.id
                        elif implementation == "sqlalchemy_async_orm":
                            operation_id, elapsed = await measure_async_orm(async_orm_connection, org_id, key)
                        elif implementation == "postgres_function":
                            operation_id = await admit_function(connection, org_id, key)
                        else:
                            operation_id = await admit_asyncpg(target, org_id, key)
                        if implementation != "sqlalchemy_async_orm":
                            elapsed = (perf_counter() - start) * 1000
                        durations[implementation].append(elapsed)
                        # Settle ONLY fixture work outside admission timing; no abandoned holds.
                        finalize_reservation(
                            session, operation_id=operation_id, claim_version=0,
                            result_payload={"results": [{"route": ROUTE, "sailings": ["demo-sailing-001"]}]},
                        )
                    print(json.dumps({"implementation": implementation, "call": index + 1,
                                      "admission_ms": round(elapsed, 3), "operation_id": operation_id}), flush=True)
            print(json.dumps({"warm_mean_ms": {name: round(sum(values[1:]) / 2, 3)
                                               for name, values in durations.items()}}), flush=True)
    finally:
        if async_orm_connection is not None:
            await async_orm_connection.close()
        if async_engine is not None:
            await async_engine.dispose()
        await connection.close()
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", type=int, required=True)
    parser.add_argument("--allow-demo-writes", action="store_true")
    comparison = parser.add_mutually_exclusive_group()
    comparison.add_argument("--prepared-comparison", action="store_true",
                            help="Compare automatic asyncpg caching with explicit preparation only")
    comparison.add_argument("--async-orm-comparison", action="store_true",
                            help="Compare direct asyncpg with existing SQLAlchemy ORM through run_sync")
    comparison.add_argument("--function-comparison", action="store_true",
                            help="Compare seven client statements with a session-local PostgreSQL function")
    args = parser.parse_args()
    if not args.allow_demo_writes:
        parser.error("Requires --allow-demo-writes and a benchmark-owned synthetic organization")
    asyncio.run(main(args.org_id, args.prepared_comparison, args.async_orm_comparison, args.function_comparison))
