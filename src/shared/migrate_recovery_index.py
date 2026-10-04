"""Explicit additive development migration for expired-reservation polling."""

from sqlalchemy import text

from .database import create_database_engine


def main() -> None:
    engine = create_database_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_operation_recovery "
                    "ON quota_usage_per_request (feature, lease_expires_at, id) "
                    "WHERE status = 'RESERVED'"
                )
            )
        print("Recovery polling index is ready; existing rows preserved.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
