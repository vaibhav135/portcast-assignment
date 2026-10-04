"""Provision one demo organization; reruns do not refill its allowance."""

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..shared.database import create_database_engine
from ..shared.models import APIQuotaMap, APIFeature, FeatureQuotaMonthly, Organization


def main() -> None:
    engine = create_database_engine()
    try:
        with Session(engine) as session, session.begin():
            organization = session.scalar(
                select(Organization).where(Organization.name == "demo-org")
            )
            if organization is None:
                organization = Organization(name="demo-org")
                session.add(organization)
                session.flush()
            feature = APIFeature.SAILING_SCHEDULE
            if session.scalar(select(APIQuotaMap).where(APIQuotaMap.feature == feature)) is None:
                session.add(APIQuotaMap(feature=feature, unit_cost=1, lease_duration_sec=30))
            if session.scalar(
                select(FeatureQuotaMonthly).where(
                    FeatureQuotaMonthly.org_id == organization.id,
                    FeatureQuotaMonthly.feature == feature,
                )
            ) is None:
                now = session.scalar(select(func.clock_timestamp()))
                month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                session.add(
                    FeatureQuotaMonthly(
                        org_id=organization.id,
                        feature=feature,
                        total_allocated=500,
                        units_consumed=0,
                        units_remaining=500,
                        resets_on=month_start + relativedelta(months=1),
                    )
                )
            org_id = organization.id
        print(f"Demo organization ID: {org_id} (existing balances preserved)")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
