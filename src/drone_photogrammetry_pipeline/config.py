"""Application settings, read once from the environment or a .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DPP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    workspace_root: Path = Path("workspace")

    # Where the deliveries to process are kept. A project takes its sources from
    # `<inputs_root>/<project slug>` unless its own configuration or `--source-root` says
    # otherwise, so a fresh clone needs one path set rather than one per project.
    inputs_root: Path = Path("inputs")

    profiles_dir: Path = Path("profiles")
    nodeodm_url: str = "http://localhost:3000"
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
