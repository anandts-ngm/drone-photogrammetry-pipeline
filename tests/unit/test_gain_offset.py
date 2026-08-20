"""Gain-plus-offset solve tests.

The question these exist to answer is whether a block pair differs by a scale factor or by a
black level. A gain can only fix the first. Synthetic reports with a known gain, a known
offset, or both let the solver be checked against each case separately.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from drone_photogrammetry_pipeline.harmonisation import (
    HarmonisationError,
    solve_gain_offset,
)
from drone_photogrammetry_pipeline.models.qa import (
    BandDifference,
    RadiometricOverlapReport,
    RadiometricPairResult,
)
from drone_photogrammetry_pipeline.qa.radiometry import QQ_LEVELS

BANDS = ("red", "green", "blue")
# A plausible spread of ground brightness, sampled at the same levels the measurement uses.
TRUE_QUANTILES = [12.0, 22.0, 48.0, 90.0, 138.0, 180.0, 205.0]


def make_report(
    transforms: dict[str, tuple[float, float]],
    edges: list[tuple[str, str]],
    *,
    with_quantiles: bool = True,
) -> RadiometricOverlapReport:
    """Each block reports `gain * truth + offset`, so the solve should invert exactly."""
    pairs = []
    for a, b in edges:
        ga, oa = transforms[a]
        gb, ob = transforms[b]
        qa = [ga * q + oa for q in TRUE_QUANTILES]
        qb = [gb * q + ob for q in TRUE_QUANTILES]
        bands = [
            BandDifference(
                band=name,
                median_a=qa[3],
                median_b=qb[3],
                median_difference=qb[3] - qa[3],
                relative_difference_pct=0.0,
                robust_normalized_difference_pct=0.0,
                qq_a=qa if with_quantiles else [],
                qq_b=qb if with_quantiles else [],
            )
            for name in BANDS
        ]
        pairs.append(
            RadiometricPairResult(
                block_a=a,
                block_b=b,
                overlap_area_ha=10.0,
                sample_count=48,
                sample_pixels=10_000,
                patch_metres=4.0,
                bands=bands,
            )
        )
    return RadiometricOverlapReport(
        project_id="Test",
        generated_at=datetime.now(UTC),
        pair_count=len(pairs),
        measured_count=len(pairs),
        qq_levels=list(QQ_LEVELS) if with_quantiles else [],
        pairs=pairs,
    )


CHAIN = [("B1", "B2"), ("B2", "B3"), ("B1", "B3")]


def test_a_pure_gain_difference_is_recovered_with_no_offset() -> None:
    report = make_report({"B1": (1.0, 0.0), "B2": (1.25, 0.0), "B3": (0.8, 0.0)}, CHAIN)

    solution = solve_gain_offset(report)

    for block in solution.blocks:
        assert block.offsets is not None
        assert block.offsets["red"] == pytest.approx(0.0, abs=0.5)
    for residual in solution.residuals:
        assert residual.median_after_offset_pct == pytest.approx(0.0, abs=0.1)


def test_a_pure_black_level_difference_is_recovered() -> None:
    """The case a gain cannot fix: identical scale, different black level."""
    report = make_report({"B1": (1.0, 0.0), "B2": (1.0, 25.0), "B3": (1.0, -10.0)}, CHAIN)

    solution = solve_gain_offset(report)

    for block in solution.blocks:
        assert block.gains["red"] == pytest.approx(1.0, abs=0.02)
    offsets = {b.block_id: b.offsets["red"] for b in solution.blocks if b.offsets}
    # Offsets are recovered up to the anchor, so differences are what must match.
    assert offsets["B2"] - offsets["B1"] == pytest.approx(-25.0, abs=0.5)
    assert offsets["B3"] - offsets["B1"] == pytest.approx(10.0, abs=0.5)


def test_a_black_level_difference_defeats_a_gain_but_not_a_gain_plus_offset() -> None:
    """This is the whole point of the module."""
    report = make_report({"B1": (1.0, 0.0), "B2": (1.0, 30.0), "B3": (1.0, -12.0)}, CHAIN)

    solution = solve_gain_offset(report)

    for residual in solution.residuals:
        with_offset = residual.median_after_offset_pct
        assert with_offset is not None
        assert residual.median_after_pct > 5.0, "a gain alone should not fix a black level"
        assert with_offset == pytest.approx(0.0, abs=0.2)
        assert with_offset < residual.median_after_pct


def test_a_combined_gain_and_offset_is_recovered() -> None:
    report = make_report({"B1": (1.0, 0.0), "B2": (1.3, 18.0), "B3": (0.75, -8.0)}, CHAIN)

    solution = solve_gain_offset(report)

    gains = {b.block_id: b.gains["red"] for b in solution.blocks}
    assert gains["B2"] / gains["B1"] == pytest.approx(1.0 / 1.3, rel=1e-3)
    assert gains["B3"] / gains["B1"] == pytest.approx(1.0 / 0.75, rel=1e-3)
    for residual in solution.residuals:
        assert residual.median_after_offset_pct == pytest.approx(0.0, abs=0.2)


def test_the_offset_anchor_preserves_mean_level() -> None:
    report = make_report({"B1": (1.0, 0.0), "B2": (1.0, 40.0), "B3": (1.0, -20.0)}, CHAIN)
    solution = solve_gain_offset(report)

    total = sum(b.offsets["red"] for b in solution.blocks if b.offsets)
    assert total == pytest.approx(0.0, abs=1e-6)
    log_gain = sum(math.log(b.gains["red"]) for b in solution.blocks)
    assert log_gain == pytest.approx(0.0, abs=1e-6)


def test_the_model_and_offsets_are_recorded() -> None:
    solution = solve_gain_offset(make_report({"B1": (1.0, 0.0), "B2": (1.2, 5.0)}, [("B1", "B2")]))

    assert solution.model == "gain_offset"
    for residual in solution.residuals:
        assert residual.offset_min is not None
        assert residual.offset_max is not None


def test_separate_groups_are_still_not_bridged() -> None:
    report = make_report(
        {"B1": (1.0, 0.0), "B2": (1.2, 5.0), "B8": (1.0, 0.0), "B9": (0.9, -3.0)},
        [("B1", "B2"), ("B8", "B9")],
    )
    solution = solve_gain_offset(report)

    assert solution.component_count == 2
    for component in (0, 1):
        members = [b for b in solution.blocks if b.component == component and b.offsets]
        assert sum(b.offsets["red"] for b in members if b.offsets) == pytest.approx(0.0, abs=1e-6)


def test_a_report_without_quantiles_is_refused_with_a_usable_message() -> None:
    """Reports written before quantile capture cannot support this solve."""
    report = make_report({"B1": (1.0, 0.0), "B2": (1.2, 0.0)}, [("B1", "B2")], with_quantiles=False)

    with pytest.raises(HarmonisationError, match="re-run 'drone-photo radiometry'"):
        solve_gain_offset(report)


def test_quantile_fit_distinguishes_scale_from_black_level() -> None:
    """The diagnostic itself, independent of any solve."""
    gain_only = make_report({"B1": (1.0, 0.0), "B2": (1.4, 0.0)}, [("B1", "B2")])
    offset_only = make_report({"B1": (1.0, 0.0), "B2": (1.0, 30.0)}, [("B1", "B2")])

    gain_fit = gain_only.pairs[0].bands[0].qq_fit
    offset_fit = offset_only.pairs[0].bands[0].qq_fit
    assert gain_fit is not None and offset_fit is not None

    assert gain_fit[0] == pytest.approx(1.4, rel=1e-6)
    assert gain_fit[1] == pytest.approx(0.0, abs=1e-6)
    assert offset_fit[0] == pytest.approx(1.0, rel=1e-6)
    assert offset_fit[1] == pytest.approx(30.0, abs=1e-6)
