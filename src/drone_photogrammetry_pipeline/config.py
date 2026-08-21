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

    # How many processes ODM may use. A property of this machine rather than of a delivery,
    # which is why it is a setting and not a profile field: a versioned profile describes the
    # product, and baking one workstation's memory into it would travel to every other.
    #
    # Unset means ODM's default, its CPU count. That is too many for large imagery: ODM's own
    # guidance is about 1 GB per thread per 2 megapixels, so 44.7 MP DJI P1 frames want roughly
    # 22 GB each. Measured here, 32 threads on 79 P1 images survived with 15.4 GB free and was
    # killed by the out-of-memory killer during undistortion with 11.5 GB free.
    odm_max_concurrency: int | None = None

    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
