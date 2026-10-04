from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import src.consumer.service as consumer_module
from src.consumer.main import app
from src.shared.database import get_session
from src.shared.models import (
    APIQuotaMap,
    APIFeature,
    FeatureQuotaMonthly,
    FeatureQuotaStatus,
    QuotaUsagePerRequest,
)
from src.shared.quota import get_quota_usage


@pytest.fixture
def schedule_quota(
    db_session: Session, monthly_quota: FeatureQuotaMonthly
) -> FeatureQuotaMonthly:
    with db_session.begin():
        feature = APIFeature.SAILING_SCHEDULE
        if db_session.scalar(select(APIQuotaMap).where(APIQuotaMap.feature == feature)) is None:
            db_session.add(APIQuotaMap(feature=feature, unit_cost=1, lease_duration_sec=30))
        quota = FeatureQuotaMonthly(
            org_id=monthly_quota.org_id,
            feature=feature,
            total_allocated=10,
            units_consumed=0,
            units_remaining=10,
            resets_on=monthly_quota.resets_on,
        )
        db_session.add(quota)
    return quota


@pytest.fixture
def api_client(db_session: Session) -> Iterator[TestClient]:
    def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_completed_lookup_replays_saved_result_and_rejects_equal_cost_input_change(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    lookup = consumer_module.perform_schedule_lookup

    def counted_lookup(request):
        nonlocal calls
        calls += 1
        return lookup(request)

    monkeypatch.setattr(consumer_module, "perform_schedule_lookup", counted_lookup)
    path = f"/orgs/{schedule_quota.org_id}/schedule-searches"
    usage_arguments = dict(org_id=schedule_quota.org_id, feature=schedule_quota.feature)
    headers = {"Idempotency-Key": str(uuid4())}
    payload = {"routes": ["SGSIN-NLRTM", "SGSIN-GBFXT"]}
    first = api_client.post(path, headers=headers, json=payload)
    assert first.status_code == 200, first.text
    # Discarding the first response simulates loss after a committed result.
    replay = api_client.post(path, headers=headers, json=payload)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert calls == 1
    assert first.json()["status"] == "DONE"
    changed = api_client.post(
        path, headers=headers, json={"routes": ["SGSIN-USLAX", "SGSIN-GBFXT"]}
    )
    assert changed.status_code == 409
    assert calls == 1
    usage = get_quota_usage(db_session, **usage_arguments)
    assert usage.monthly.used == 2
    assert usage.monthly.reserved == 0
    assert usage.monthly.available == 8


def test_completed_replay_keeps_original_charge_after_feature_repricing(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = f"/orgs/{schedule_quota.org_id}/schedule-searches"
    arguments = dict(org_id=schedule_quota.org_id, feature=schedule_quota.feature)
    headers = {"Idempotency-Key": str(uuid4())}
    payload = {"routes": ["SGSIN-NLRTM", "SGSIN-GBFXT"]}
    calls = 0
    lookup = consumer_module.perform_schedule_lookup

    def counted_lookup(request):
        nonlocal calls
        calls += 1
        return lookup(request)

    monkeypatch.setattr(consumer_module, "perform_schedule_lookup", counted_lookup)
    first = api_client.post(path, headers=headers, json=payload)
    assert first.status_code == 200, first.text

    with db_session.begin():
        db_session.execute(
            update(APIQuotaMap)
            .where(APIQuotaMap.feature == APIFeature.SAILING_SCHEDULE)
            .values(unit_cost=3)
        )

    replay = api_client.post(path, headers=headers, json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert calls == 1
    changed = api_client.post(path, headers=headers, json={"routes": ["different-route"]})
    assert changed.status_code == 409
    assert calls == 1
    usage = get_quota_usage(db_session, **arguments)
    assert usage.monthly.used == 2
    assert usage.monthly.reserved == 0
    assert usage.monthly.available == 8

    # New work uses the current price; only recognized retries retain old pricing.
    fresh = api_client.post(
        path, headers={"Idempotency-Key": str(uuid4())}, json=payload
    )
    assert fresh.status_code == 200, fresh.text
    assert calls == 2
    usage = get_quota_usage(db_session, **arguments)
    assert usage.monthly.used == 8
    assert usage.monthly.reserved == 0
    assert usage.monthly.available == 2


@pytest.mark.parametrize("behavior, status_code", [("fail", 502), ("timeout", 504)])
def test_confirmed_feature_failure_releases_capacity_and_does_not_restart_on_replay(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    behavior: str,
    status_code: int,
) -> None:
    path = f"/orgs/{schedule_quota.org_id}/schedule-searches"
    headers = {"Idempotency-Key": str(uuid4())}
    payload = {"routes": ["SGSIN-NLRTM"], "demo_behavior": behavior}
    assert api_client.post(path, headers=headers, json=payload).status_code == status_code
    assert api_client.post(path, headers=headers, json=payload).status_code == 409
    usage = get_quota_usage(
        db_session, org_id=schedule_quota.org_id, feature=schedule_quota.feature
    )
    assert usage.monthly.used == usage.monthly.reserved == 0
    assert usage.monthly.available == 10
    operations = db_session.scalars(
        select(QuotaUsagePerRequest).where(
            QuotaUsagePerRequest.org_id == schedule_quota.org_id,
            QuotaUsagePerRequest.feature == schedule_quota.feature,
        )
    ).all()
    assert len(operations) == 1
    assert operations[0].status == FeatureQuotaStatus.RELEASED
    assert operations[0].request_payload is not None
    assert operations[0].result_payload is None


def test_rejected_and_invalid_requests_do_not_execute_feature(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_lookup(request):
        raise AssertionError("Rejected request must not execute the feature")

    monkeypatch.setattr(consumer_module, "perform_schedule_lookup", unexpected_lookup)
    path = f"/orgs/{schedule_quota.org_id}/schedule-searches"
    usage_arguments = dict(org_id=schedule_quota.org_id, feature=schedule_quota.feature)
    headers = {"Idempotency-Key": str(uuid4())}
    assert api_client.post(path, headers=headers, json={"routes": ["route"] * 11}).status_code == 429
    assert api_client.post(path, headers=headers, json={"routes": []}).status_code == 422
    assert api_client.post(path, json={"routes": ["route"]}).status_code == 422
    usage = get_quota_usage(db_session, **usage_arguments)
    assert usage.monthly.available == 10
    db_session.commit()

    def unavailable_admission(*args, **kwargs):
        raise OperationalError("admission", {}, RuntimeError("simulated connection loss"))

    monkeypatch.setattr(consumer_module, "reserve_quota", unavailable_admission)
    assert api_client.post(path, headers=headers, json={"routes": ["route"]}).status_code == 503


def test_uncertain_final_accounting_does_not_refund_successful_work(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = dict(org_id=schedule_quota.org_id, feature=schedule_quota.feature)

    def unavailable_finalization(*args, **kwargs):
        raise OperationalError("finalization", {}, RuntimeError("simulated connection loss"))

    monkeypatch.setattr(consumer_module, "finalize_reservation", unavailable_finalization)
    response = api_client.post(
        f"/orgs/{schedule_quota.org_id}/schedule-searches",
        headers={"Idempotency-Key": str(uuid4())},
        json={"routes": ["SGSIN-NLRTM"]},
    )
    assert response.status_code == 503
    usage = get_quota_usage(db_session, **arguments)
    assert usage.monthly.used == 0
    assert usage.monthly.reserved == 1
    assert usage.monthly.available == 9
