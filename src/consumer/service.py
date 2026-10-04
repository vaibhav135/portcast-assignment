"""Small, deliberately side-effect-free consumer; no live shipping-provider calls."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..shared.models import APIQuotaMap, APIFeature, FeatureQuotaStatus, QuotaUsagePerRequest
from ..shared.quota import (
    IdempotencyConflict,
    OperationConflict,
    QuotaNotConfigured,
    _reserve_quota_in_transaction,
    claim_expired_reservations,
    finalize_reservation,
    release_reservation,
)
from .schemas import (
    DemoBehavior,
    ScheduleResult,
    ScheduleSearchRequest,
    ScheduleSearchResponse,
)
from .timing import time_quota


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
    operation = time_quota(
        "quota_admission",
        _admit_schedule_operation,
        session,
        org_id=org_id,
        idempotency_key=idempotency_key,
        request=request,
    )
    return execute_schedule_operation(session, operation)


def _admit_schedule_operation(
    session: Session,
    *,
    org_id: int,
    idempotency_key: UUID,
    request: ScheduleSearchRequest,
) -> QuotaUsagePerRequest:
    """Commit fresh lookup, pricing, and reservation together before feature work."""
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
        fresh = operation is None
        if operation is not None:
            # Existing work retains its original allocation, regardless of repricing.
            if operation.request_payload != request_payload:
                raise IdempotencyConflict("Operation key already used with different inputs")
            session.expunge(operation)
        else:
            configuration = session.execute(
                select(APIQuotaMap.unit_cost, APIQuotaMap.lease_duration_sec)
                .where(APIQuotaMap.feature == feature)
            ).one_or_none()
            if configuration is None:
                raise QuotaNotConfigured("Schedule-search feature is not configured")
            unit_cost, lease_duration = configuration
            # Lookup is only a fast path; atomic insertion still fences racing keys.
            operation = _reserve_quota_in_transaction(
                session,
                org_id=org_id,
                feature=feature,
                units=len(request.routes) * unit_cost,
                idempotency_key=idempotency_key,
                lease_duration=lease_duration,
                request_payload=request_payload,
                reject_pending_replay=True,
            )
            # Fresh work is durable only after exiting this transaction.
    if not fresh and operation.status == FeatureQuotaStatus.RESERVED:
        claims = claim_expired_reservations(
            session, feature=feature, operation_id=operation.id, limit=1
        )
        if not claims:
            raise OperationConflict("Operation is in progress; retry later")
        operation = claims[0]
    return operation


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
            time_quota(
                "quota_release",
                release_reservation,
                session, operation_id=operation.id, claim_version=operation.claim_version
            )
            raise
        operation = time_quota(
            "quota_finalize",
            finalize_reservation,
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
