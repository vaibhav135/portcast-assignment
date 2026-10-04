"""Explicit additive development migration; existing operation rows remain valid."""

from sqlalchemy import text

from .database import create_database_engine


def main() -> None:
    engine = create_database_engine()
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE quota_usage_per_request "
                    "ADD COLUMN IF NOT EXISTS request_payload JSONB, "
                    "ADD COLUMN IF NOT EXISTS result_payload JSONB"
                )
            )
        print("Operation input/result columns are ready; existing rows preserved.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
