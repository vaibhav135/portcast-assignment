from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.models import (
    FeatureQuotaExtra,
    FeatureQuotaMonthly,
    FeatureQuotaStatus,
    QuotaUsagePerRequest,
)
from src.quota import (
    IdempotencyConflict,
    InsufficientQuota,
    OperationConflict,
    finalize_reservation,
    release_reservation,
    reserve_quota,
)


def test_reservation_holds_units_and_records_operation(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    key = uuid4()
    operation = reserve_quota(
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
        reserve_quota(
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
    original = reserve_quota(db_session, **arguments)
    repeated = reserve_quota(db_session, **arguments)

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
    original = reserve_quota(db_session, units=3, **arguments)
    with pytest.raises(IdempotencyConflict):
        reserve_quota(db_session, units=4, **arguments)

    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 3
    assert monthly_quota.units_remaining == 7
    stored = db_session.get(QuotaUsagePerRequest, original.id)
    assert stored is not None
    assert stored.used_from_monthly == 3
    assert stored.reserved_at == original.reserved_at


@pytest.mark.parametrize(
    "settle, expected_status, opposite",
    [
        (finalize_reservation, FeatureQuotaStatus.DONE, release_reservation),
        (release_reservation, FeatureQuotaStatus.RELEASED, finalize_reservation),
    ],
)
@pytest.mark.parametrize("units", [3, 13], ids=["monthly", "mixed"])
def test_settlement_is_repeatable_but_cannot_change_terminal_state(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
    settle,
    expected_status: FeatureQuotaStatus,
    opposite,
    units: int,
) -> None:
    operation = reserve_quota(
        db_session,
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=units,
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
    db_session.refresh(extra_quota)
    finalized = expected_status == FeatureQuotaStatus.DONE
    monthly_consumed = min(units, 10) if finalized else 0
    credit_consumed = max(units - 10, 0) if finalized else 0
    assert monthly_quota.units_consumed == monthly_consumed
    assert monthly_quota.units_remaining == 10 - monthly_consumed
    assert extra_quota.units_consumed == credit_consumed
    assert extra_quota.units_remaining == 7 - credit_consumed
    assert db_session.get(QuotaUsagePerRequest, operation.id).status == expected_status


def test_stale_claim_cannot_finalize_or_release(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    operation = reserve_quota(
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
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
) -> None:
    operation = reserve_quota(
        db_session,
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=13,
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
    db_session.refresh(extra_quota)
    assert extra_quota.units_consumed == 0
    assert extra_quota.units_remaining == 7


def test_mixed_reservation_uses_monthly_first_and_replays_without_another_charge(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
) -> None:
    arguments = dict(
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=13,
        idempotency_key=uuid4(),
    )
    operation = reserve_quota(db_session, **arguments)
    repeated = reserve_quota(db_session, **arguments)
    assert operation.id == repeated.id
    assert operation.used_from_monthly == 10
    assert operation.used_from_extra == 3
    db_session.refresh(monthly_quota)
    db_session.refresh(extra_quota)
    assert monthly_quota.units_consumed == 10
    assert monthly_quota.units_remaining == 0
    assert extra_quota.units_consumed == 3
    assert extra_quota.units_remaining == 4


@pytest.mark.parametrize("expired", [False, True], ids=["insufficient", "expired"])
def test_unusable_credits_roll_back_monthly_deduction_and_operation(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
    expired: bool,
) -> None:
    if expired:
        with db_session.begin():
            extra_quota.expires_on = extra_quota.last_added - timedelta(days=1)
    with pytest.raises(InsufficientQuota):
        reserve_quota(
            db_session,
            org_id=monthly_quota.org_id,
            feature=monthly_quota.feature,
            units=13 if expired else 18,
            idempotency_key=uuid4(),
        )
    db_session.refresh(monthly_quota)
    db_session.refresh(extra_quota)
    assert monthly_quota.units_consumed == 0
    assert monthly_quota.units_remaining == 10
    assert extra_quota.units_consumed == 0
    assert extra_quota.units_remaining == 7
    assert db_session.scalars(
        select(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.org_id == monthly_quota.org_id
        )
    ).all() == []


def test_credit_only_release_refunds_once_without_extending_expiration(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
) -> None:
    arguments = dict(org_id=monthly_quota.org_id, feature=monthly_quota.feature)
    reserve_quota(db_session, units=10, idempotency_key=uuid4(), **arguments)
    operation = reserve_quota(db_session, units=7, idempotency_key=uuid4(), **arguments)
    assert operation.used_from_monthly == 0
    assert operation.used_from_extra == 7
    expires_on = extra_quota.last_added - timedelta(days=1)
    with db_session.begin():
        extra_quota.expires_on = expires_on

    for _ in range(2):
        released = release_reservation(
            db_session, operation_id=operation.id, claim_version=operation.claim_version
        )
        assert released.status == FeatureQuotaStatus.RELEASED
    # A refund restores accounting, not credit validity.
    with pytest.raises(InsufficientQuota):
        reserve_quota(db_session, units=7, idempotency_key=uuid4(), **arguments)
    db_session.refresh(monthly_quota)
    db_session.refresh(extra_quota)
    assert monthly_quota.units_consumed == 10
    assert monthly_quota.units_remaining == 0
    assert extra_quota.units_consumed == 0
    assert extra_quota.units_remaining == 7
    assert extra_quota.expires_on == expires_on
