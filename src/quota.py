from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from .models import (
    APIQuotaMap,
    APIFeature,
    FeatureQuotaMonthly,
    FeatureQuotaStatus,
    QuotaUsagePerRequest,
)


class InsufficientQuota(Exception):
    """The request cannot be admitted without exceeding available capacity."""


def reserve_monthly(
    session: Session,
    *,
    org_id: int,
    feature: APIFeature,
    units: int,
    idempotency_key: UUID,
) -> QuotaUsagePerRequest:
    """Reserve monthly units and persist the operation in one short transaction.

    First slice: a configured organization/feature in the current period, positive
    units, and a new operation key. Credits, duplicate-key recovery, and resets
    will extend this contract later.

    The function owns its transaction: use a session without an active transaction.
    Insufficient capacity must leave both balances and operation records unchanged.
    Return a detached RESERVED operation after committing; do not execute feature
    work here. Database errors (including duplicate keys) roll back both changes.
    This first slice does not yet turn duplicate keys into retry-safe results.
    """
    if units <= 0:
        raise ValueError("Reservation units must be positive")

    with session.begin():
        lease_duration = session.scalar(
            select(APIQuotaMap.lease_duration_sec).where(APIQuotaMap.feature == feature)
        )
        if lease_duration is None:
            raise ValueError(f"No quota configuration for feature {feature}")

        acquired = session.scalar(
            update(FeatureQuotaMonthly)
            .where(
                FeatureQuotaMonthly.org_id == org_id,
                FeatureQuotaMonthly.feature == feature,
                FeatureQuotaMonthly.units_remaining >= units,
            )
            .values(
                units_consumed=FeatureQuotaMonthly.units_consumed + units,
                units_remaining=FeatureQuotaMonthly.units_remaining - units,
            )
            .returning(FeatureQuotaMonthly.id)
            .execution_options(synchronize_session=False)
        )
        if acquired is None:
            raise InsufficientQuota("Monthly quota unavailable or insufficient")

        operation = session.scalar(
            insert(QuotaUsagePerRequest)
            .values(
                org_id=org_id,
                feature=feature,
                status=FeatureQuotaStatus.RESERVED,
                used_from_monthly=units,
                used_from_extra=0,
                idempotency_key=idempotency_key,
                reserved_at=func.clock_timestamp(),
                lease_expires_at=func.clock_timestamp()
                + timedelta(seconds=lease_duration),
            )
            .returning(QuotaUsagePerRequest)
        )
        assert operation is not None
        # Keep the returned snapshot readable even with expire_on_commit=True.
        session.expunge(operation)

    return operation
