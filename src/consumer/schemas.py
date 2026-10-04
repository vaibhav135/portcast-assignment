from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..shared.models import FeatureQuotaStatus


class DemoBehavior(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    TIMEOUT = "timeout"


class ScheduleSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Every route lookup consumes one configured feature unit; a batch is atomic.
    routes: list[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    ] = Field(min_length=1, max_length=1000)
    demo_behavior: DemoBehavior = DemoBehavior.SUCCESS


class ScheduleResult(BaseModel):
    route: str
    sailings: list[str]


class ScheduleSearchResponse(BaseModel):
    operation_id: int
    status: FeatureQuotaStatus
    results: list[ScheduleResult]
