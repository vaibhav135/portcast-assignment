import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, delete, event, inspect, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

import src.consumer.service as service
import src.shared.quota as quota_module
from src.consumer.schemas import ScheduleSearchRequest
from src.shared.config import get_database_config
from src.shared.models import (
    APIQuotaMap,
    APIFeature,
    FeatureQuotaExtra,
    FeatureQuotaMonthly,
    FeatureQuotaStatus,
    Organization,
    QuotaUsagePerRequest,
)
from src.shared.quota import IdempotencyConflict, InsufficientQuota, OperationConflict


@pytest.fixture(autouse=True)
def require_test_database() -> None:
    # Check before conftest creates a connection or dotenv supplies a demo DB name.
    if os.environ.get("DB_NAME") != "portcast_test":
        pytest.fail("Explicit DB_NAME=portcast_test is required for admission regressions")


@pytest.fixture
def sailing_quota(
    require_test_database: None,
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
) -> FeatureQuotaMonthly:
    with db_session.begin():
        feature = APIFeature.SAILING_SCHEDULE
        configuration = db_session.scalar(
            select(APIQuotaMap).where(APIQuotaMap.feature == feature)
        )
        if configuration is None:
            db_session.add(APIQuotaMap(feature=feature, unit_cost=1, lease_duration_sec=30))
        else:
            # This change is covered by conftest's outer rollback.
            configuration.unit_cost = 1
        quota = FeatureQuotaMonthly(
            org_id=monthly_quota.org_id,
            feature=feature,
            total_allocated=10,
            units_consumed=0,
            units_remaining=10,
            resets_on=monthly_quota.resets_on,
        )
        db_session.add(quota)
    return quota


def test_fresh_admission_commits_once_and_reads_combined_pricing_once(
    db_session: Session,
    sailing_quota: FeatureQuotaMonthly,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commits = []
    pricing_selects = []
    helper_calls = []
    original = service._reserve_quota_in_transaction
    connection = db_session.get_bind()

    def after_commit(session):
        commits.append(session)

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and "api_quota_map" in statement:
            assert db_session.in_transaction()
            pricing_selects.append(statement)

    def checked_helper(session, **kwargs):
        assert session is db_session
        assert session.in_transaction()
        operation = original(session, **kwargs)
        assert session.in_transaction()
        assert inspect(operation).detached
        assert commits == []
        helper_calls.append(operation.id)
        return operation

    monkeypatch.setattr(service, "_reserve_quota_in_transaction", checked_helper)
    event.listen(db_session, "after_commit", after_commit)
    event.listen(connection, "before_cursor_execute", before_cursor_execute)
    try:
        operation = service._admit_schedule_operation(
            db_session, org_id=sailing_quota.org_id, idempotency_key=uuid4(),
            request=ScheduleSearchRequest(routes=["route-a", "route-b"]),
        )
    finally:
        event.remove(db_session, "after_commit", after_commit)
        event.remove(connection, "before_cursor_execute", before_cursor_execute)

    assert commits == [db_session]
    assert helper_calls == [operation.id]
    assert len(pricing_selects) == 1
    projection = pricing_selects[0].split("FROM", 1)[0]
    assert "unit_cost" in projection and "lease_duration_sec" in projection
    assert not db_session.in_transaction()
    assert inspect(operation).detached
    assert operation.status == FeatureQuotaStatus.RESERVED
    assert (operation.used_from_monthly, operation.used_from_extra) == (2, 0)

    lookup_calls = []
    lookup = service.perform_schedule_lookup

    def checked_lookup(request):
        assert not db_session.in_transaction()
        lookup_calls.append(request)
        return lookup(request)

    monkeypatch.setattr(service, "perform_schedule_lookup", checked_lookup)
    response = service.execute_schedule_operation(db_session, operation)
    assert response.status == FeatureQuotaStatus.DONE
    assert len(lookup_calls) == 1
    assert not db_session.in_transaction()


def test_failure_after_reservation_rolls_back_entire_admission(
    db_session: Session,
    sailing_quota: FeatureQuotaMonthly,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = sailing_quota.org_id
    key = uuid4()
    original = service._reserve_quota_in_transaction

    class InjectedFailure(Exception):
        pass

    def reserve_then_fail(session, **kwargs):
        operation = original(session, **kwargs)
        assert session.in_transaction()
        assert inspect(operation).detached
        assert session.scalar(select(QuotaUsagePerRequest.id).where(
            QuotaUsagePerRequest.id == operation.id,
        )) == operation.id
        assert session.execute(select(
            FeatureQuotaMonthly.units_consumed, FeatureQuotaMonthly.units_remaining,
        ).where(FeatureQuotaMonthly.org_id == org_id,
                FeatureQuotaMonthly.feature == APIFeature.SAILING_SCHEDULE)).one() == (2, 8)
        raise InjectedFailure("before outer commit")

    monkeypatch.setattr(service, "_reserve_quota_in_transaction", reserve_then_fail)
    with pytest.raises(InjectedFailure, match="before outer commit"):
        service._admit_schedule_operation(
            db_session, org_id=org_id, idempotency_key=key,
            request=ScheduleSearchRequest(routes=["route-a", "route-b"]),
        )
    assert not db_session.in_transaction()
    with db_session.begin():
        assert db_session.scalar(select(QuotaUsagePerRequest.id).where(
            QuotaUsagePerRequest.org_id == org_id,
            QuotaUsagePerRequest.idempotency_key == key,
        )) is None
        assert db_session.execute(select(
            FeatureQuotaMonthly.units_consumed, FeatureQuotaMonthly.units_remaining,
        ).where(FeatureQuotaMonthly.org_id == org_id,
                FeatureQuotaMonthly.feature == APIFeature.SAILING_SCHEDULE)).one() == (0, 10)
    assert not db_session.in_transaction()


def test_transaction_helper_rejects_session_without_transaction(
    db_session: Session, sailing_quota: FeatureQuotaMonthly,
) -> None:
    assert not db_session.in_transaction()
    with pytest.raises(RuntimeError, match="transaction"):
        quota_module._reserve_quota_in_transaction(
            db_session, org_id=sailing_quota.org_id, feature=sailing_quota.feature,
            units=1, idempotency_key=uuid4(), request_payload=None,
            reject_pending_replay=True, lease_duration=30,
        )
    assert not db_session.in_transaction()


@pytest.fixture
def committed_sailing_quota(require_test_database: None) -> Iterator[tuple[Engine, int]]:
    config = get_database_config()
    assert config.name == "portcast_test"
    engine = create_engine(
        config.database_url,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=10000 -c lock_timeout=10000"},
    )
    org_id = config_id = None
    try:
        with Session(engine) as session, session.begin():
            config_id = session.scalar(insert(APIQuotaMap).values(
                feature=APIFeature.SAILING_SCHEDULE, unit_cost=1, lease_duration_sec=30,
            ).on_conflict_do_nothing(index_elements=["feature"]).returning(APIQuotaMap.id))
            assert session.scalar(select(APIQuotaMap.unit_cost).where(
                APIQuotaMap.feature == APIFeature.SAILING_SCHEDULE,
            )) == 1, "Race regressions require sailing unit_cost=1 in portcast_test"
            organization = Organization(name=f"admission-race-{uuid4()}")
            session.add(organization)
            session.flush()
            org_id = organization.id
            next_month = (datetime.now(timezone.utc).replace(day=1) + timedelta(days=32)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0,
            )
            session.add(FeatureQuotaMonthly(
                org_id=org_id, feature=APIFeature.SAILING_SCHEDULE,
                total_allocated=10, units_consumed=0, units_remaining=10,
                resets_on=next_month,
            ))
        yield engine, org_id
    finally:
        try:
            with engine.begin() as connection:
                if org_id is not None:
                    for model in (QuotaUsagePerRequest, FeatureQuotaExtra, FeatureQuotaMonthly):
                        connection.execute(delete(model).where(model.org_id == org_id))
                    connection.execute(delete(Organization).where(Organization.id == org_id))
                if config_id is not None:
                    connection.execute(delete(APIQuotaMap).where(APIQuotaMap.id == config_id))
        finally:
            engine.dispose()


@pytest.mark.parametrize("race, expected_error, units", [
    ("same-key-same-payload", OperationConflict, 2),
    ("same-key-different-payload", IdempotencyConflict, 2),
    ("different-keys", InsufficientQuota, 7),
])
def test_independent_session_admission_races(
    committed_sailing_quota: tuple[Engine, int],
    monkeypatch: pytest.MonkeyPatch,
    race: str,
    expected_error: type[Exception],
    units: int,
) -> None:
    engine, org_id = committed_sailing_quota
    barrier = Barrier(2)
    original = service._reserve_quota_in_transaction
    key = uuid4()

    def gated_helper(session, **kwargs):
        assert session.in_transaction()
        # Reaching this helper means the initial replay lookup missed. Neither
        # contender may insert until both independent transactions get here.
        barrier.wait(timeout=10)
        return original(session, **kwargs)

    monkeypatch.setattr(service, "_reserve_quota_in_transaction", gated_helper)

    def admit(contender):
        routes = [f"route-{index}" for index in range(units)]
        if race == "same-key-different-payload" and contender:
            routes[0] = "changed-route"
        request = ScheduleSearchRequest(routes=routes)
        with Session(engine) as session:
            try:
                operation = service._admit_schedule_operation(
                    session, org_id=org_id,
                    idempotency_key=uuid4() if race == "different-keys" else key,
                    request=request,
                )
            except (OperationConflict, IdempotencyConflict, InsufficientQuota) as error:
                assert not session.in_transaction()
                return error
            assert not session.in_transaction()
            assert inspect(operation).detached
            return operation

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, contender) for contender in range(2)]
        try:
            outcomes = [future.result(timeout=30) for future in futures]
        finally:
            barrier.abort()

    accepted = [outcome for outcome in outcomes if isinstance(outcome, QuotaUsagePerRequest)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, Exception)]
    assert len(accepted) == len(rejected) == 1
    assert type(rejected[0]) is expected_error
    assert accepted[0].status == FeatureQuotaStatus.RESERVED
    with Session(engine) as session:
        operations = session.scalars(select(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.org_id == org_id,
        )).all()
        assert len(operations) == 1
        assert operations[0].id == accepted[0].id
        assert operations[0].status == FeatureQuotaStatus.RESERVED
        assert (operations[0].used_from_monthly, operations[0].used_from_extra) == (units, 0)
        assert session.execute(select(
            FeatureQuotaMonthly.units_consumed, FeatureQuotaMonthly.units_remaining,
        ).where(FeatureQuotaMonthly.org_id == org_id)).one() == (units, 10 - units)
