from datetime import timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .models import (
    APIQuotaMap,
    APIFeature,
    FeatureQuotaExtra,
    FeatureQuotaMonthly,
    FeatureQuotaStatus,
    QuotaUsagePerRequest,
)


class InsufficientQuota(Exception):
    """The request cannot be admitted without exceeding available capacity."""


class IdempotencyConflict(Exception):
    """The operation key was already used with a different unit quantity."""


class OperationConflict(Exception):
    """The claim is stale or the requested transition conflicts with final state."""


def reserve_quota(
    session: Session,
    *,
    org_id: int,
    feature: APIFeature,
    units: int,
    idempotency_key: UUID,
) -> QuotaUsagePerRequest:
    """Reserve monthly allowance first, then unexpired credits, in one transaction.

    Assumes a configured organization/feature in the current period. Keys are
    scoped to organization and feature; same-key retries return the existing
    operation, while a changed unit quantity raises IdempotencyConflict.

    The function owns its transaction: use a session without an active transaction.
    Insufficient capacity must leave both balances and operation records unchanged.
    Return a detached RESERVED operation after committing; do not execute feature
    work here. The caller must inspect its status; replay does not restart released
    work or take over an expired lease. Resets and recovery are separate
    slices. Concurrent key claiming relies on PostgreSQL READ COMMITTED isolation.
    """
    if units <= 0:
        raise ValueError("Reservation units must be positive")

    with session.begin():
        lease_duration = session.scalar(
            select(APIQuotaMap.lease_duration_sec).where(APIQuotaMap.feature == feature)
        )
        if lease_duration is None:
            raise ValueError(f"No quota configuration for feature {feature}")

        # Claim identity before capacity. PostgreSQL waits on an uncommitted
        # duplicate, so only the winning transaction can acquire units.
        operation = session.scalar(
            insert(QuotaUsagePerRequest)
            .values(
                org_id=org_id,
                feature=feature,
                status=FeatureQuotaStatus.RESERVED,
                # Provisional allocation claims the key. The actual split is
                # recorded below before commit; no other transaction sees this.
                used_from_monthly=units,
                used_from_extra=0,
                idempotency_key=idempotency_key,
                reserved_at=func.clock_timestamp(),
                lease_expires_at=func.clock_timestamp()
                + timedelta(seconds=lease_duration),
            )
            .on_conflict_do_nothing(constraint="uq_operation_key")
            .returning(QuotaUsagePerRequest)
        )
        if operation is None:
            existing = session.scalar(
                select(QuotaUsagePerRequest)
                .where(
                    QuotaUsagePerRequest.org_id == org_id,
                    QuotaUsagePerRequest.feature == feature,
                    QuotaUsagePerRequest.idempotency_key == idempotency_key,
                )
                .execution_options(populate_existing=True)
            )
            if existing is None:
                raise RuntimeError("Conflicting operation disappeared during replay")
            if existing.used_from_monthly + existing.used_from_extra != units:
                raise IdempotencyConflict("Operation key already used with different units")
            session.expunge(existing)
            return existing

        # Hold the monthly row while determining the split. All accounting paths
        # acquire locks in operation -> monthly -> credits order.
        available_monthly = session.scalar(
            select(FeatureQuotaMonthly.units_remaining)
            .where(
                FeatureQuotaMonthly.org_id == org_id,
                FeatureQuotaMonthly.feature == feature,
            )
            .with_for_update()
        )
        if available_monthly is None:
            raise InsufficientQuota("Monthly quota is not configured")
        monthly_units = min(available_monthly, units)
        credit_units = units - monthly_units

        if monthly_units:
            acquired = session.scalar(
                update(FeatureQuotaMonthly)
                .where(
                    FeatureQuotaMonthly.org_id == org_id,
                    FeatureQuotaMonthly.feature == feature,
                    FeatureQuotaMonthly.units_remaining >= monthly_units,
                )
                .values(
                    units_consumed=FeatureQuotaMonthly.units_consumed + monthly_units,
                    units_remaining=FeatureQuotaMonthly.units_remaining - monthly_units,
                )
                .returning(FeatureQuotaMonthly.id)
                .execution_options(synchronize_session=False)
            )
            if acquired is None:
                raise RuntimeError("Locked monthly balance changed unexpectedly")

        if credit_units:
            acquired = session.scalar(
                update(FeatureQuotaExtra)
                .where(
                    FeatureQuotaExtra.org_id == org_id,
                    FeatureQuotaExtra.feature == feature,
                    FeatureQuotaExtra.units_remaining >= credit_units,
                    FeatureQuotaExtra.expires_on > func.clock_timestamp(),
                )
                .values(
                    units_consumed=FeatureQuotaExtra.units_consumed + credit_units,
                    units_remaining=FeatureQuotaExtra.units_remaining - credit_units,
                )
                .returning(FeatureQuotaExtra.id)
                .execution_options(synchronize_session=False)
            )
            if acquired is None:
                # This exception rolls back the monthly deduction and key claim too.
                raise InsufficientQuota("Combined allowance and usable credits are insufficient")

        operation.used_from_monthly = monthly_units
        operation.used_from_extra = credit_units
        session.flush()
        # Keep the returned snapshot readable even with expire_on_commit=True.
        session.expunge(operation)

    return operation


def finalize_reservation(
    session: Session, *, operation_id: int, claim_version: int
) -> QuotaUsagePerRequest:
    """Record confirmed success without deducting capacity a second time."""
    return _settle_reservation(
        session,
        operation_id=operation_id,
        claim_version=claim_version,
        target=FeatureQuotaStatus.DONE,
    )


def release_reservation(
    session: Session, *, operation_id: int, claim_version: int
) -> QuotaUsagePerRequest:
    """Refund a confirmed failure once, preserving source periods and credit expiry."""
    return _settle_reservation(
        session,
        operation_id=operation_id,
        claim_version=claim_version,
        target=FeatureQuotaStatus.RELEASED,
    )


def _settle_reservation(
    session: Session,
    *,
    operation_id: int,
    claim_version: int,
    target: FeatureQuotaStatus,
) -> QuotaUsagePerRequest:
    # Serialize competing success/failure calls on this operation. Keep the lock
    # through any compensating balance update and state change in the same commit.
    with session.begin():
        operation = session.scalar(
            select(QuotaUsagePerRequest)
            .where(QuotaUsagePerRequest.id == operation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation is None:
            raise ValueError("Reservation operation not found")
        if operation.claim_version != claim_version:
            raise OperationConflict("Operation ownership has changed")
        if operation.status == target:
            session.expunge(operation)
            return operation
        if operation.status != FeatureQuotaStatus.RESERVED:
            raise OperationConflict(f"Operation is already {operation.status.value}")
        if target == FeatureQuotaStatus.RELEASED:
            quota = session.scalar(
                select(FeatureQuotaMonthly)
                .where(
                    FeatureQuotaMonthly.org_id == operation.org_id,
                    FeatureQuotaMonthly.feature == operation.feature,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if quota is None:
                raise RuntimeError("Monthly accounting row is missing")

            # resets_on is the exclusive next-month boundary. No historical
            # aggregate is needed: only refund if this row still holds the same month.
            current_period = (quota.resets_on - timedelta(microseconds=1)).astimezone(
                timezone.utc
            )
            reserved_at = operation.reserved_at.astimezone(timezone.utc)
            current_month = (current_period.year, current_period.month)
            reserved_month = (reserved_at.year, reserved_at.month)
            if reserved_month > current_month:
                raise RuntimeError("Monthly accounting period precedes the reservation")
            if reserved_month == current_month:
                quota.units_consumed -= operation.used_from_monthly
                quota.units_remaining += operation.used_from_monthly

            if operation.used_from_extra:
                credits = session.scalar(
                    select(FeatureQuotaExtra)
                    .where(
                        FeatureQuotaExtra.org_id == operation.org_id,
                        FeatureQuotaExtra.feature == operation.feature,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if credits is None:
                    raise RuntimeError("Credit accounting row is missing")
                credits.units_consumed -= operation.used_from_extra
                credits.units_remaining += operation.used_from_extra
                # Never extend validity: refunded expired credits remain unusable.

        operation.status = target
        session.flush()
        session.expunge(operation)

    return operation
