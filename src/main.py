from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .database import create_database_engine, get_session
from .models import APIFeature
from .quota import QuotaNotConfigured, get_quota_usage
from .schemas import QuotaUsageResponse


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        app.state.session_factory = sessionmaker(bind=engine)
        yield
    finally:
        engine.dispose()


app = FastAPI(title="Portcast quota demo", lifespan=lifespan)


class HealthResponse(BaseModel):
    status: str


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
