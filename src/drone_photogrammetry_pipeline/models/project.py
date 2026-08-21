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

    # Optional, and normally absent. A delivery sits in a different place on every machine, so
    # committing a path here would mean every reader editing a tracked file and every pull
    # carrying someone else's drive letter. Left unset, the sources are found under
    # `DPP_INPUTS_ROOT`; see `resolve_source_root`.
    source_root: Path | None = None

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

    # Ground sample distance of the browsable overview, in metres. Left unset it is chosen from
    # the survey's extent, which is what a new area wants: one fixed number cannot serve both a
    # 6,285 ha survey and a 350 ha one.
    overview_gsd: float | None = None
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
        if self.overview_gsd is not None and self.overview_gsd <= 0:
            raise ValueError("overview_gsd must be positive, or absent to choose it from extent")
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
    if config.source_root is not None and not config.source_root.is_absolute():
        config = config.model_copy(
            update={"source_root": (path.parent / config.source_root).resolve()}
        )
    return config


def resolve_source_root(
    config: ProjectConfig,
    *,
    inputs_root: Path,
    slug: str,
    override: Path | None = None,
) -> Path:
    """Where this project's delivered blocks are, from the first source that names one.

    Three ways to say it, in this order: `--source-root` on the command line, `source_root` in
    the configuration, and the convention `<inputs_root>/<slug>`. The convention exists so that
    a clone works with one setting in `.env` rather than an edit to every tracked project file;
    the other two exist because a delivery that is already on disk somewhere should not have to
    be moved or copied to be processed.

    Reports every candidate it tried when none of them is a directory, because "no such
    directory" without saying which three were considered is the least useful thing a tool can
    say at this point.
    """

    # Resolved before use, not only before display: `DPP_INPUTS_ROOT` defaults to a relative
    # `inputs`, and reporting "tried inputs/sant" tells a reader nothing about where to put
    # the delivery.
    def absolute(path: Path) -> Path:
        return path.expanduser().resolve()

    candidates: list[tuple[str, Path]] = []
    if override is not None:
        candidates.append(("--source-root", absolute(override)))
    if config.source_root is not None:
        candidates.append(
            (
                f"source_root in the configuration for {config.project_id}",
                absolute(config.source_root),
            )
        )
    candidates.append((f"DPP_INPUTS_ROOT/{slug}", absolute(inputs_root / slug)))

    for _, candidate in candidates:
        if candidate.is_dir():
            return candidate

    tried = "\n".join(f"  {origin}: {candidate}" for origin, candidate in candidates)
    raise ProjectConfigError(
        f"no source directory for {config.project_id}. Tried, in order:\n{tried}\n"
        "Put the delivery's block directories under the last one, or pass --source-root"
    )
