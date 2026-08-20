"""What a block is: what it declares about itself, and what is actually on disk."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .enums import HeightType, SensorId, ValidationSeverity


class VerticalReference(BaseModel):
    """The vertical reference, stated rather than inferred.

    `UNKNOWN` is a legal value and an honest one. It does not stop a run, but it marks the
    run as unsuitable for absolute-Z QA, which is the whole reason the field exists: the
    alternative is a Z comparison against a datum nobody recorded.

    `geoid_applied` records whether a geoid model has ALREADY been applied to the delivered
    heights. Deliveries exist where the geoid was applied in the field, before processing,
    and reapplying it would introduce a tens-of-metres error. That fact lives in a document,
    not in the file, so it has to be carried here.
    """

    height_type: HeightType = HeightType.UNKNOWN
    epsg: int | None = None
    geoid_model: str | None = None
    geoid_applied: bool | None = None
    datum_name: str | None = None


class BlockConfig(BaseModel):
    project_id: str
    block_id: str
    sensor: SensorId | None = None
    lens: str | None = None
    crs: str | None = None
    vertical: VerticalReference = Field(default_factory=VerticalReference)
    notes: str = ""

    @property
    def master_crs(self) -> str | None:
        """The CRS to write on the master, compound when a vertical code is declared.

        Built from the two declared codes rather than stored separately, so the horizontal
        code cannot drift apart from the one used for the compound string.
        """
        if self.crs is None:
            return None
        if self.vertical.epsg is None:
            return self.crs
        return f"{self.crs}+{self.vertical.epsg}"


def load_block_config(path: Path) -> BlockConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"block config {path} must contain a YAML mapping")
    return BlockConfig.model_validate(raw)


class Block(BaseModel):
    """What was found on disk for one block. A pure inventory — it judges nothing."""

    block_id: str
    root: Path
    layout: str
    config: BlockConfig | None = None

    images: list[Path] = Field(default_factory=list)
    navigation: list[Path] = Field(default_factory=list)
    control: list[Path] = Field(default_factory=list)
    checkpoints: list[Path] = Field(default_factory=list)
    reference: list[Path] = Field(default_factory=list)

    @property
    def image_count(self) -> int:
        return len(self.images)


class ValidationFinding(BaseModel):
    """One expectation about a block, and what was actually found."""

    name: str
    severity: ValidationSeverity
    detail: str = ""

    @property
    def is_fatal(self) -> bool:
        return self.severity is ValidationSeverity.MISSING_FATAL


class ValidatedBlock(BaseModel):
    """A block that has been through validation.

    Deliberately a distinct type from `Block`. A function that requires validated input
    should be unable to accept unvalidated input by construction rather than by convention.
    """

    block: Block
    findings: list[ValidationFinding] = Field(default_factory=list)

    # Recorded rather than inferred later: a block whose vertical reference is undeclared can
    # still be processed, but nothing downstream may claim an absolute-Z result for it.
    suitable_for_absolute_z: bool = False

    @property
    def fatal(self) -> list[ValidationFinding]:
        return [finding for finding in self.findings if finding.is_fatal]

    @property
    def is_processable(self) -> bool:
        return not self.fatal

    def of_severity(self, severity: ValidationSeverity) -> list[ValidationFinding]:
        return [finding for finding in self.findings if finding.severity is severity]
