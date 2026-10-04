from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..shared.database import get_session, lifespan
from ..shared.quota import (
    IdempotencyConflict,
    InsufficientQuota,
    OperationConflict,
    QuotaNotConfigured,
)
from ..shared.schemas import HealthResponse
from .schemas import ScheduleSearchRequest, ScheduleSearchResponse
from .service import DemoFeatureFailure, DemoFeatureTimeout, search_schedules


app = FastAPI(title="Portcast schedule-search consumer", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health(session: Annotated[Session, Depends(get_session)]) -> HealthResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return HealthResponse(status="ok")


@app.post("/orgs/{org_id}/schedule-searches", response_model=ScheduleSearchResponse)
def schedule_search(
    org_id: int,
    request: ScheduleSearchRequest,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
    session: Annotated[Session, Depends(get_session)],
) -> ScheduleSearchResponse:
    # Demo interface: production integration must authenticate and scope org access.
    try:
        return search_schedules(
            session, org_id=org_id, idempotency_key=idempotency_key, request=request
        )
    except QuotaNotConfigured as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InsufficientQuota as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except (IdempotencyConflict, OperationConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DemoFeatureTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except DemoFeatureFailure as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
