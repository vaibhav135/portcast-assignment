from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from dateutil.relativedelta import relativedelta
from sqlalchemy.orm import Session

import src.quota as quota_module
from src.models import FeatureQuotaExtra, FeatureQuotaMonthly, FeatureQuotaStatus
from src.quota import release_reservation, reserve_quota


@pytest.mark.parametrize(
    "boundary",
    [datetime(2028, 3, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)],
    ids=["leap-february", "year-transition"],
)
def test_exact_month_boundary_refreshes_once_without_rollover_or_credit_reset(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
    monkeypatch: pytest.MonkeyPatch,
    boundary: datetime,
) -> None:
    with db_session.begin():
        monthly_quota.units_consumed = 6
        monthly_quota.units_remaining = 4
        monthly_quota.resets_on = boundary
        monthly_quota.last_renewed = boundary - relativedelta(months=1)
    original_row_id = monthly_quota.id
    arguments = dict(org_id=monthly_quota.org_id, feature=monthly_quota.feature)
    before = boundary - timedelta(seconds=1)
    monkeypatch.setattr(quota_module, "_database_now", lambda session: before)
    old_operation = reserve_quota(db_session, units=1, idempotency_key=uuid4(), **arguments)

    monkeypatch.setattr(quota_module, "_database_now", lambda session: boundary)
    new_operation = reserve_quota(db_session, units=3, idempotency_key=uuid4(), **arguments)
    db_session.refresh(monthly_quota)
    db_session.refresh(extra_quota)
    assert monthly_quota.id == original_row_id
    assert monthly_quota.units_consumed == 3
    assert monthly_quota.units_remaining == 7  # No rollover of the previous three units.
    assert monthly_quota.resets_on == boundary + relativedelta(months=1)
    assert monthly_quota.last_renewed == boundary
    assert extra_quota.units_consumed == 0
    assert extra_quota.units_remaining == 7
    assert old_operation.reserved_at == before
    assert new_operation.reserved_at == boundary
    db_session.commit()  # End the inspection transaction before calling settlement.

    released = release_reservation(
        db_session, operation_id=old_operation.id, claim_version=old_operation.claim_version
    )
    assert released.status == FeatureQuotaStatus.RELEASED
    assert released.reserved_at == before
    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 3
    assert monthly_quota.units_remaining == 7


def test_inactive_months_grant_only_one_current_allowance(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2027, 5, 12, 10, tzinfo=timezone.utc)
    with db_session.begin():
        monthly_quota.units_consumed = 10
        monthly_quota.units_remaining = 0
        monthly_quota.resets_on = datetime(2027, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(quota_module, "_database_now", lambda session: now)
    reserve_quota(
        db_session,
        org_id=monthly_quota.org_id,
        feature=monthly_quota.feature,
        units=10,
        idempotency_key=uuid4(),
    )
    db_session.refresh(monthly_quota)
    assert monthly_quota.units_consumed == 10
    assert monthly_quota.units_remaining == 0
    assert monthly_quota.resets_on == datetime(2027, 6, 1, tzinfo=timezone.utc)
