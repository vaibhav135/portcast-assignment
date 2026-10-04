from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.database import create_database_engine
from src.models import APIQuotaMap, APIFeature, FeatureQuotaMonthly, Organization


@pytest.fixture
def db_session() -> Iterator[Session]:
    """Use real Postgres, but roll back all fixture/application rows after each test."""
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(
                    bind=connection,
                    join_transaction_mode="create_savepoint",
                    expire_on_commit=False,
                ) as session:
                    yield session
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.fixture
def monthly_quota(db_session: Session) -> FeatureQuotaMonthly:
    feature = APIFeature.CONTAINER_TRACKING
    configuration = db_session.scalar(
        select(APIQuotaMap).where(APIQuotaMap.feature == feature)
    )
    if configuration is None:
        db_session.add(APIQuotaMap(feature=feature, unit_cost=1, lease_duration_sec=30))

    organization = Organization(name=f"reservation-test-{uuid4()}")
    db_session.add(organization)
    db_session.flush()

    now = datetime.now(timezone.utc)
    next_month = (now.replace(day=1) + timedelta(days=32)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    quota = FeatureQuotaMonthly(
        org_id=organization.id,
        feature=feature,
        total_allocated=10,
        units_consumed=0,
        units_remaining=10,
        resets_on=next_month,
    )
    db_session.add(quota)
    db_session.commit()
    return quota
