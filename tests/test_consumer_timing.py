from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import src.consumer.main as consumer_api
import src.consumer.service as consumer_service
import src.consumer.timing as timing
from src.consumer.schemas import ScheduleSearchRequest
from src.shared.config import get_database_config
from src.shared.database import get_session
from src.shared.models import APIQuotaMap, APIFeature, FeatureQuotaMonthly, QuotaUsagePerRequest
from src.shared.quota import get_quota_usage, reserve_quota


@pytest.fixture(autouse=True)
def timing_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Never let these integration tests use the original demo database.
    if get_database_config().name != "portcast_test":
        pytest.skip("Consumer timing tests require DB_NAME=portcast_test")
    monkeypatch.setenv("QUOTA_BENCHMARK_TIMING", "1")


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

    consumer_api.app.dependency_overrides[get_session] = override_session
    try:
        with TestClient(consumer_api.app) as client:
            yield client
    finally:
        consumer_api.app.dependency_overrides.pop(get_session, None)


def post_search(client, quota, *, key=None, behavior="success"):
    return client.post(
        f"/orgs/{quota.org_id}/schedule-searches",
        headers={"Idempotency-Key": str(key or uuid4())},
        json={"routes": ["SGSIN-NLRTM"], "demo_behavior": behavior},
    )


def metrics(response) -> dict[str, tuple[float, str]]:
    parsed = {}
    for metric in response.headers["Server-Timing"].split(", "):
        name, duration, outcome = metric.split(";")
        assert name not in parsed
        assert duration.startswith("dur=")
        assert outcome.startswith('desc="') and outcome.endswith('"')
        milliseconds = float(duration.removeprefix("dur="))
        assert milliseconds >= 0
        parsed[name] = (milliseconds, outcome.removeprefix('desc="')[:-1])
    return parsed


def test_success_times_lookup_reserve_and_commits_but_excludes_feature(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = []
    ticks = iter([1.0, 1.012, 101.0, 101.007])

    def clock():
        events.append("clock")
        return next(ticks)

    def committed(session):
        events.append("commit")

    def traced(name, function):
        def call(*args, **kwargs):
            events.append(f"{name}_start")
            result = function(*args, **kwargs)
            events.append(f"{name}_end")
            return result
        return call

    monkeypatch.setattr(timing, "perf_counter", clock)
    monkeypatch.setattr(db_session, "scalar", traced("scalar", db_session.scalar))
    monkeypatch.setattr(db_session, "execute", traced("execute", db_session.execute))
    for name in ("_reserve_quota_in_transaction", "finalize_reservation", "perform_schedule_lookup"):
        monkeypatch.setattr(consumer_service, name, traced(name, getattr(consumer_service, name)))
    event.listen(db_session, "after_commit", committed)
    try:
        response = post_search(api_client, schedule_quota)
    finally:
        event.remove(db_session, "after_commit", committed)

    assert response.status_code == 200, response.text
    assert metrics(response) == {
        "quota_admission": (12.0, "ok"),
        "quota_finalize": (7.0, "ok"),
    }
    boundaries = [i for i, value in enumerate(events) if value == "clock"]
    assert len(boundaries) == 4
    admission = events[boundaries[0] + 1:boundaries[1]]
    before_reserve = admission[:admission.index("_reserve_quota_in_transaction_start")]
    assert before_reserve.count("scalar_start") == 1
    assert before_reserve.count("execute_start") == 1
    assert admission.count("commit") == 1
    assert admission.index("_reserve_quota_in_transaction_end") < admission.index("commit")
    assert admission[-1] == "commit"
    assert events[boundaries[1] + 1:boundaries[2]] == [
        "perform_schedule_lookup_start", "perform_schedule_lookup_end"
    ]
    finalization = events[boundaries[2] + 1:boundaries[3]]
    assert finalization[0] == "finalize_reservation_start"
    assert finalization.count("commit") == 1
    assert finalization[-1] == "finalize_reservation_end"


def test_replay_has_only_admission_and_does_not_inherit_previous_timings(
    schedule_quota: FeatureQuotaMonthly, api_client: TestClient
) -> None:
    key = uuid4()
    first = post_search(api_client, schedule_quota, key=key)
    replay = post_search(api_client, schedule_quota, key=key)
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert set(metrics(first)) == {"quota_admission", "quota_finalize"}
    assert set(metrics(replay)) == {"quota_admission"}


@pytest.mark.parametrize("behavior,status_code", [("fail", 502), ("timeout", 504)])
def test_confirmed_failure_keeps_release_header_on_http_exception(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    behavior: str,
    status_code: int,
) -> None:
    key = uuid4()
    response = post_search(api_client, schedule_quota, key=key, behavior=behavior)
    assert response.status_code == status_code, response.text
    result = metrics(response)
    assert set(result) == {"quota_admission", "quota_release"}
    assert all(outcome == "ok" for _, outcome in result.values())
    replay = post_search(api_client, schedule_quota, key=key, behavior=behavior)
    assert replay.status_code == 409
    assert set(metrics(replay)) == {"quota_admission"}
    usage = get_quota_usage(
        db_session, org_id=schedule_quota.org_id, feature=schedule_quota.feature
    )
    assert (usage.monthly.used, usage.monthly.reserved, usage.monthly.available) == (0, 0, 10)


@pytest.mark.parametrize(
    "stage,metric,behavior",
    [
        ("lookup", "quota_admission", "success"),
        ("_reserve_quota_in_transaction", "quota_admission", "success"),
        ("finalize_reservation", "quota_finalize", "success"),
        ("release_reservation", "quota_release", "fail"),
    ],
)
def test_database_failures_record_elapsed_time_and_distinct_outcome(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    metric: str,
    behavior: str,
) -> None:
    def unavailable(*args, **kwargs):
        raise OperationalError(stage, {}, RuntimeError("simulated connection loss"))

    ticks = iter([1.0, 1.010, 2.0, 2.025])
    monkeypatch.setattr(timing, "perf_counter", lambda: next(ticks))
    target = db_session if stage == "lookup" else consumer_service
    monkeypatch.setattr(target, "scalar" if stage == "lookup" else stage, unavailable)
    response = post_search(api_client, schedule_quota, behavior=behavior)
    assert response.status_code == 503, response.text
    result = metrics(response)
    assert result[metric] == (10.0 if metric == "quota_admission" else 25.0, "db_error")
    assert set(result) == ({metric} if metric == "quota_admission" else {"quota_admission", metric})


def test_lost_commit_acknowledgement_is_timed_as_database_error_and_replays(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalize = consumer_service.finalize_reservation

    def commit_then_fail(*args, **kwargs):
        finalize(*args, **kwargs)
        raise OperationalError("commit acknowledgement", {}, RuntimeError("simulated loss"))

    monkeypatch.setattr(consumer_service, "finalize_reservation", commit_then_fail)
    key = uuid4()
    response = post_search(api_client, schedule_quota, key=key)
    assert response.status_code == 503
    assert metrics(response)["quota_finalize"][1] == "db_error"
    replay = post_search(api_client, schedule_quota, key=key)
    assert replay.status_code == 200, replay.text
    assert set(metrics(replay)) == {"quota_admission"}
    usage = get_quota_usage(
        db_session, org_id=schedule_quota.org_id, feature=schedule_quota.feature
    )
    assert usage.monthly.used > 0
    assert usage.monthly.reserved == 0


def test_expired_claim_is_part_of_admission(
    db_session: Session,
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = uuid4()
    operation = reserve_quota(
        db_session,
        org_id=schedule_quota.org_id,
        feature=schedule_quota.feature,
        units=1,
        idempotency_key=key,
        request_payload=ScheduleSearchRequest(routes=["SGSIN-NLRTM"]).model_dump(mode="json"),
    )
    with db_session.begin():
        db_session.execute(
            update(QuotaUsagePerRequest)
            .where(QuotaUsagePerRequest.id == operation.id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
    elapsed = 0.0
    claim = consumer_service.claim_expired_reservations

    def measured_claim(*args, **kwargs):
        nonlocal elapsed
        assert not db_session.in_transaction()
        result = claim(*args, **kwargs)
        assert not db_session.in_transaction()
        elapsed += 0.020
        return result

    monkeypatch.setattr(timing, "perf_counter", lambda: elapsed)
    monkeypatch.setattr(consumer_service, "claim_expired_reservations", measured_claim)
    response = post_search(api_client, schedule_quota, key=key)
    assert response.status_code == 200, response.text
    assert metrics(response)["quota_admission"] == (20.0, "ok")
    assert response.json()["operation_id"] == operation.id


def test_concurrent_requests_have_isolated_collectors(
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = Barrier(2)
    session_lock = Lock()
    search = consumer_api.search_schedules

    def overlapping_requests(*args, **kwargs):
        # Both route collectors are live concurrently. Only database access is
        # serialized because the rollback fixture owns one connection/session.
        barrier.wait(timeout=10)
        with session_lock:
            return search(*args, **kwargs)

    monkeypatch.setattr(consumer_api, "search_schedules", overlapping_requests)
    with ThreadPoolExecutor(max_workers=2) as executor:
        success = executor.submit(post_search, api_client, schedule_quota)
        failure = executor.submit(post_search, api_client, schedule_quota, behavior="fail")
        success_response = success.result(timeout=20)
        failure_response = failure.result(timeout=20)
    assert success_response.status_code == 200, success_response.text
    assert failure_response.status_code == 502, failure_response.text
    assert set(metrics(success_response)) == {"quota_admission", "quota_finalize"}
    assert set(metrics(failure_response)) == {"quota_admission", "quota_release"}


@pytest.mark.parametrize("flag", [None, "0", "true"])
def test_disabled_flag_emits_no_headers_and_never_reads_clock(
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
) -> None:
    def unexpected_clock():
        raise AssertionError("Disabled quota timings must not read the clock")

    if flag is None:
        monkeypatch.delenv("QUOTA_BENCHMARK_TIMING")
    else:
        monkeypatch.setenv("QUOTA_BENCHMARK_TIMING", flag)
    monkeypatch.setattr(timing, "perf_counter", unexpected_clock)
    key = uuid4()
    for response in (
        post_search(api_client, schedule_quota, key=key),
        post_search(api_client, schedule_quota, key=key),
        post_search(api_client, schedule_quota, behavior="fail"),
    ):
        assert response.status_code in (200, 502), response.text
        assert "Server-Timing" not in response.headers


def test_disabling_after_enabled_request_clears_timing_state(
    schedule_quota: FeatureQuotaMonthly,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "Server-Timing" in post_search(api_client, schedule_quota).headers
    monkeypatch.delenv("QUOTA_BENCHMARK_TIMING")
    response = post_search(api_client, schedule_quota)
    assert response.status_code == 200, response.text
    assert "Server-Timing" not in response.headers
