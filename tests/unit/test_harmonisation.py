"""Harmonisation solve tests.

The solve is pure arithmetic over a measured report, so a report can be synthesised with
known per-block gains and the solver checked against them exactly. That is the whole reason
this module reads no rasters.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from drone_photogrammetry_pipeline.harmonisation import (
    HarmonisationError,
    solve_gains,
)
from drone_photogrammetry_pipeline.models.harmonisation import HarmonisationSolution
from drone_photogrammetry_pipeline.models.qa import (
    BandDifference,
    RadiometricOverlapReport,
    RadiometricPairResult,
)

BANDS = ("red", "green", "blue")


def make_report(
    truth: dict[str, float],
    edges: list[tuple[str, str]],
    *,
    base: float = 120.0,
    sample_pixels: int = 10_000,
    project_id: str = "Test",
) -> RadiometricOverlapReport:
    """A report in which every block's true brightness is `base / truth[block]`.

    A block whose values are dimmed by a factor needs a gain of that factor to come back, so
    the solver should recover `truth` up to the anchor.
    """
    pairs = []
    for a, b in edges:
        bands = [
            BandDifference(
                band=name,
                median_a=base / truth[a],
                median_b=base / truth[b],
                median_difference=base / truth[b] - base / truth[a],
                relative_difference_pct=0.0,
                robust_normalized_difference_pct=0.0,
            )
            for name in BANDS
        ]
        pairs.append(
            RadiometricPairResult(
                block_a=a,
                block_b=b,
                overlap_area_ha=10.0,
                sample_count=48,
                sample_pixels=sample_pixels,
                patch_metres=4.0,
                bands=bands,
            )
        )
    return RadiometricOverlapReport(
        project_id=project_id,
        generated_at=datetime.now(UTC),
        pair_count=len(pairs),
        measured_count=len(pairs),
        pairs=pairs,
    )


def red_gain(solution: HarmonisationSolution, block: str) -> float:
    gains = solution.gains_for(block)
    assert gains is not None, f"{block} missing from the solution"
    return gains["red"]


def normalised(solution: HarmonisationSolution, block: str, reference: str) -> float:
    """Gain of `block` relative to `reference`, which removes the anchor."""
    return red_gain(solution, block) / red_gain(solution, reference)


def test_known_gains_are_recovered_up_to_the_anchor() -> None:
    truth = {"B1": 1.0, "B2": 1.25, "B3": 0.8}
    report = make_report(truth, [("B1", "B2"), ("B2", "B3"), ("B1", "B3")])

    solution = solve_gains(report)

    for block in truth:
        assert normalised(solution, block, "B1") == pytest.approx(
            truth[block] / truth["B1"], rel=1e-6
        )


def test_a_consistent_network_is_solved_exactly() -> None:
    truth = {"B1": 1.0, "B2": 1.4, "B3": 0.7, "B4": 1.1}
    report = make_report(truth, [("B1", "B2"), ("B2", "B3"), ("B3", "B4"), ("B4", "B1")])

    solution = solve_gains(report)

    for residual in solution.residuals:
        assert residual.median_after_pct == pytest.approx(0.0, abs=1e-6)
        assert residual.p90_after_pct == pytest.approx(0.0, abs=1e-6)


def test_the_anchor_preserves_mean_brightness() -> None:
    """Harmonising must not quietly lighten or darken the whole project."""
    truth = {"B1": 1.0, "B2": 1.5, "B3": 0.6}
    solution = solve_gains(make_report(truth, [("B1", "B2"), ("B2", "B3")]))

    log_sum = sum(math.log(block.gains["red"]) for block in solution.blocks)
    assert log_sum == pytest.approx(0.0, abs=1e-6)


def test_disagreement_is_reduced_and_reported() -> None:
    truth = {"B1": 1.0, "B2": 1.3, "B3": 0.75}
    solution = solve_gains(make_report(truth, [("B1", "B2"), ("B2", "B3"), ("B1", "B3")]))

    for residual in solution.residuals:
        assert residual.median_before_pct > 10.0
        assert residual.median_after_pct < residual.median_before_pct
        assert residual.gain_min < residual.gain_max


def test_an_inconsistent_network_leaves_a_residual() -> None:
    """Three blocks whose pairwise claims cannot all be true at once."""
    report = make_report({"B1": 1.0, "B2": 1.2, "B3": 1.0}, [("B1", "B2"), ("B2", "B3")])
    # Contradict the third edge: B1 and B3 are claimed equal above, so assert a difference.
    contradiction = make_report({"B1": 1.0, "B3": 1.5}, [("B1", "B3")])
    report.pairs.extend(contradiction.pairs)
    report.measured_count = len(report.pairs)

    solution = solve_gains(report)

    assert any(r.median_after_pct > 1.0 for r in solution.residuals)


def test_separate_groups_are_reported_not_bridged() -> None:
    """Blocks sharing no chain of overlaps have no measured relationship."""
    report = make_report(
        {"B1": 1.0, "B2": 1.2, "B9": 1.0, "B8": 0.9},
        [("B1", "B2"), ("B8", "B9")],
    )

    solution = solve_gains(report)

    assert solution.component_count == 2
    assert not solution.is_single_component
    groups = {block.block_id: block.component for block in solution.blocks}
    assert groups["B1"] == groups["B2"]
    assert groups["B8"] == groups["B9"]
    assert groups["B1"] != groups["B8"]


def test_each_group_is_anchored_separately() -> None:
    report = make_report(
        {"B1": 1.0, "B2": 1.44, "B8": 1.0, "B9": 2.25}, [("B1", "B2"), ("B8", "B9")]
    )
    solution = solve_gains(report)

    for component in (0, 1):
        members = [b for b in solution.blocks if b.component == component]
        assert sum(math.log(b.gains["red"]) for b in members) == pytest.approx(0.0, abs=1e-6)


def test_per_block_residual_and_overlap_count_are_recorded() -> None:
    truth = {"B1": 1.0, "B2": 1.3, "B3": 0.75}
    solution = solve_gains(make_report(truth, [("B1", "B2"), ("B2", "B3"), ("B1", "B3")]))

    for block in solution.blocks:
        assert block.overlap_count == 2
        assert block.residual_pct is not None


def test_weighting_favours_the_better_measured_constraint() -> None:
    """A contradiction should resolve toward the pair backed by more samples."""
    strong = make_report({"A": 1.0, "B": 1.0}, [("A", "B")], sample_pixels=1_000_000)
    weak = make_report({"A": 1.0, "B": 2.0}, [("A", "B")], sample_pixels=100)
    strong.pairs.extend(weak.pairs)
    strong.measured_count = len(strong.pairs)

    weighted = solve_gains(strong, weight_by_samples=True)
    unweighted = solve_gains(strong, weight_by_samples=False)

    # With weighting the well-sampled "they agree" constraint should dominate, leaving the
    # two gains closer together than the unweighted compromise does.
    def spread(solution: HarmonisationSolution) -> float:
        gains = [b.gains["red"] for b in solution.blocks]
        return max(gains) / min(gains)

    assert spread(weighted) < spread(unweighted)


def test_an_empty_report_is_refused() -> None:
    empty = RadiometricOverlapReport(
        project_id="Test",
        generated_at=datetime.now(UTC),
        pair_count=0,
        measured_count=0,
        pairs=[],
    )
    with pytest.raises(HarmonisationError, match="nothing to solve"):
        solve_gains(empty)


def test_an_unknown_anchor_is_refused_rather_than_guessed() -> None:
    report = make_report({"B1": 1.0, "B2": 1.2}, [("B1", "B2")])
    with pytest.raises(HarmonisationError, match="anchor"):
        solve_gains(report, anchor="whatever_looks_best")
