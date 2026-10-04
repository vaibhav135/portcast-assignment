from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .config import get_database_config


def create_database_engine() -> Engine:
    config = get_database_config()
    return create_engine(
        config.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Each application owns its engine/pool, independent of other applications."""
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        app.state.session_factory = sessionmaker(bind=engine)
        yield
    finally:
        engine.dispose()


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        # Callers own transaction boundaries; never auto-commit on request exit.
        yield session
