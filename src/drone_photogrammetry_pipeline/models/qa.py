"""QA result contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .enums import CheckOutcome, GateStatus


class RasterCheck(BaseModel):
    """One contract clause, its verdict, and the evidence for that verdict."""

    name: str
    clause: str
    outcome: CheckOutcome
    expected: Any = None
    observed: Any = None
    message: str = ""


class RasterQAResult(BaseModel):
    status: GateStatus
    checks: dict[str, Any] = Field(default_factory=dict)
    details: list[RasterCheck] = Field(default_factory=list)

    @property
    def failures(self) -> list[RasterCheck]:
        return [check for check in self.details if check.outcome is CheckOutcome.FAIL]

    @property
    def reviews(self) -> list[RasterCheck]:
        return [check for check in self.details if check.outcome is CheckOutcome.REVIEW]


class BandDifference(BaseModel):
    """How two blocks disagree on one band over the ground they share."""

    band: str
    median_a: float
    median_b: float
    median_difference: float

    # Symmetric relative difference between the two medians, in percent. Symmetric so that
    # swapping the two blocks only changes the sign, never the magnitude.
    relative_difference_pct: float

    # Median of the per-sample symmetric difference. Robust to the outliers that a mean
    # would follow, which matters when a few samples fall on water, shadow or vehicles.
    robust_normalized_difference_pct: float


class RadiometricPairResult(BaseModel):
    block_a: str
    block_b: str
    overlap_area_ha: float
    sample_count: int
    sample_pixels: int
    patch_metres: float
    bands: list[BandDifference] = Field(default_factory=list)

    # No threshold is frozen yet, so no pair is judged. Establishing one is a benchmark
    # exercise, not a guess: see docs/radiometry.md.
    status: GateStatus = GateStatus.NOT_EVALUATED
    note: str = ""

    @property
    def max_abs_relative_difference_pct(self) -> float:
        return max((abs(b.relative_difference_pct) for b in self.bands), default=0.0)

    @property
    def mean_abs_relative_difference_pct(self) -> float:
        if not self.bands:
            return 0.0
        return sum(abs(b.relative_difference_pct) for b in self.bands) / len(self.bands)


class RadiometricOverlapReport(BaseModel):
    project_id: str
    generated_at: datetime
    pair_count: int
    measured_count: int
    pairs: list[RadiometricPairResult] = Field(default_factory=list)

    @property
    def measured(self) -> list[RadiometricPairResult]:
        return [p for p in self.pairs if p.sample_pixels > 0]
