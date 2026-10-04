from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..shared.database import get_session, lifespan
from ..shared.models import APIFeature
from ..shared.quota import QuotaNotConfigured, get_quota_usage
from ..shared.schemas import HealthResponse, QuotaUsageResponse


app = FastAPI(title="Portcast quota reporting server", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
def health(session: Annotated[Session, Depends(get_session)]) -> HealthResponse:
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return HealthResponse(status="ok")


@app.get("/orgs/{org_id}/features/{feature}/usage", response_model=QuotaUsageResponse)
def quota_usage(
    org_id: int,
    feature: APIFeature,
    session: Annotated[Session, Depends(get_session)],
) -> QuotaUsageResponse:
    # Demo interface: production integration must authenticate and scope org access.
    try:
        return get_quota_usage(session, org_id=org_id, feature=feature)
    except QuotaNotConfigured as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
