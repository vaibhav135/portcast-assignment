from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.models import FeatureQuotaMonthly, FeatureQuotaStatus, QuotaUsagePerRequest
from src.quota import (
    IdempotencyConflict,
    InsufficientQuota,
    OperationConflict,
    finalize_reservation,
    release_reservation,
    reserve_monthly,
)


def test_reservation_holds_units_and_records_operation(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    key = uuid4()
    operation = reserve_monthly(
        db_session,
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=3,
        idempotency_key=key,
    )

    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 3
    assert monthly_quota.units_remaining == 7
    stored = db_session.scalar(
        select(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.org_id == monthly_quota.org_id,
            QuotaUsagePerRequest.feature == monthly_quota.feature,
            QuotaUsagePerRequest.idempotency_key == key,
        )
    )
    assert stored is not None
    assert stored.id == operation.id
    assert stored.status == FeatureQuotaStatus.RESERVED
    assert stored.used_from_monthly == 3
    assert stored.used_from_extra == 0
    assert stored.claim_version == 0
    assert stored.lease_expires_at > stored.reserved_at


def test_insufficient_quota_changes_nothing(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    with pytest.raises(InsufficientQuota):
        reserve_monthly(
            db_session,
            org_id=monthly_quota.org_id,
            feature=monthly_quota.feature,
            units=11,
            idempotency_key=uuid4(),
        )

    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 0
    assert monthly_quota.units_remaining == 10
    operations = db_session.scalars(
        select(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.org_id == monthly_quota.org_id
        )
    ).all()
    assert operations == []


def test_retry_reuses_reservation_even_when_quota_is_exhausted(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    key = uuid4()
    arguments = dict(
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=10,
        idempotency_key=key,
    )
    original = reserve_monthly(db_session, **arguments)
    repeated = reserve_monthly(db_session, **arguments)

    assert repeated.id == original.id
    assert repeated.reserved_at == original.reserved_at
    assert repeated.lease_expires_at == original.lease_expires_at
    assert repeated.claim_version == original.claim_version
    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 10
    assert monthly_quota.units_remaining == 0
    operations = db_session.scalars(
        select(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.org_id == monthly_quota.org_id
        )
    ).all()
    assert len(operations) == 1


def test_same_key_with_different_units_is_rejected_without_changes(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    key = uuid4()
    arguments = dict(
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        idempotency_key=key,
    )
    original = reserve_monthly(db_session, units=3, **arguments)
    with pytest.raises(IdempotencyConflict):
        reserve_monthly(db_session, units=4, **arguments)

    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 3
    assert monthly_quota.units_remaining == 7
    stored = db_session.get(QuotaUsagePerRequest, original.id)
    assert stored is not None
    assert stored.used_from_monthly == 3
    assert stored.reserved_at == original.reserved_at


@pytest.mark.parametrize(
    "settle, expected_status, consumed, remaining, opposite",
    [
        (finalize_reservation, FeatureQuotaStatus.DONE, 3, 7, release_reservation),
        (release_reservation, FeatureQuotaStatus.RELEASED, 0, 10, finalize_reservation),
    ],
)
def test_settlement_is_repeatable_but_cannot_change_terminal_state(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    settle,
    expected_status: FeatureQuotaStatus,
    consumed: int,
    remaining: int,
    opposite,
) -> None:
    operation = reserve_monthly(
        db_session,
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=3,
        idempotency_key=uuid4(),
    )
    arguments = dict(operation_id=operation.id, claim_version=operation.claim_version)
    settled = settle(db_session, **arguments)
    repeated = settle(db_session, **arguments)
    assert settled.status == repeated.status == expected_status
    assert settled.id == repeated.id == operation.id
    with pytest.raises(OperationConflict):
        opposite(db_session, **arguments)
    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == consumed
    assert monthly_quota.units_remaining == remaining
    assert db_session.get(QuotaUsagePerRequest, operation.id).status == expected_status


def test_stale_claim_cannot_finalize_or_release(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    operation = reserve_monthly(
        db_session,
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=3,
        idempotency_key=uuid4(),
    )
    # Simulate a committed ownership takeover without changing original reservation time.
    with db_session.begin():
        db_session.execute(
            update(QuotaUsagePerRequest)
            .where(QuotaUsagePerRequest.id == operation.id)
            .values(claim_version=1)
        )
    for settle in (finalize_reservation, release_reservation):
        with pytest.raises(OperationConflict):
            settle(db_session, operation_id=operation.id, claim_version=0)
    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 3
    assert monthly_quota.units_remaining == 7
    assert db_session.get(QuotaUsagePerRequest, operation.id).status == FeatureQuotaStatus.RESERVED


def test_old_month_release_does_not_refund_current_month(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    operation = reserve_monthly(
        db_session,
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=3,
        idempotency_key=uuid4(),
    )
    # Simulate resetting the single aggregate row and consuming two units in the new month.
    next_reset = (monthly_quota.resets_on + timedelta(days=32)).replace(day=1)
    with db_session.begin():
        db_session.execute(
            update(FeatureQuotaMonthly)
            .where(FeatureQuotaMonthly.id == monthly_quota.id)
            .values(
                units_consumed=2,
                units_remaining=8,
                last_renewed=monthly_quota.resets_on,
                resets_on=next_reset,
            )
        )
    released = release_reservation(
        db_session, operation_id=operation.id, claim_version=operation.claim_version
    )
    assert released.status == FeatureQuotaStatus.RELEASED
    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 2
    assert monthly_quota.units_remaining == 8
