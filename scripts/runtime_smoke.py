"""Verify a running Compose runtime: uv run python -m scripts.runtime_smoke --allow-demo-writes."""

import argparse
import json
import time
from datetime import timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from src.consumer.schemas import ScheduleSearchRequest
from src.shared.database import create_database_engine
from src.shared.models import (
    APIQuotaMap, APIFeature, FeatureQuotaMonthly, FeatureQuotaStatus,
    Organization, QuotaUsagePerRequest,
)
from src.shared.quota import reserve_quota


def http(url, *, payload=None, key=None, expected=200, timeout=5):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Idempotency-Key"] = str(key)
    request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                      headers=headers)
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        assert response.code == expected, f"Expected HTTP {expected}, got {response.code}"
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default="http://127.0.0.1:8000")
    parser.add_argument("--consumer-url", default="http://127.0.0.1:8001")
    parser.add_argument("--allow-demo-writes", action="store_true",
                        help="Allow isolated fixture writes to the DB configured by env/root .env")
    args = parser.parse_args()
    if not args.allow_demo_writes:
        parser.error("--allow-demo-writes is required; confirm DB env/root .env targets the demo runtime")

    engine = create_database_engine()
    engine.hide_parameters = True
    org_id = None
    feature = APIFeature.SAILING_SCHEDULE
    try:
        for base in (args.server_url, args.consumer_url):
            assert http(f"{base.rstrip('/')}/health")["status"] == "ok"
        print("OK: server and consumer health")
        with engine.begin() as connection:
            cost = connection.scalar(select(APIQuotaMap.unit_cost).where(APIQuotaMap.feature == feature))
            assert cost is not None and cost > 0, "Shared sailing-schedule feature must be configured"
            fixture_id = connection.scalar(insert(Organization).values(
                name=f"runtime-smoke-{uuid4()}").returning(Organization.id))
            now = connection.scalar(select(func.clock_timestamp()))
            next_month = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                          + relativedelta(months=1))
            connection.execute(insert(FeatureQuotaMonthly).values(
                org_id=fixture_id, feature=feature, total_allocated=10 * cost,
                units_consumed=0, units_remaining=10 * cost, resets_on=next_month))
        org_id = fixture_id
        usage_url = f"{args.server_url.rstrip('/')}/orgs/{org_id}/features/{feature.value}/usage"
        search_url = f"{args.consumer_url.rstrip('/')}/orgs/{org_id}/schedule-searches"
        payload = ScheduleSearchRequest(routes=[" SGSIN-NLRTM "]).model_dump(mode="json")
        key = uuid4()
        result = http(search_url, payload=payload, key=key)
        assert result["status"] == "DONE"
        assert http(search_url, payload=payload, key=key) == result, "Replay result changed"
        changed = ScheduleSearchRequest(routes=["SGSIN-USLAX"]).model_dump(mode="json")
        http(search_url, payload=changed, key=key, expected=409)
        usage = http(usage_url)["monthly"]
        assert (usage["used"], usage["reserved"], usage["available"]) == (cost, 0, 9 * cost)
        print("OK: request, identical replay, same-cost input conflict, and usage")

        # Simulate abandonment AFTER a committed hold; no process is killed here.
        with Session(engine) as session:
            abandoned = reserve_quota(session, org_id=org_id, feature=feature, units=cost,
                                      idempotency_key=uuid4(), request_payload=payload)
        assert abandoned.status == FeatureQuotaStatus.RESERVED and abandoned.claim_version == 0
        allocation = (abandoned.used_from_monthly, abandoned.used_from_extra)
        assert allocation == (cost, 0)
        with engine.begin() as connection:
            connection.execute(update(QuotaUsagePerRequest).where(
                QuotaUsagePerRequest.id == abandoned.id,
                QuotaUsagePerRequest.org_id == org_id,
            ).values(lease_expires_at=func.clock_timestamp() - timedelta(minutes=1)))

        deadline = time.monotonic() + 30
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "Recovery did not finish within 30 seconds"
            usage = http(usage_url, timeout=min(5, remaining))["monthly"]
            with Session(engine) as session, session.begin():
                recovered = session.scalar(select(QuotaUsagePerRequest).where(
                    QuotaUsagePerRequest.id == abandoned.id,
                    QuotaUsagePerRequest.org_id == org_id))
                assert recovered is not None, "Owned operation disappeared"
                if recovered.status == FeatureQuotaStatus.DONE:
                    assert recovered.claim_version == 1, "Expected exactly one recovery claim"
                    assert recovered.reserved_at == abandoned.reserved_at
                    assert (recovered.used_from_monthly, recovered.used_from_extra) == allocation
                    assert recovered.result_payload is not None, "Recovery result missing"
                    if (usage["used"], usage["reserved"], usage["available"]) == (2 * cost, 0, 8 * cost):
                        break
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        print("OK: abandoned committed hold recovered once; allocation and usage preserved")
    finally:
        try:
            if org_id is not None:
                with engine.begin() as connection:
                    for model in (QuotaUsagePerRequest, FeatureQuotaMonthly):
                        connection.execute(delete(model).where(model.org_id == org_id))
                    connection.execute(delete(Organization).where(Organization.id == org_id))
                print("OK: isolated runtime-smoke fixture cleaned up")
        finally:
            engine.dispose()


if __name__ == "__main__":
    main()
