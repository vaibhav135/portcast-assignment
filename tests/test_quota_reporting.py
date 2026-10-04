from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import src.shared.quota as quota_module
from src.server.main import app
from src.shared.database import get_session
from src.shared.models import APIFeature, FeatureQuotaExtra, FeatureQuotaMonthly, Organization
from src.shared.quota import finalize_reservation, get_quota_usage, reserve_quota


def test_reporting_separates_completed_units_and_both_reservation_sources(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
) -> None:
    arguments = dict(org_id=monthly_quota.org_id, feature=monthly_quota.feature)
    completed = reserve_quota(db_session, units=3, idempotency_key=uuid4(), **arguments)
    finalize_reservation(
        db_session, operation_id=completed.id, claim_version=completed.claim_version
    )
    reserve_quota(db_session, units=9, idempotency_key=uuid4(), **arguments)
    usage = get_quota_usage(db_session, **arguments)
    assert usage.monthly.model_dump() == dict(limit=10, used=3, reserved=7, available=0)
    assert usage.credits.reserved == 2
    assert usage.credits.available == 5
    assert usage.credits.expires_on == extra_quota.expires_on
    assert usage.next_reset == monthly_quota.resets_on


def test_reporting_refreshes_without_admission_and_retains_old_credit_holds(
    db_session: Session,
    monthly_quota: FeatureQuotaMonthly,
    extra_quota: FeatureQuotaExtra,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = datetime(2027, 1, 1, tzinfo=timezone.utc)
    with db_session.begin():
        monthly_quota.resets_on = boundary
        extra_quota.expires_on = boundary + timedelta(days=1)
    monkeypatch.setattr(
        quota_module, "_database_now", lambda session: boundary - timedelta(seconds=1)
    )
    arguments = dict(org_id=monthly_quota.org_id, feature=monthly_quota.feature)
    reserve_quota(db_session, units=13, idempotency_key=uuid4(), **arguments)
    monkeypatch.setattr(
        quota_module, "_database_now", lambda session: boundary + timedelta(days=2)
    )
    usage = get_quota_usage(db_session, **arguments)
    assert usage.monthly.model_dump() == dict(limit=10, used=0, reserved=0, available=10)
    assert usage.period_start == boundary
    assert usage.next_reset == datetime(2027, 2, 1, tzinfo=timezone.utc)
    assert usage.credits.reserved == 3
    assert usage.credits.available == 0  # Expired, not erased or made usable by reset.
    assert usage.credits.expires_on == boundary + timedelta(days=1)


def test_usage_endpoint_is_scoped_to_org_and_feature(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> None:
    with db_session.begin():
        other_org = Organization(name=f"reporting-test-{uuid4()}")
        db_session.add(other_org)
        db_session.flush()
        db_session.add(
            FeatureQuotaMonthly(
                org_id=other_org.id,
                feature=monthly_quota.feature,
                total_allocated=99,
                units_consumed=0,
                units_remaining=99,
                resets_on=monthly_quota.resets_on,
            )
        )
    reserve_quota(
        db_session,
        org_id=other_org.id,
        feature=monthly_quota.feature,
        units=20,
        idempotency_key=uuid4(),
    )

    def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            path = f"/orgs/{monthly_quota.org_id}/features"
            response = client.get(f"{path}/{monthly_quota.feature.value}/usage")
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["org_id"] == monthly_quota.org_id
            assert body["monthly"] == dict(limit=10, used=0, reserved=0, available=10)
            assert body["credits"] == dict(reserved=0, available=0, expires_on=None)
            assert client.get(f"{path}/{APIFeature.SAILING_SCHEDULE.value}/usage").status_code == 404
            assert client.get(f"{path}/not-a-feature/usage").status_code == 422
    finally:
        app.dependency_overrides.pop(get_session, None)
