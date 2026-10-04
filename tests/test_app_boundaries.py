from src.consumer.main import app as consumer_app
from src.server.main import app as server_app


def test_server_exposes_reporting_routes_only() -> None:
    paths = {route.path for route in server_app.routes}

    assert "/health" in paths
    assert "/orgs/{org_id}/features/{feature}/usage" in paths
    assert "/orgs/{org_id}/schedule-searches" not in paths


def test_consumer_exposes_schedule_routes_only() -> None:
    paths = {route.path for route in consumer_app.routes}

    assert "/health" in paths
    assert "/orgs/{org_id}/schedule-searches" in paths
    assert "/orgs/{org_id}/features/{feature}/usage" not in paths
