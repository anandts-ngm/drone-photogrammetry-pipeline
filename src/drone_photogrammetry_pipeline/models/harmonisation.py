"""Harmonisation contracts.

A harmonisation solution is a claim about a whole project: these per-block coefficients,
solved from these overlaps, reduce disagreement from this to that. Every part of that claim
is recorded, so the correction is reproducible and reversible rather than a one-off.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BlockGains(BaseModel):
    block_id: str

    # Band name to multiplicative gain. A gain above 1 means the block is darker than the
    # network and is brightened; below 1 means it is brighter and is dimmed.
    gains: dict[str, float]

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


class HarmonisationSolution(BaseModel):
    project_id: str
    generated_at: datetime
    source_report: str

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
