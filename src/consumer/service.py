"""Small, deliberately side-effect-free consumer; no live shipping-provider calls."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..shared.models import APIQuotaMap, APIFeature, FeatureQuotaStatus, QuotaUsagePerRequest
from ..shared.quota import (
    IdempotencyConflict,
    OperationConflict,
    QuotaNotConfigured,
    claim_expired_reservations,
    finalize_reservation,
    release_reservation,
    reserve_quota,
)
from .schemas import (
    DemoBehavior,
    ScheduleResult,
    ScheduleSearchRequest,
    ScheduleSearchResponse,
)


class DemoFeatureFailure(Exception):
    """Confirmed failure with no remaining execution or side effects."""


class DemoFeatureTimeout(DemoFeatureFailure):
    """Injected safely stopped timeout, not an uncertain remote timeout."""


def perform_schedule_lookup(request: ScheduleSearchRequest) -> dict:
    if request.demo_behavior == DemoBehavior.FAIL:
        raise DemoFeatureFailure("Demo schedule provider failed")
    if request.demo_behavior == DemoBehavior.TIMEOUT:
        raise DemoFeatureTimeout("Demo lookup timed out; no work remains running")
    return {
        "results": [
            ScheduleResult(route=route, sailings=["demo-sailing-001"]).model_dump()
            for route in request.routes
        ]
    }


def search_schedules(
    session: Session,
    *,
    org_id: int,
    idempotency_key: UUID,
    request: ScheduleSearchRequest,
) -> ScheduleSearchResponse:
    feature = APIFeature.SAILING_SCHEDULE
    request_payload = request.model_dump(mode="json")
    with session.begin():
        operation = session.scalar(
            select(QuotaUsagePerRequest)
            .where(
                QuotaUsagePerRequest.org_id == org_id,
                QuotaUsagePerRequest.feature == feature,
                QuotaUsagePerRequest.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        )
        if operation is not None:
            # Existing work retains its original allocation, regardless of repricing.
            if operation.request_payload != request_payload:
                raise IdempotencyConflict("Operation key already used with different inputs")
            session.expunge(operation)
        else:
            unit_cost = session.scalar(
                select(APIQuotaMap.unit_cost).where(APIQuotaMap.feature == feature)
            )
            if unit_cost is None:
                raise QuotaNotConfigured("Schedule-search feature is not configured")
    if operation is not None and operation.status == FeatureQuotaStatus.RESERVED:
        claims = claim_expired_reservations(
            session, feature=feature, operation_id=operation.id, limit=1
        )
        if not claims:
            raise OperationConflict("Operation is in progress; retry later")
        operation = claims[0]
    elif operation is None:
        # This lookup is only a replay fast path. Atomic key claiming in reserve_quota
        # still protects concurrent new requests that both observed no existing row.
        operation = reserve_quota(
            session,
            org_id=org_id,
            feature=feature,
            units=len(request.routes) * unit_cost,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            reject_pending_replay=True,
        )
    return execute_schedule_operation(session, operation)


def execute_schedule_operation(
    session: Session, operation: QuotaUsagePerRequest
) -> ScheduleSearchResponse:
    """Execute admitted/claimed pure work outside transactions; never reserve again."""
    if operation.feature != APIFeature.SAILING_SCHEDULE:
        raise ValueError("Only the pure demo schedule feature supports recovery")
    if operation.status == FeatureQuotaStatus.RELEASED:
        raise OperationConflict("Operation was released; use a new key for new work")
    if operation.status != FeatureQuotaStatus.DONE:
        request = ScheduleSearchRequest.model_validate(operation.request_payload)
        try:
            result = perform_schedule_lookup(request)
        except DemoFeatureFailure:
            # Only known failures are refunded. Database errors/unknown commits
            # are not evidence of failed execution and must not trigger a refund.
            release_reservation(
                session, operation_id=operation.id, claim_version=operation.claim_version
            )
            raise
        operation = finalize_reservation(
            session,
            operation_id=operation.id,
            claim_version=operation.claim_version,
            result_payload=result,
        )
    if operation.result_payload is None:
        raise RuntimeError("Completed demo operation has no recoverable result")
    return ScheduleSearchResponse(
        operation_id=operation.id,
        status=operation.status,
        results=operation.result_payload["results"],
    )
