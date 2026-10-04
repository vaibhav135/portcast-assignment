from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class APIFeature(StrEnum):
    CONTAINER_TRACKING = "container-tracking"
    SAILING_SCHEDULE = "sailing-schedule"


class FeatureQuotaStatus(StrEnum):
    RESERVED = "RESERVED"
    DONE = "DONE"
    RELEASED = "RELEASED"


feature_type = Enum(
    APIFeature,
    name="api_feature",
    values_callable=lambda members: [member.value for member in members],
    validate_strings=True,
)
status_type = Enum(FeatureQuotaStatus, name="feature_quota_status", validate_strings=True)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "org"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)


class APIQuotaMap(Base):
    __tablename__ = "api_quota_map"
    __table_args__ = (
        CheckConstraint("lease_duration_sec > 0", name="ck_feature_positive_lease"),
        CheckConstraint("unit_cost > 0", name="ck_feature_positive_cost"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    feature: Mapped[APIFeature] = mapped_column(feature_type, unique=True)
    lease_duration_sec: Mapped[int] = mapped_column(Integer)
    # Unit cost is multiplied by request quantity; it is not always a per-call cost.
    unit_cost: Mapped[int] = mapped_column(BigInteger)


class QuotaUsagePerRequest(Base):
    __tablename__ = "quota_usage_per_request"
    __table_args__ = (
        UniqueConstraint("org_id", "feature", "idempotency_key", name="uq_operation_key"),
        CheckConstraint("used_from_monthly >= 0", name="ck_operation_monthly_nonnegative"),
        CheckConstraint("used_from_extra >= 0", name="ck_operation_extra_nonnegative"),
        CheckConstraint(
            "used_from_monthly + used_from_extra > 0", name="ck_operation_positive_units"
        ),
        CheckConstraint("claim_version >= 0", name="ck_operation_claim_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("org.id"))
    feature: Mapped[APIFeature] = mapped_column(feature_type)
    status: Mapped[FeatureQuotaStatus] = mapped_column(status_type)
    used_from_monthly: Mapped[int] = mapped_column(BigInteger)
    used_from_extra: Mapped[int] = mapped_column(BigInteger)
    idempotency_key: Mapped[UUID] = mapped_column(Uuid)
    # Set once on admission; takeover must only update the lease and claim version.
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claim_version: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class FeatureQuotaExtra(Base):
    __tablename__ = "feature_quota_extra"
    __table_args__ = (
        UniqueConstraint("org_id", "feature", name="uq_extra_org_feature"),
        CheckConstraint("total_allocated >= 0", name="ck_extra_allocated_nonnegative"),
        CheckConstraint("units_consumed >= 0", name="ck_extra_consumed_nonnegative"),
        CheckConstraint("units_remaining >= 0", name="ck_extra_remaining_nonnegative"),
        CheckConstraint(
            "total_allocated = units_consumed + units_remaining", name="ck_extra_balance"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("org.id"))
    feature: Mapped[APIFeature] = mapped_column(feature_type)
    total_allocated: Mapped[int] = mapped_column(BigInteger)
    # Includes held reservations as well as finalized consumption.
    units_consumed: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    units_remaining: Mapped[int] = mapped_column(BigInteger)
    expires_on: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_added: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FeatureQuotaMonthly(Base):
    __tablename__ = "feature_quota_monthly"
    __table_args__ = (
        UniqueConstraint("org_id", "feature", name="uq_monthly_org_feature"),
        CheckConstraint("total_allocated >= 0", name="ck_monthly_allocated_nonnegative"),
        CheckConstraint("units_consumed >= 0", name="ck_monthly_consumed_nonnegative"),
        CheckConstraint("units_remaining >= 0", name="ck_monthly_remaining_nonnegative"),
        CheckConstraint(
            "total_allocated = units_consumed + units_remaining", name="ck_monthly_balance"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("org.id"))
    feature: Mapped[APIFeature] = mapped_column(feature_type)
    total_allocated: Mapped[int] = mapped_column(BigInteger)
    # Includes held reservations as well as finalized consumption.
    units_consumed: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    units_remaining: Mapped[int] = mapped_column(BigInteger)
    resets_on: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_renewed: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
