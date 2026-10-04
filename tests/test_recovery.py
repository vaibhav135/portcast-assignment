import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select, update
from sqlalchemy.orm import Session

import src.consumer.recovery_worker as recovery_module
import src.consumer.service as consumer_module
from src.consumer.recovery_worker import recover_pending_operations
from src.consumer.schemas import ScheduleSearchRequest
from src.shared.models import (
    APIQuotaMap,
    APIFeature,
    FeatureQuotaExtra,
    FeatureQuotaMonthly,
    FeatureQuotaStatus,
    QuotaUsagePerRequest,
)
from src.shared.quota import (
    OperationConflict,
    claim_expired_reservations,
    finalize_reservation,
    get_quota_usage,
    release_reservation,
    reserve_quota,
)


@pytest.fixture
def schedule_arguments(db_session: Session, monthly_quota: FeatureQuotaMonthly) -> dict:
    org_id, resets_on = monthly_quota.org_id, monthly_quota.resets_on
    feature = APIFeature.SAILING_SCHEDULE
    with db_session.begin():
        if db_session.scalar(select(APIQuotaMap).where(APIQuotaMap.feature == feature)) is None:
            db_session.add(APIQuotaMap(feature=feature, unit_cost=1, lease_duration_sec=30))
        db_session.add(FeatureQuotaMonthly(
            org_id=org_id, feature=feature, total_allocated=10,
            units_consumed=0, units_remaining=10, resets_on=resets_on,
        ))
    return dict(org_id=org_id, feature=feature)


def _abandon(session: Session, arguments: dict, payload: dict | None) -> QuotaUsagePerRequest:
    operation = reserve_quota(
        session, **arguments, units=1, idempotency_key=uuid4(), request_payload=payload,
    )
    with session.begin():
        session.execute(update(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.id == operation.id,
        ).values(lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    return operation


def test_abandoned_persisted_requests_recover_in_bounded_batches(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    schedule_arguments: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracking_arguments = dict(org_id=monthly_quota.org_id, feature=monthly_quota.feature)
    abandoned = [
        _abandon(db_session, schedule_arguments, ScheduleSearchRequest(
            routes=[route],
        ).model_dump(mode="json"))
        for route in ["SGSIN-NLRTM", "SGSIN-GBFXT", "SGSIN-USLAX"]
    ]
    active = reserve_quota(
        db_session, **schedule_arguments, units=1, idempotency_key=uuid4(),
        request_payload=ScheduleSearchRequest(routes=["active-route"]).model_dump(mode="json"),
    )
    with db_session.begin():
        db_session.execute(update(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.id == active.id,
        ).values(lease_expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    unsupported = _abandon(db_session, tracking_arguments, None)
    seen = []
    executed = []
    lookup = consumer_module.perform_schedule_lookup
    execute = consumer_module.execute_schedule_operation

    def observed_execution(session, operation):
        assert session is db_session
        assert not session.in_transaction(), "Execution must run after the claim commits"
        assert inspect(operation).detached
        assert operation.feature == APIFeature.SAILING_SCHEDULE
        executed.append(operation.id)
        return execute(session, operation)

    def observed_lookup(request):
        assert not db_session.in_transaction(), "Lookup must run after the claim commits"
        assert isinstance(request, ScheduleSearchRequest)
        seen.append(request.model_dump(mode="json"))
        return lookup(request)

    monkeypatch.setattr(consumer_module, "perform_schedule_lookup", observed_lookup)
    monkeypatch.setattr(consumer_module, "execute_schedule_operation", observed_execution)
    # Support either a direct helper import or a service-module reference.
    if hasattr(recovery_module, "execute_schedule_operation"):
        monkeypatch.setattr(recovery_module, "execute_schedule_operation", observed_execution)
    assert recover_pending_operations(db_session, limit=2) == 2
    assert len(seen) == 2
    with db_session.begin():
        rows = db_session.execute(select(
            QuotaUsagePerRequest.status, QuotaUsagePerRequest.claim_version,
        ).where(QuotaUsagePerRequest.id.in_([operation.id for operation in abandoned]))).all()
        assert sum(row.status == FeatureQuotaStatus.DONE for row in rows) == 2
        assert sum(row.claim_version == 1 for row in rows) == 2
    assert recover_pending_operations(db_session, limit=2) == 1
    assert recover_pending_operations(db_session, limit=2) == 0
    assert sorted(executed) == sorted(operation.id for operation in abandoned)
    assert sorted(payload["routes"] for payload in seen) == sorted(
        operation.request_payload["routes"] for operation in abandoned
    )
    with db_session.begin():
        for original in abandoned:
            row = db_session.get(QuotaUsagePerRequest, original.id)
            assert row.status == FeatureQuotaStatus.DONE
            assert row.claim_version == original.claim_version + 1
            assert row.result_payload == lookup(ScheduleSearchRequest.model_validate(original.request_payload))
        for original in [active, unsupported]:
            row = db_session.get(QuotaUsagePerRequest, original.id)
            assert row.status == FeatureQuotaStatus.RESERVED
            assert row.claim_version == original.claim_version
            assert row.result_payload is None
    usage = get_quota_usage(db_session, **schedule_arguments)
    assert (usage.monthly.used, usage.monthly.reserved, usage.monthly.available) == (3, 1, 6)


@pytest.mark.parametrize("behavior", ["fail", "timeout"])
def test_confirmed_recovery_failure_refunds_once(
    db_session: Session, schedule_arguments: dict, behavior: str,
) -> None:
    payload = ScheduleSearchRequest(routes=["SGSIN-NLRTM"], demo_behavior=behavior).model_dump(mode="json")
    original = _abandon(db_session, schedule_arguments, payload)
    assert recover_pending_operations(db_session) == 1
    assert recover_pending_operations(db_session) == 0
    with db_session.begin():
        row = db_session.get(QuotaUsagePerRequest, original.id)
        assert row.status == FeatureQuotaStatus.RELEASED
        assert row.claim_version == original.claim_version + 1
        assert row.request_payload == payload
        assert row.result_payload is None
    usage = get_quota_usage(db_session, **schedule_arguments)
    assert (usage.monthly.used, usage.monthly.reserved, usage.monthly.available) == (0, 0, 10)


def test_invalid_context_and_unknown_lookup_error_remain_reserved_and_logged(
    db_session: Session,
    schedule_arguments: dict,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    invalid = _abandon(db_session, schedule_arguments, {"routes": []})
    payload = ScheduleSearchRequest(routes=["SGSIN-NLRTM"]).model_dump(mode="json")
    uncertain = _abandon(db_session, schedule_arguments, payload)
    seen = []

    def unknown_error(request):
        assert not db_session.in_transaction()
        seen.append(request.model_dump(mode="json"))
        raise RuntimeError("Unexpected lookup error; outcome is unknown")

    monkeypatch.setattr(consumer_module, "perform_schedule_lookup", unknown_error)
    with caplog.at_level(logging.WARNING):
        assert recover_pending_operations(db_session, limit=2) == 0
    assert seen == [payload]
    assert len([record for record in caplog.records if record.levelno >= logging.WARNING]) >= 2
    with db_session.begin():
        for original in [invalid, uncertain]:
            row = db_session.get(QuotaUsagePerRequest, original.id)
            assert row.status == FeatureQuotaStatus.RESERVED
            assert row.request_payload == original.request_payload
            assert row.result_payload is None
    usage = get_quota_usage(db_session, **schedule_arguments)
    assert (usage.monthly.used, usage.monthly.reserved, usage.monthly.available) == (0, 2, 8)


def test_claim_preserves_original_allocation_and_fences_stale_settlement(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
) -> None:
    arguments = dict(org_id=monthly_quota.org_id, feature=monthly_quota.feature)
    monthly_id, extra_id = monthly_quota.id, extra_quota.id
    original = reserve_quota(db_session, **arguments, units=12, idempotency_key=uuid4())
    replay_arguments = dict(arguments, units=12, idempotency_key=original.idempotency_key)
    assert reserve_quota(db_session, **replay_arguments).id == original.id
    with pytest.raises(OperationConflict):
        reserve_quota(db_session, **replay_arguments, reject_pending_replay=True)
    assert not db_session.in_transaction()
    active = reserve_quota(db_session, **arguments, units=1, idempotency_key=uuid4())
    with db_session.begin():
        db_session.execute(update(APIQuotaMap).where(
            APIQuotaMap.feature == arguments["feature"],
        ).values(lease_duration_sec=73))
        db_session.execute(update(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.id == original.id,
        ).values(lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        db_session.execute(update(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.id == active.id,
        ).values(lease_expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    assert claim_expired_reservations(
        db_session, feature=arguments["feature"], operation_id=active.id,
    ) == []
    with db_session.begin():
        before = db_session.scalar(select(func.clock_timestamp()))
    claimed = claim_expired_reservations(
        db_session, feature=arguments["feature"], limit=1, operation_id=original.id,
    )
    assert not db_session.in_transaction()
    with db_session.begin():
        after = db_session.scalar(select(func.clock_timestamp()))
    assert len(claimed) == 1
    current = claimed[0]
    assert inspect(current).detached
    assert current.id == original.id
    assert current.claim_version == original.claim_version + 1
    assert current.reserved_at == original.reserved_at
    assert (current.used_from_monthly, current.used_from_extra) == (10, 2)
    assert current.status == FeatureQuotaStatus.RESERVED
    assert before + timedelta(seconds=73) <= current.lease_expires_at <= after + timedelta(seconds=73)
    for settle in [finalize_reservation, release_reservation]:
        with pytest.raises(OperationConflict):
            settle(db_session, operation_id=original.id, claim_version=original.claim_version)
        assert not db_session.in_transaction()
    # Capture IDs/arguments above: failed transactions expire attached fixture objects.
    with db_session.begin():
        monthly = db_session.get(FeatureQuotaMonthly, monthly_id)
        credits = db_session.get(FeatureQuotaExtra, extra_id)
        row = db_session.get(QuotaUsagePerRequest, original.id)
        assert (monthly.units_consumed, monthly.units_remaining) == (10, 0)
        assert (credits.units_consumed, credits.units_remaining) == (3, 4)
        assert row.status == FeatureQuotaStatus.RESERVED
        assert row.reserved_at == original.reserved_at
        assert (row.used_from_monthly, row.used_from_extra) == (10, 2)
        assert row.claim_version == current.claim_version
    done = finalize_reservation(
        db_session, operation_id=current.id, claim_version=current.claim_version,
        result_payload={"recovered": True},
    )
    assert done.status == FeatureQuotaStatus.DONE
