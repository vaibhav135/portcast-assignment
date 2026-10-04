from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from .config import get_database_config


def create_database_engine() -> Engine:
    config = get_database_config()
    return create_engine(
        config.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5},
    )


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        # Callers own transaction boundaries; never auto-commit on request exit.
        yield session
