"""Centralized Configuration Management for Location Intelligence engine.

Provides strongly-typed settings derived from environment variables with sensible
defaults for development, test, and production environments.
"""
import os
from pathlib import Path
from pydantic import BaseModel, Field


class Settings(BaseModel):
    dsn: str = Field(
        default_factory=lambda: os.getenv(
            "HOMEXO_DSN",
            os.getenv("HOMEXO_TEST_DSN", "postgresql://rera_user:rera_password@localhost:5432/homexo_location_test"),
        )
    )
    rera_db_url: str = Field(
        default_factory=lambda: os.getenv(
            "RERA_DB_URL", "postgresql://rera_user:rera_password@localhost:5432/rera_db"
        )
    )
    tile_server_url: str = Field(
        default_factory=lambda: os.getenv("HOMEXO_TILE_SERVER_URL", "http://localhost:8080")
    )
    map_cache_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("HOMEXO_MAP_CACHE_DIR", "/tmp/homexo_static_maps"))
    )
    default_model_version: str = "location-v1"


settings = Settings()
