from .database import create_database_engine
from .models import Base


def main() -> None:
    engine = create_database_engine()
    try:
        Base.metadata.create_all(engine)
        print("Development database tables created (existing tables left unchanged).")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
