"""Versioned processing profiles.

ODM option names and values live in profile YAML and nowhere else. The `odm` mappings below
are deliberately untyped passthroughs: this repository does not maintain a second copy of
ODM's option schema, because a stale copy would silently disagree with the engine that
actually runs. Options are validated against the running engine's `/options` endpoint in
Phase 4, which is the only source that cannot go stale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..integrity import canonical_json_sha256
from .enums import ProcessingEngine, SensorId


class ProcessingSpec(BaseModel):
    engine: ProcessingEngine
    odm: dict[str, Any] = Field(default_factory=dict)


class RadiometrySpec(BaseModel):
    policy: str
    provisional: bool = False

    # What is known to have happened to this product's radiometry before it reached us. For
    # an external delivery that history lives in a document or in somebody's memory, never in
    # the file, so it has to be written down here or it is lost.
    history: str = ""

    odm: dict[str, Any] = Field(default_factory=dict)


class OutputsSpec(BaseModel):
    orthophoto: bool = True
    dsm: bool = False
    point_cloud: bool = False


class QASpec(BaseModel):
    raster_contract: bool = True


class PackagingSpec(BaseModel):
    """How the packager turns whatever the engine produced into the master contract."""

    source_asset: str | None = None
    band_selection: list[int] | None = None

    # Deriving alpha from NoData=0 marks every legitimately black pixel invalid. It is never
    # done implicitly; a profile has to ask for it, and the product is then flagged REVIEW.
    allow_alpha_from_nodata: bool = False


class ProcessingProfile(BaseModel):
    profile_id: str
    profile_version: int
    sensor: SensorId | None = None
    lens: str | None = None
    processing: ProcessingSpec
    radiometry: RadiometrySpec
    outputs: OutputsSpec = Field(default_factory=OutputsSpec)
    qa: QASpec = Field(default_factory=QASpec)
    packaging: PackagingSpec = Field(default_factory=PackagingSpec)


class LoadedProfile(BaseModel):
    """A profile together with the hash of the document it came from."""

    profile: ProcessingProfile
    profile_hash: str
    path: Path


class ProfileNotFoundError(FileNotFoundError):
    pass


def load_profile_file(path: Path) -> LoadedProfile:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"profile {path} must contain a YAML mapping")
    return LoadedProfile(
        profile=ProcessingProfile.model_validate(raw),
        profile_hash=canonical_json_sha256(raw),
        path=path,
    )


def load_profile(profiles_dir: Path, profile_id: str) -> LoadedProfile:
    """Load a profile by id, matching on the declared `profile_id` rather than the filename.

    Matching on the declared id means renaming a file cannot silently change which profile
    a manifest refers to.
    """
    candidates = sorted(profiles_dir.glob("*.yaml")) + sorted(profiles_dir.glob("*.yml"))
    for candidate in candidates:
        loaded = load_profile_file(candidate)
        if loaded.profile.profile_id == profile_id:
            return loaded
    known = ", ".join(sorted(p.stem for p in candidates)) or "none"
    raise ProfileNotFoundError(
        f"no profile with profile_id '{profile_id}' in {profiles_dir} (files present: {known})"
    )
