from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import FeatureQuotaMonthly, FeatureQuotaStatus, QuotaUsagePerRequest
from src.quota import InsufficientQuota, reserve_monthly


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
