from datetime import datetime

from pydantic import BaseModel, Field

from .models import APIFeature


class MonthlyUsage(BaseModel):
    limit: int = Field(ge=0)
    used: int = Field(ge=0)
    reserved: int = Field(ge=0)
    available: int = Field(ge=0)


class CreditUsage(BaseModel):
    reserved: int = Field(ge=0)
    available: int = Field(ge=0)
    expires_on: datetime | None


class QuotaUsageResponse(BaseModel):
    org_id: int
    feature: APIFeature
    period_start: datetime
    next_reset: datetime
    monthly: MonthlyUsage
    credits: CreditUsage
