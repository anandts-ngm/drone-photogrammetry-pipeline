"""A delivery's settings, written down once instead of retyped per command.

Two of these fields relocate every pixel or every elevation if they are wrong, and neither
can be inferred from the imagery:

* `declare_crs` adds a vertical component that the delivered files do not carry. Buduunkhad's
  is `EPSG:32647+5705`, documented only in `METADATA_Buduunkhad_XV-023222.txt`, which also
  records that the geoid was already applied in the field -- reapplying it costs about 48 m.
  Sant has no such document, so it must have no declaration at all.
* `height_type` says which surface those heights are above. Normal and orthometric heights are
  different surfaces, and the Baltic 1977 system these deliveries use is normal.

Putting them on a command line means retyping them for every stage and every rerun, and being
one flag away from a 48 m error with nothing to review. Putting them in a file makes them
reviewable, diffable, and wrong only once.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from .enums import HeightType, SensorId, SourceType


class ProjectConfigError(RuntimeError):
    pass


class ProjectConfig(BaseModel):
    """Everything about one delivery that the commands would otherwise ask for."""

    # Unknown keys are refused rather than ignored. This file is hand-edited, and a mistyped
    # `destripe_preview` that is quietly dropped reads as configured while doing nothing.
    model_config = ConfigDict(extra="forbid")

    project_id: str
    source_root: Path

    # Which external system produced the deliverables. Absent for a raw-imagery project, where
    # this pipeline is the producer rather than the consumer.
    source_type: SourceType | None = None

    # Filename of the orthophoto inside each block directory. Terra writes `dom.tif`; a P1
    # delivery names its files per block, so this is not universal.
    asset: str = "dom.tif"

    profile_id: str = "external_terra"

    # See the module docstring. Omit both unless a document states them.
    declare_crs: str | None = None
    height_type: HeightType = HeightType.UNKNOWN

    # An external product carries no sensor identification in the file, so a mixed delivery
    # must leave this unset rather than stamp one sensor across blocks that had two.
    sensor: SensorId | None = None

    allow_alpha_from_nodata: bool = False
    verify_pixels: bool = False

    # Ground sample distance of the browsable overview, in metres.
    overview_gsd: float = 0.5
    preview_longest_side: int = 2048
    destripe_previews: bool = True

    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> ProjectConfig:
        if not self.project_id.strip():
            raise ValueError("project_id must not be empty")
        # A vertical reference without a height type is a number whose meaning is unstated,
        # which is the ambiguity the declaration exists to remove.
        if self.declare_crs and "+" in self.declare_crs and self.height_type is HeightType.UNKNOWN:
            raise ValueError(
                f"{self.project_id}: declare_crs '{self.declare_crs}' adds a vertical component "
                "but height_type is UNKNOWN. State which surface the heights are above, or "
                "declare only the horizontal CRS"
            )
        if self.overview_gsd <= 0:
            raise ValueError("overview_gsd must be positive")
        return self


def load_project(path: Path) -> ProjectConfig:
    """Read a project configuration, reporting where a bad value came from."""
    if not path.is_file():
        raise ProjectConfigError(f"no project configuration at {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ProjectConfigError(f"{path} is not valid YAML: {error}") from error
    if not isinstance(document, dict):
        raise ProjectConfigError(f"{path} must contain a mapping, found {type(document).__name__}")

    try:
        config = ProjectConfig.model_validate(document)
    except ValueError as error:
        raise ProjectConfigError(f"{path}: {error}") from error

    # Resolved relative to the configuration file, so a config and its delivery can be moved
    # together and a relative path keeps meaning what it said.
    if not config.source_root.is_absolute():
        config = config.model_copy(
            update={"source_root": (path.parent / config.source_root).resolve()}
        )
    return config
