"""The run manifest: the record that makes a product reproducible.

Every field is either measured or explicitly absent. A value that could not be determined is
`None` and stays `None`; it is never filled with a plausible default, because a manifest
whose numbers might be guesses is not evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .enums import (
    AlphaProvenance,
    GateStatus,
    HeightType,
    ProcessingEngine,
    SensorId,
    SourceType,
    WorkflowStatus,
)
from .qa import RasterQAResult

MANIFEST_SCHEMA_VERSION = 1


class PackagingOperation(BaseModel):
    """An operation that changed the file. Recorded so nothing happens silently."""

    name: str
    detail: str = ""


class GridRecord(BaseModel):
    """The raster grid, captured before and after packaging so equality can be asserted."""

    width: int
    height: int
    transform: list[float]
    crs: str | None


class PackagingRecord(BaseModel):
    backend: str
    gdal_version: str
    creation_options: dict[str, Any]
    source_path: str
    source_sha256: str
    alpha_provenance: AlphaProvenance
    operations: list[PackagingOperation] = Field(default_factory=list)
    grid_in: GridRecord
    grid_out: GridRecord
    grid_preserved: bool
    pixels_verified: bool = False


class RunManifest(BaseModel):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    run_id: str

    project_id: str = ""
    block_id: str = ""
    sensor: SensorId | None = None
    lens: str | None = None

    source_type: SourceType | None = None
    processing_engine: ProcessingEngine | None = None

    profile_id: str = ""
    profile_version: int | None = None
    profile_hash: str = ""

    image_count: int = 0
    input_manifest_hash: str = ""

    nodeodm_task_id: str = ""
    nodeodm_version: str = ""
    odm_version: str = ""

    started_at: datetime | None = None
    finished_at: datetime | None = None

    processing_status: WorkflowStatus = WorkflowStatus.VALIDATION_PENDING

    crs: str | None = None
    height_type: HeightType = HeightType.UNKNOWN

    # Copied from the profile so that the product's radiometric history is readable from the
    # manifest itself, not only recoverable by looking up the profile hash.
    radiometry_policy: str = ""
    radiometry_history: str = ""

    # Read back from the produced raster, never rounded, never normalised across blocks.
    pixel_size_x: float | None = None
    pixel_size_y: float | None = None

    packaging: PackagingRecord | None = None

    raster_qa: RasterQAResult | None = None
    checkpoint_rmse_xy: float | None = None
    checkpoint_rmse_z: float | None = None
    lidar_median_dz: float | None = None
    radiometric_overlap_score: float | None = None

    outputs: dict[str, str] = Field(default_factory=dict)
    output_hashes: dict[str, str] = Field(default_factory=dict)

    gate_status: GateStatus = GateStatus.NOT_EVALUATED


class BlockRunSummary(BaseModel):
    """One block's line in a project run. The manifest remains the authoritative record."""

    block_id: str
    gate_status: GateStatus
    reused: bool = False
    pixel_size_x: float | None = None
    source_bytes: int = 0
    master_bytes: int = 0
    seconds: float = 0.0
    manifest_path: str = ""
    master_path: str | None = None
    error: str | None = None

    @property
    def failed(self) -> bool:
        """Whether this block failed to produce an approved master.

        A block that stops before QA keeps `gate_status = NOT_EVALUATED`, which is the honest
        record — QA genuinely never ran — so failure cannot be read from the gate alone.
        """
        return self.error is not None or self.gate_status is GateStatus.FAIL


class ProjectRunSummary(BaseModel):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    project_id: str
    source_root: str
    started_at: datetime
    finished_at: datetime
    blocks: list[BlockRunSummary] = Field(default_factory=list)

    @property
    def failed(self) -> int:
        return sum(1 for block in self.blocks if block.failed)

    @property
    def passed(self) -> int:
        return sum(
            1 for block in self.blocks if not block.failed and block.gate_status is GateStatus.PASS
        )

    @property
    def review(self) -> int:
        return sum(
            1
            for block in self.blocks
            if not block.failed and block.gate_status is GateStatus.REVIEW
        )

    @property
    def source_bytes(self) -> int:
        return sum(block.source_bytes for block in self.blocks)

    @property
    def master_bytes(self) -> int:
        return sum(block.master_bytes for block in self.blocks)
