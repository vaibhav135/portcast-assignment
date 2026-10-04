import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import URL


class DatabaseConfig(BaseModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    name: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretStr

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.username,
            password=self.password.get_secret_value(),
            host=self.host,
            port=self.port,
            database=self.name,
        )


def get_database_config() -> DatabaseConfig:
    # Environment values override the project's local .env configuration.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    return DatabaseConfig.model_validate(
        {
            "host": os.environ.get("DB_HOST"),
            "port": os.environ.get("DB_PORT"),
            "name": os.environ.get("DB_NAME"),
            "username": os.environ.get("DB_USERNAME"),
            "password": os.environ.get("DB_PASSWORD"),
        }
    )
