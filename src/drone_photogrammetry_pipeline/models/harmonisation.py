"""Harmonisation contracts.

A harmonisation solution is a claim about a whole project: these per-block coefficients,
solved from these overlaps, reduce disagreement from this to that. Every part of that claim
is recorded, so the correction is reproducible and reversible rather than a one-off.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class RadiometricSpace(StrEnum):
    """Which encoding the gains were solved in, and therefore must be applied in.

    Measured on B42 (the survey's largest correction): the same physical adjustment is a gain
    of 1.42 in light but 1.16 in display values, because in the power-law region
    `g_encoded = g_linear ** (1/2.4)`. Applying a linear gain directly to DN over-brightens by
    about 23%, a mean error of 22 DN — larger than any error this pipeline is trying to
    remove.

    The two solutions are interchangeable in quality (median residual 6.13% versus 6.17% when
    each is applied in its own space) but not in application. Recording the space is what
    stops a solution being applied in the wrong one, which a filename cannot.
    """

    ENCODED = "encoded"
    LINEAR = "linear"


class BlockGains(BaseModel):
    block_id: str

    # Band name to multiplicative gain. A gain above 1 means the block is darker than the
    # network and is brightened; below 1 means it is brighter and is dimmed.
    gains: dict[str, float]

    # Additive term, in DN, applied after the gain: corrected = gain * value + offset.
    # Absent for a gain-only solution. An offset corrects a black-level difference, which no
    # multiplicative gain can reach.
    offsets: dict[str, float] | None = None

    overlap_count: int

    # Which connected group of overlaps this block belongs to. Gains are only comparable
    # within a group: two blocks that share no chain of overlaps have no measured
    # relationship, and pretending otherwise would invent one.
    component: int = 0

    # Median absolute disagreement across this block's overlaps after the solve. A block that
    # stays high is one a single gain cannot fix — usually a gradient within the block.
    residual_pct: float | None = None


class BandResidual(BaseModel):
    band: str
    median_before_pct: float
    median_after_pct: float
    p90_before_pct: float
    p90_after_pct: float
    gain_min: float
    gain_max: float

    # Present only on a gain+offset solution. Compared against median_after_pct and
    # p90_after_pct, which use the same gains without the offsets, so the difference between
    # the two columns is exactly what the offset term bought.
    median_after_offset_pct: float | None = None
    p90_after_offset_pct: float | None = None
    offset_min: float | None = None
    offset_max: float | None = None


class HarmonisationSolution(BaseModel):
    project_id: str
    generated_at: datetime
    source_report: str

    # "gain" or "gain_offset".
    model: str = "gain"

    # Defaults to ENCODED so that solutions written before this field existed keep the meaning
    # they were produced with, rather than being silently reinterpreted as linear.
    space: RadiometricSpace = RadiometricSpace.ENCODED

    # How the solution is pinned down. Relative constraints alone leave every block free to
    # drift together, so one degree of freedom per connected group must be fixed by choice.
    anchor: str
    weighting: str

    constraint_count: int
    block_count: int
    component_count: int

    blocks: list[BlockGains] = Field(default_factory=list)
    residuals: list[BandResidual] = Field(default_factory=list)

    @property
    def is_single_component(self) -> bool:
        return self.component_count == 1

    def gains_for(self, block_id: str) -> dict[str, float] | None:
        for block in self.blocks:
            if block.block_id == block_id:
                return block.gains
        return None
