import multiprocessing
import time
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import URL, Engine, create_engine, delete, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.database import create_database_engine
from src.models import (
    APIQuotaMap,
    APIFeature,
    FeatureQuotaExtra,
    FeatureQuotaMonthly,
    FeatureQuotaStatus,
    Organization,
    QuotaUsagePerRequest,
)
from src.quota import InsufficientQuota, release_reservation, reserve_quota


@pytest.fixture
def committed_quota() -> Iterator[tuple[Engine, int]]:
    """Separate processes need committed rows; delete only this fixture's data."""
    engine = create_database_engine()
    org_id = None
    config_id = None
    try:
        with engine.begin() as connection:
            config_id = connection.scalar(
                pg_insert(APIQuotaMap)
                .values(
                    feature=APIFeature.CONTAINER_TRACKING,
                    lease_duration_sec=30,
                    unit_cost=1,
                )
                .on_conflict_do_nothing(index_elements=["feature"])
                .returning(APIQuotaMap.id)
            )
            org_id = connection.scalar(
                insert(Organization)
                .values(name=f"contention-test-{uuid4()}")
                .returning(Organization.id)
            )
            now = datetime.now(timezone.utc)
            next_month = (now.replace(day=1) + timedelta(days=32)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            connection.execute(
                insert(FeatureQuotaMonthly).values(
                    org_id=org_id,
                    feature=APIFeature.CONTAINER_TRACKING,
                    total_allocated=25,
                    units_consumed=0,
                    units_remaining=25,
                    resets_on=next_month,
                )
            )
        assert org_id is not None
        yield engine, org_id
    finally:
        with engine.begin() as connection:
            if org_id is not None:
                connection.execute(
                    delete(QuotaUsagePerRequest).where(QuotaUsagePerRequest.org_id == org_id)
                )
                connection.execute(
                    delete(FeatureQuotaMonthly).where(FeatureQuotaMonthly.org_id == org_id)
                )
                connection.execute(
                    delete(FeatureQuotaExtra).where(FeatureQuotaExtra.org_id == org_id)
                )
                connection.execute(delete(Organization).where(Organization.id == org_id))
            if config_id is not None:
                connection.execute(delete(APIQuotaMap).where(APIQuotaMap.id == config_id))
        engine.dispose()


def _reserve_in_process(
    url: URL,
    application_name: str,
    org_id: int,
    units: int,
    key: UUID | None,
) -> tuple[list[int], int]:
    # Each spawned process creates its own engine/connections, never shares a Session.
    engine = create_engine(
        url,
        connect_args={
            "application_name": application_name,
            "connect_timeout": 5,
            "options": "-c statement_timeout=30000",
        },
    )
    operation_ids = []
    rejected = 0
    try:
        for _ in range(10):
            with Session(engine) as session:
                try:
                    operation = reserve_quota(
                        session,
                        org_id=org_id,
                        feature=APIFeature.CONTAINER_TRACKING,
                        units=units,
                        idempotency_key=key if key is not None else uuid4(),
                    )
                except InsufficientQuota:
                    rejected += 1
                else:
                    operation_ids.append(operation.id)
        return operation_ids, rejected
    finally:
        engine.dispose()


def _run_contending_calls(engine: Engine, row_to_lock, worker, *arguments) -> list:
    application_name = f"quota-contention-{uuid4()}"
    with ProcessPoolExecutor(
        max_workers=4, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        # Gate all four first attempts on an actual Postgres row lock. The test
        # fails if contention is not observed; simultaneous startup is insufficient.
        with engine.connect() as blocker:
            transaction = blocker.begin()
            try:
                blocker.execute(row_to_lock.with_for_update())
                futures = [
                    pool.submit(
                        worker, engine.url, application_name, *arguments
                    )
                    for _ in range(4)
                ]
                deadline = time.monotonic() + 15
                waiting = 0
                with engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as monitor:
                    while time.monotonic() < deadline:
                        waiting = monitor.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name = :name AND wait_event_type = 'Lock'"
                            ),
                            {"name": application_name},
                        )
                        if waiting == 4:
                            break
                        time.sleep(0.05)
                assert waiting == 4, f"Expected four contending processes, observed {waiting}"
            finally:
                # Always release the gate before waiting for worker termination.
                transaction.rollback()
        return [future.result(timeout=30) for future in futures]


def _run_contending_reservations(
    engine: Engine, org_id: int, *, units: int = 1, key: UUID | None = None
) -> list[tuple[list[int], int]]:
    return _run_contending_calls(
        engine,
        select(FeatureQuotaMonthly.id).where(FeatureQuotaMonthly.org_id == org_id),
        _reserve_in_process,
        org_id,
        units,
        key,
    )


def test_independent_processes_cannot_overspend(
    committed_quota: tuple[Engine, int],
) -> None:
    engine, org_id = committed_quota
    outcomes = _run_contending_reservations(engine, org_id)

    assert sum(len(operation_ids) for operation_ids, _ in outcomes) == 25
    assert sum(rejected for _, rejected in outcomes) == 15
    with Session(engine) as session:
        quota = session.scalar(
            select(FeatureQuotaMonthly).where(FeatureQuotaMonthly.org_id == org_id)
        )
        assert quota is not None
        assert quota.units_consumed == 25
        assert quota.units_remaining == 0
        operations = session.scalars(
            select(QuotaUsagePerRequest).where(QuotaUsagePerRequest.org_id == org_id)
        ).all()
        assert len(operations) == 25
        assert all(operation.status == FeatureQuotaStatus.RESERVED for operation in operations)
        assert sum(operation.used_from_monthly for operation in operations) == 25
        assert sum(operation.used_from_extra for operation in operations) == 0


@pytest.mark.parametrize("credit_funded", [False, True], ids=["monthly", "mixed"])
def test_concurrent_duplicates_reserve_only_once(
    committed_quota: tuple[Engine, int],
    credit_funded: bool,
) -> None:
    engine, org_id = committed_quota
    if credit_funded:
        with engine.begin() as connection:
            connection.execute(
                insert(FeatureQuotaExtra).values(
                    org_id=org_id,
                    feature=APIFeature.CONTAINER_TRACKING,
                    total_allocated=10,
                    units_consumed=0,
                    units_remaining=10,
                    expires_on=datetime.now(timezone.utc) + timedelta(days=90),
                )
            )
    outcomes = _run_contending_reservations(
        engine, org_id, units=35 if credit_funded else 25, key=uuid4()
    )

    operation_ids = [operation_id for ids, _ in outcomes for operation_id in ids]
    assert len(operation_ids) == 40
    assert len(set(operation_ids)) == 1
    assert sum(rejected for _, rejected in outcomes) == 0
    with Session(engine) as session:
        quota = session.scalar(
            select(FeatureQuotaMonthly).where(FeatureQuotaMonthly.org_id == org_id)
        )
        assert quota is not None
        assert quota.units_consumed == 25
        assert quota.units_remaining == 0
        if credit_funded:
            credits = session.scalar(
                select(FeatureQuotaExtra).where(FeatureQuotaExtra.org_id == org_id)
            )
            assert credits is not None
            assert credits.units_consumed == 10
            assert credits.units_remaining == 0
        operations = session.scalars(
            select(QuotaUsagePerRequest).where(QuotaUsagePerRequest.org_id == org_id)
        ).all()
        assert len(operations) == 1
        assert operations[0].id == operation_ids[0]
        assert operations[0].used_from_monthly == 25
        assert operations[0].used_from_extra == (10 if credit_funded else 0)


def _release_in_process(url: URL, application_name: str, operation_id: int) -> str:
    engine = create_engine(
        url,
        connect_args={
            "application_name": application_name,
            "connect_timeout": 5,
            "options": "-c statement_timeout=30000",
        },
    )
    try:
        with Session(engine) as session:
            operation = release_reservation(
                session, operation_id=operation_id, claim_version=0
            )
            return operation.status.value
    finally:
        engine.dispose()


@pytest.mark.parametrize("credit_funded", [False, True], ids=["monthly", "mixed"])
def test_concurrent_release_refunds_only_once(
    committed_quota: tuple[Engine, int],
    credit_funded: bool,
) -> None:
    engine, org_id = committed_quota
    if credit_funded:
        with engine.begin() as connection:
            connection.execute(
                insert(FeatureQuotaExtra).values(
                    org_id=org_id,
                    feature=APIFeature.CONTAINER_TRACKING,
                    total_allocated=10,
                    units_consumed=0,
                    units_remaining=10,
                    expires_on=datetime.now(timezone.utc) + timedelta(days=90),
                )
            )
    with Session(engine) as session:
        operation = reserve_quota(
            session,
            org_id=org_id,
            feature=APIFeature.CONTAINER_TRACKING,
            units=35 if credit_funded else 25,
            idempotency_key=uuid4(),
        )
    outcomes = _run_contending_calls(
        engine,
        select(QuotaUsagePerRequest.id).where(QuotaUsagePerRequest.id == operation.id),
        _release_in_process,
        operation.id,
    )
    assert outcomes == [FeatureQuotaStatus.RELEASED.value] * 4
    with Session(engine) as session:
        quota = session.scalar(
            select(FeatureQuotaMonthly).where(FeatureQuotaMonthly.org_id == org_id)
        )
        assert quota is not None
        assert quota.units_consumed == 0
        assert quota.units_remaining == 25
        if credit_funded:
            credits = session.scalar(
                select(FeatureQuotaExtra).where(FeatureQuotaExtra.org_id == org_id)
            )
            assert credits is not None
            assert credits.units_consumed == 0
            assert credits.units_remaining == 10
        assert session.get(QuotaUsagePerRequest, operation.id).status == FeatureQuotaStatus.RELEASED


def test_concurrent_batches_cannot_overspend_combined_capacity(
    committed_quota: tuple[Engine, int],
) -> None:
    engine, org_id = committed_quota
    with engine.begin() as connection:
        connection.execute(
            insert(FeatureQuotaExtra).values(
                org_id=org_id,
                feature=APIFeature.CONTAINER_TRACKING,
                total_allocated=10,
                units_consumed=0,
                units_remaining=10,
                expires_on=datetime.now(timezone.utc) + timedelta(days=90),
            )
        )
    # 25 included + 10 credits = five complete batches of seven, never partial work.
    outcomes = _run_contending_reservations(engine, org_id, units=7)
    assert sum(len(ids) for ids, _ in outcomes) == 5
    assert sum(rejected for _, rejected in outcomes) == 35
    with Session(engine) as session:
        monthly = session.scalar(
            select(FeatureQuotaMonthly).where(FeatureQuotaMonthly.org_id == org_id)
        )
        credits = session.scalar(
            select(FeatureQuotaExtra).where(FeatureQuotaExtra.org_id == org_id)
        )
        assert monthly is not None and credits is not None
        assert monthly.units_consumed == 25
        assert monthly.units_remaining == 0
        assert credits.units_consumed == 10
        assert credits.units_remaining == 0
        operations = session.scalars(
            select(QuotaUsagePerRequest).where(QuotaUsagePerRequest.org_id == org_id)
        ).all()
        assert len(operations) == 5
        assert sum(operation.used_from_monthly for operation in operations) == 25
        assert sum(operation.used_from_extra for operation in operations) == 10
        assert all(
            operation.used_from_monthly + operation.used_from_extra == 7
            for operation in operations
        )
