"""The block.yaml contract.

The filesystem inventory model and the scanner that builds it arrive in Phase 3 with
`ingest/`. This module defines only what a block declares about itself, which packaging
already needs in order to label a product.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .enums import HeightType, SensorId


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
