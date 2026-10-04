from datetime import datetime, timedelta, timezone
from uuid import UUID

from dateutil.relativedelta import relativedelta
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
from .schemas import CreditUsage, MonthlyUsage, QuotaUsageResponse


class InsufficientQuota(Exception):
    """The request cannot be admitted without exceeding available capacity."""


class IdempotencyConflict(Exception):
    """The operation key was already used with different units or inputs."""


class OperationConflict(Exception):
    """The claim is stale or the requested transition conflicts with final state."""


class QuotaNotConfigured(Exception):
    """No included allowance is configured for the organization and feature."""


def _database_now(session: Session) -> datetime:
    """Authoritative clock; tests may replace this seam without mocking accounting."""
    return session.scalar(select(func.clock_timestamp())).astimezone(timezone.utc)


def _refresh_monthly_quota(session: Session, quota: FeatureQuotaMonthly) -> datetime:
    """Refresh in place; the caller must hold this aggregate row's FOR UPDATE lock."""
    now = _database_now(session)
    if now >= quota.resets_on:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        quota.units_consumed = 0
        quota.units_remaining = quota.total_allocated
        quota.last_renewed = now
        quota.resets_on = month_start + relativedelta(months=1)
        session.flush()
    return now


def get_quota_usage(
    session: Session, *, org_id: int, feature: APIFeature
) -> QuotaUsageResponse:
    """Report current included usage and usable credits; owns a short transaction.

    Lock balances in the same monthly -> credits order as admission/release. The
    reservation aggregates use one statement snapshot; finalization only changes
    classification, not consumption. Never lock operation rows here: settlement
    may already hold one while waiting for the monthly lock.
    """
    with session.begin():
        monthly = session.scalar(
            select(FeatureQuotaMonthly)
            .where(
                FeatureQuotaMonthly.org_id == org_id,
                FeatureQuotaMonthly.feature == feature,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if monthly is None:
            raise QuotaNotConfigured("Quota not configured for organization and feature")
        now = _refresh_monthly_quota(session, monthly)
        period_start = monthly.resets_on - relativedelta(months=1)
        credits = session.scalar(
            select(FeatureQuotaExtra)
            .where(FeatureQuotaExtra.org_id == org_id, FeatureQuotaExtra.feature == feature)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        monthly_reserved, credit_reserved = session.execute(
            select(
                func.coalesce(
                    func.sum(QuotaUsagePerRequest.used_from_monthly).filter(
                        QuotaUsagePerRequest.reserved_at >= period_start,
                        QuotaUsagePerRequest.reserved_at < monthly.resets_on,
                    ),
                    0,
                ),
                func.coalesce(func.sum(QuotaUsagePerRequest.used_from_extra), 0),
            ).where(
                QuotaUsagePerRequest.org_id == org_id,
                QuotaUsagePerRequest.feature == feature,
                QuotaUsagePerRequest.status == FeatureQuotaStatus.RESERVED,
            )
        ).one()
        response = QuotaUsageResponse(
            org_id=org_id,
            feature=feature,
            period_start=period_start,
            next_reset=monthly.resets_on,
            monthly=MonthlyUsage(
                limit=monthly.total_allocated,
                used=monthly.units_consumed - monthly_reserved,
                reserved=monthly_reserved,
                available=monthly.units_remaining,
            ),
            credits=CreditUsage(
                reserved=credit_reserved,
                available=(
                    credits.units_remaining
                    if credits is not None and credits.expires_on > now
                    else 0
                ),
                expires_on=credits.expires_on if credits is not None else None,
            ),
        )
    return response


def reserve_quota(
    session: Session,
    *,
    org_id: int,
    feature: APIFeature,
    units: int,
    idempotency_key: UUID,
    request_payload: dict | None = None,
    reject_pending_replay: bool = False,
) -> QuotaUsagePerRequest:
    """Reserve monthly allowance first, then unexpired credits, in one transaction.

    Assumes a configured organization/feature; an expired monthly row is refreshed
    lazily under its lock. Keys are
    scoped to organization and feature; same-key retries return the existing
    operation, while a changed unit quantity raises IdempotencyConflict.

    The function owns its transaction: use a session without an active transaction.
    Insufficient capacity must leave both balances and operation records unchanged.
    Return a detached RESERVED operation after committing; do not execute feature
    work here. The caller must inspect its status; replay does not restart released
    work or take over an expired lease. Recovery is a separate slice.
    Concurrent key claiming relies on PostgreSQL READ COMMITTED isolation.
    """
    if units <= 0:
        raise ValueError("Reservation units must be positive")

    with session.begin():
        lease_duration = session.scalar(
            select(APIQuotaMap.lease_duration_sec).where(APIQuotaMap.feature == feature)
        )
        if lease_duration is None:
            raise ValueError(f"No quota configuration for feature {feature}")

        return _reserve_quota_in_transaction(
            session,
            org_id=org_id,
            feature=feature,
            units=units,
            idempotency_key=idempotency_key,
            lease_duration=lease_duration,
            request_payload=request_payload,
            reject_pending_replay=reject_pending_replay,
        )


def _reserve_quota_in_transaction(
    session: Session,
    *,
    org_id: int,
    feature: APIFeature,
    units: int,
    idempotency_key: UUID,
    lease_duration: int,
    request_payload: dict | None = None,
    reject_pending_replay: bool = False,
) -> QuotaUsagePerRequest:
    """Internal admission primitive; caller owns commit/rollback and configuration.

    Return a detached snapshot, but it is not durable until the caller commits.
    Never execute feature work while this admission transaction is still open.
    """
    if not session.in_transaction():
        raise RuntimeError("Reservation requires an active transaction")
    if units <= 0:
        raise ValueError("Reservation units must be positive")
    if lease_duration <= 0:
        raise ValueError("Reservation lease duration must be positive")

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
            request_payload=request_payload,
            reserved_at=func.clock_timestamp(),
            lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_duration),
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
        if existing.request_payload != request_payload:
            raise IdempotencyConflict("Operation key already used with different inputs")
        if reject_pending_replay and existing.status == FeatureQuotaStatus.RESERVED:
            raise OperationConflict("Operation is in progress; retry later")
        session.expunge(existing)
        return existing

    # Hold the monthly row while determining the split. All accounting paths
    # acquire locks in operation -> monthly -> credits order.
    monthly_quota = session.scalar(
        select(FeatureQuotaMonthly)
        .where(
            FeatureQuotaMonthly.org_id == org_id,
            FeatureQuotaMonthly.feature == feature,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if monthly_quota is None:
        raise InsufficientQuota("Monthly quota is not configured")
    admitted_at = _refresh_monthly_quota(session, monthly_quota)
    # Assign the original admission time only once, before its first commit.
    # Key/row-lock waits must not assign an old period or an already-expired lease.
    operation.reserved_at = admitted_at
    operation.lease_expires_at = admitted_at + timedelta(seconds=lease_duration)
    monthly_units = min(monthly_quota.units_remaining, units)
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
            # The caller must roll back the monthly deduction and key claim too.
            raise InsufficientQuota("Combined allowance and usable credits are insufficient")

    operation.used_from_monthly = monthly_units
    operation.used_from_extra = credit_units
    session.flush()
    # Keep the returned snapshot readable even with expire_on_commit=True.
    session.expunge(operation)
    return operation


def claim_expired_reservations(
    session: Session,
    *,
    feature: APIFeature,
    limit: int = 20,
    operation_id: int | None = None,
) -> list[QuotaUsagePerRequest]:
    """Claim expired holds without spending/refunding units; owns its transaction.

    SKIP LOCKED lets competing recovery processes work on different operations.
    Expiry permits takeover, not proof of failed work. The caller must know that
    rerunning its feature is safe and settle using the returned claim version.
    """
    if limit <= 0:
        raise ValueError("Recovery batch size must be positive")
    with session.begin():
        query = (
            select(QuotaUsagePerRequest, APIQuotaMap.lease_duration_sec)
            .join(APIQuotaMap, APIQuotaMap.feature == QuotaUsagePerRequest.feature)
            .where(
                QuotaUsagePerRequest.feature == feature,
                QuotaUsagePerRequest.status == FeatureQuotaStatus.RESERVED,
                QuotaUsagePerRequest.lease_expires_at <= func.clock_timestamp(),
            )
            .order_by(QuotaUsagePerRequest.lease_expires_at, QuotaUsagePerRequest.id)
            .limit(limit)
            .with_for_update(of=QuotaUsagePerRequest, skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if operation_id is not None:
            query = query.where(QuotaUsagePerRequest.id == operation_id)
        rows = session.execute(query).all()
        now = _database_now(session)
        operations = []
        for operation, lease_duration in rows:
            operation.claim_version += 1
            operation.lease_expires_at = now + timedelta(seconds=lease_duration)
            operations.append(operation)
        session.flush()
        for operation in operations:
            session.expunge(operation)
    return operations


def finalize_reservation(
    session: Session,
    *,
    operation_id: int,
    claim_version: int,
    result_payload: dict | None = None,
) -> QuotaUsagePerRequest:
    """Record confirmed success without deducting capacity a second time."""
    return _settle_reservation(
        session,
        operation_id=operation_id,
        claim_version=claim_version,
        target=FeatureQuotaStatus.DONE,
        result_payload=result_payload,
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
    result_payload: dict | None = None,
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

            _refresh_monthly_quota(session, quota)

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

        if target == FeatureQuotaStatus.DONE:
            # Result and DONE commit together; a replay never overwrites this result.
            operation.result_payload = result_payload
        operation.status = target
        session.flush()
        session.expunge(operation)

    return operation
