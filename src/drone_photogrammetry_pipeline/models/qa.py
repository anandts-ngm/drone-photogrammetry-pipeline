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

    # Matched quantiles of the two blocks over the ground they share, at the levels named by
    # the report's `qq_levels`. Quantiles rather than paired pixels, for the same reason the
    # rest of this module compares distributions: the two blocks are not co-registered to the
    # pixel, so pairing individual pixels would mix a geometric signal into a radiometric
    # measurement.
    #
    # Stored rather than summarised so that a reader can re-derive the fit and check it. A
    # slope alone cannot be argued with.
    qq_a: list[float] = Field(default_factory=list)
    qq_b: list[float] = Field(default_factory=list)

    @property
    def qq_fit(self) -> tuple[float, float] | None:
        """Least-squares line mapping this block pair's quantiles: b ~ slope * a + intercept.

        A slope near 1 with a non-zero intercept means the blocks differ by a black level,
        which a multiplicative gain cannot correct. A slope away from 1 with a near-zero
        intercept is a pure gain difference.
        """
        if len(self.qq_a) < 2 or len(self.qq_a) != len(self.qq_b):
            return None
        n = len(self.qq_a)
        mean_a = sum(self.qq_a) / n
        mean_b = sum(self.qq_b) / n
        variance = sum((a - mean_a) ** 2 for a in self.qq_a)
        if variance <= 0:
            return None
        slope = (
            sum((a - mean_a) * (b - mean_b) for a, b in zip(self.qq_a, self.qq_b, strict=True))
            / variance
        )
        return slope, mean_b - slope * mean_a

    @property
    def qq_gain(self) -> float | None:
        """Best pure scale factor between the two blocks: b ~ gain * a, no intercept.

        Needed as the honest comparison for a gain-and-offset model. Taking the slope from a
        fit that also estimated an intercept and then throwing the intercept away is not a
        gain-only model — it can be worse than applying nothing at all, because the two terms
        were fitted together.
        """
        if len(self.qq_a) < 1 or len(self.qq_a) != len(self.qq_b):
            return None
        denominator = sum(a * a for a in self.qq_a)
        if denominator <= 0:
            return None
        return sum(a * b for a, b in zip(self.qq_a, self.qq_b, strict=True)) / denominator


class RadiometricPairResult(BaseModel):
    block_a: str
    block_b: str
    overlap_area_ha: float
    sample_count: int
    sample_pixels: int
    patch_metres: float
    bands: list[BandDifference] = Field(default_factory=list)

    # Graded against thresholds derived in `qa.radiometry`, or left NOT_EVALUATED when the pair
    # was measured in encoded values, where those thresholds do not apply. Defaults to
    # NOT_EVALUATED so reports written before the thresholds existed keep their meaning.
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

    # Percentile levels at which every pair's qq_a / qq_b were sampled. Empty in reports
    # written before quantile capture existed, which is why every consumer must check.
    qq_levels: list[float] = Field(default_factory=list)

    # Whether the sampled values were linearised before the statistics were taken. Every
    # number in this report -- medians, quantiles, percentages -- is in that space, and a gain
    # solved from it is only valid applied in the same space. Defaults to False so reports
    # written before this field existed keep their original meaning.
    linearised: bool = False

    pairs: list[RadiometricPairResult] = Field(default_factory=list)

    @property
    def measured(self) -> list[RadiometricPairResult]:
        return [p for p in self.pairs if p.sample_pixels > 0]

    def count_by_status(self) -> dict[GateStatus, int]:
        counts = dict.fromkeys(GateStatus, 0)
        for pair in self.measured:
            counts[pair.status] += 1
        return counts

    @property
    def status(self) -> GateStatus:
        """The worst grade any measured pair received.

        The worst rather than an average: a project is as consistent as its least consistent
        join, and averaging would let one unusable pair disappear into two hundred good ones.
        """
        counts = self.count_by_status()
        for status in (GateStatus.FAIL, GateStatus.REVIEW, GateStatus.PASS):
            if counts[status]:
                return status
        return GateStatus.NOT_EVALUATED
