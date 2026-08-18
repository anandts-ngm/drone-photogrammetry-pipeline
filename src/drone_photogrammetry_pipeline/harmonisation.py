"""Solving per-block radiometric coefficients from measured overlaps.

The disagreement measured by `qa.radiometry` is between pairs. Turning it into one
coefficient per block is a network adjustment: for every overlapping pair the two blocks
should agree, and the coefficients that best satisfy all pairs at once are a least-squares
solution. Nobody chooses them.

Three properties this has to have, and why:

* **One solve for the whole project.** Correcting each block to look good on its own is what
  produces seams in the first place, because two neighbours then receive different
  corrections. The whole point is to satisfy the pairs jointly.
* **An anchor per connected group.** The constraints are all relative — they say block A
  should match block B, never what either should be absolutely. That leaves one degree of
  freedom free per group of connected blocks, and it has to be fixed deliberately. The
  default preserves the group's mean brightness, so harmonising does not quietly lighten or
  darken the whole project.
* **No solving across a gap.** Blocks that share no chain of overlaps have no measured
  relationship at all. Their groups are solved separately and reported separately rather
  than tied together by a number nothing supports.

This module does arithmetic only. It reads no rasters and writes no files, so the part of
harmonisation most likely to be wrong is also the part that is cheapest to test.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray

from .models.harmonisation import BandResidual, BlockGains, HarmonisationSolution
from .models.qa import RadiometricOverlapReport, RadiometricPairResult

ANCHOR_PROJECT_MEAN = "project_mean"
WEIGHT_BY_SAMPLES = "sqrt_sample_pixels"
WEIGHT_NONE = "none"


class HarmonisationError(RuntimeError):
    pass


def _symmetric_pct(a: float, b: float) -> float:
    centre = (a + b) / 2.0
    return 200.0 * (b - a) / (a + b) if centre > 0 else 0.0


def _components(
    blocks: list[str], pairs: list[RadiometricPairResult]
) -> tuple[dict[str, int], int]:
    """Group blocks that are connected by a chain of overlaps."""
    adjacency: dict[str, set[str]] = defaultdict(set)
    for pair in pairs:
        adjacency[pair.block_a].add(pair.block_b)
        adjacency[pair.block_b].add(pair.block_a)

    component_of: dict[str, int] = {}
    index = 0
    for block in blocks:
        if block in component_of:
            continue
        stack = [block]
        while stack:
            current = stack.pop()
            if current in component_of:
                continue
            component_of[current] = index
            stack.extend(adjacency[current] - component_of.keys())
        index += 1
    return component_of, index


def _usable_pairs(report: RadiometricOverlapReport, band_index: int) -> list[RadiometricPairResult]:
    usable = []
    for pair in report.measured:
        if len(pair.bands) <= band_index:
            continue
        band = pair.bands[band_index]
        if band.median_a > 0 and band.median_b > 0:
            usable.append(pair)
    return usable


def _solve_band(
    pairs: list[RadiometricPairResult],
    blocks: list[str],
    band_index: int,
    component_of: dict[str, int],
    component_count: int,
    *,
    weight: bool,
) -> NDArray[np.float64]:
    position = {block: i for i, block in enumerate(blocks)}
    rows: list[NDArray[np.float64]] = []
    values: list[float] = []

    for pair in pairs:
        band = pair.bands[band_index]
        row = np.zeros(len(blocks), dtype=np.float64)
        row[position[pair.block_a]] = 1.0
        row[position[pair.block_b]] = -1.0
        # In log space a multiplicative gain becomes additive, which makes the system linear.
        # Weighting by the square root of the sample count reflects that a median estimated
        # from more pixels is the more trustworthy constraint.
        scale = math.sqrt(pair.sample_pixels) if weight and pair.sample_pixels > 0 else 1.0
        rows.append(row * scale)
        values.append(math.log(band.median_b / band.median_a) * scale)

    # One anchor per connected group. A single global anchor would leave every additional
    # group underdetermined, and least squares would then distribute the slack arbitrarily
    # instead of failing.
    anchor_scale = (
        math.sqrt(sum(p.sample_pixels for p in pairs) / max(len(pairs), 1)) if weight else 1.0
    )
    for component in range(component_count):
        row = np.array(
            [1.0 if component_of[block] == component else 0.0 for block in blocks],
            dtype=np.float64,
        )
        rows.append(row * anchor_scale)
        values.append(0.0)

    solution, *_ = np.linalg.lstsq(np.array(rows), np.array(values), rcond=None)
    return np.asarray(np.exp(solution), dtype=np.float64)


def solve_gains(
    report: RadiometricOverlapReport,
    *,
    anchor: str = ANCHOR_PROJECT_MEAN,
    weight_by_samples: bool = True,
) -> HarmonisationSolution:
    """Solve one multiplicative gain per block per band from measured overlaps."""
    if anchor != ANCHOR_PROJECT_MEAN:
        raise HarmonisationError(
            f"unknown anchor '{anchor}'; only '{ANCHOR_PROJECT_MEAN}' is implemented, and an "
            "anchor must be an explicit choice rather than an accident of the solver"
        )

    measured = report.measured
    if not measured:
        raise HarmonisationError(
            f"{report.project_id} has no measured overlaps, so there is nothing to solve from"
        )

    band_names = [band.band for band in measured[0].bands]
    blocks = sorted({b for p in measured for b in (p.block_a, p.block_b)})
    component_of, component_count = _components(blocks, measured)

    gains: dict[str, NDArray[np.float64]] = {}
    residuals: list[BandResidual] = []
    after_by_block: dict[str, list[float]] = defaultdict(list)
    constraint_count = 0

    for band_index, band_name in enumerate(band_names):
        pairs = _usable_pairs(report, band_index)
        if not pairs:
            raise HarmonisationError(f"no usable constraints for band '{band_name}'")
        constraint_count = max(constraint_count, len(pairs))
        solved = _solve_band(
            pairs, blocks, band_index, component_of, component_count, weight=weight_by_samples
        )
        gains[band_name] = solved

        position = {block: i for i, block in enumerate(blocks)}
        before, after = [], []
        for pair in pairs:
            band = pair.bands[band_index]
            before.append(abs(_symmetric_pct(band.median_a, band.median_b)))
            corrected = abs(
                _symmetric_pct(
                    solved[position[pair.block_a]] * band.median_a,
                    solved[position[pair.block_b]] * band.median_b,
                )
            )
            after.append(corrected)
            after_by_block[pair.block_a].append(corrected)
            after_by_block[pair.block_b].append(corrected)

        residuals.append(
            BandResidual(
                band=band_name,
                median_before_pct=float(np.median(before)),
                median_after_pct=float(np.median(after)),
                p90_before_pct=float(np.percentile(before, 90)),
                p90_after_pct=float(np.percentile(after, 90)),
                gain_min=float(solved.min()),
                gain_max=float(solved.max()),
            )
        )

    overlap_count: dict[str, int] = defaultdict(int)
    for pair in measured:
        overlap_count[pair.block_a] += 1
        overlap_count[pair.block_b] += 1

    return HarmonisationSolution(
        project_id=report.project_id,
        generated_at=datetime.now(UTC),
        source_report=f"{report.project_id} radiometry, {report.measured_count} measured pairs",
        anchor=anchor,
        weighting=WEIGHT_BY_SAMPLES if weight_by_samples else WEIGHT_NONE,
        constraint_count=constraint_count,
        block_count=len(blocks),
        component_count=component_count,
        blocks=[
            BlockGains(
                block_id=block,
                gains={name: float(gains[name][index]) for name in band_names},
                overlap_count=overlap_count[block],
                component=component_of[block],
                residual_pct=(
                    float(np.median(after_by_block[block])) if after_by_block[block] else None
                ),
            )
            for index, block in enumerate(blocks)
        ],
        residuals=residuals,
    )
