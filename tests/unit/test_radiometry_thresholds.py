"""The pass thresholds, and the one thing they refuse to judge.

These numbers came out of the deliveries rather than out of a preference, so the tests pin the
boundaries and the reasons attached to them. The derivation is in `qa.radiometry` and
`docs/radiometry.md`; what matters here is that the boundaries do not drift silently.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from drone_photogrammetry_pipeline.models.enums import GateStatus
from drone_photogrammetry_pipeline.models.qa import (
    BandDifference,
    RadiometricOverlapReport,
    RadiometricPairResult,
)
from drone_photogrammetry_pipeline.qa.radiometry import (
    PAIR_FAIL_PCT,
    PAIR_REVIEW_PCT,
    judge_pair,
)


def pair(mean_pct: float, *, status: GateStatus, block_b: str = "B2") -> RadiometricPairResult:
    """A pair whose three bands all differ by `mean_pct`, already graded."""
    return RadiometricPairResult(
        block_a="B1",
        block_b=block_b,
        overlap_area_ha=50.0,
        sample_count=48,
        sample_pixels=10_000,
        patch_metres=4.0,
        bands=[
            BandDifference(
                band=name,
                median_a=100.0,
                median_b=100.0 + mean_pct,
                median_difference=mean_pct,
                relative_difference_pct=mean_pct,
                robust_normalized_difference_pct=mean_pct,
            )
            for name in ("red", "green", "blue")
        ],
        status=status,
    )


def report(*pairs: RadiometricPairResult) -> RadiometricOverlapReport:
    return RadiometricOverlapReport(
        project_id="Buduunkhad",
        generated_at=datetime(2026, 8, 21, tzinfo=UTC),
        pair_count=len(pairs),
        measured_count=len(pairs),
        linearised=True,
        pairs=list(pairs),
    )


@pytest.mark.parametrize(
    ("mean_pct", "expected"),
    [
        (0.3, GateStatus.PASS),
        (6.0, GateStatus.PASS),
        (PAIR_REVIEW_PCT, GateStatus.PASS),
        (PAIR_REVIEW_PCT + 0.1, GateStatus.REVIEW),
        (25.0, GateStatus.REVIEW),
        (PAIR_FAIL_PCT, GateStatus.REVIEW),
        (PAIR_FAIL_PCT + 0.1, GateStatus.FAIL),
        (65.2, GateStatus.FAIL),
    ],
)
def test_the_boundaries_are_inclusive_at_the_lower_grade(
    mean_pct: float, expected: GateStatus
) -> None:
    """A pair exactly on a threshold gets the better grade, so the gate is not off by an epsilon."""
    status, _ = judge_pair(mean_pct, linearised=True)
    assert status is expected


def test_the_median_of_a_delivered_survey_passes() -> None:
    """6.0% is the median of the corrected Buduunkhad masters, which were accepted and shipped.

    A gate that failed the middle of a delivery already judged good would be measuring
    something other than what it claims to.
    """
    assert judge_pair(6.02, linearised=True)[0] is GateStatus.PASS


def test_the_worst_delivered_pair_fails() -> None:
    """B70/B74: 65% mean disagreement over 51.7 ha, coherent across all three bands."""
    status, note = judge_pair(65.17, linearised=True)

    assert status is GateStatus.FAIL
    assert "65.2%" in note


def test_a_graded_pair_says_what_it_was_graded_against() -> None:
    _, note = judge_pair(20.0, linearised=True)

    assert "linear light" in note
    assert f"{PAIR_REVIEW_PCT:g}%" in note


def test_an_encoded_measurement_is_not_judged() -> None:
    """The same percentage means different things in the two spaces.

    The identical Buduunkhad imagery measures a 7.3% median disagreement in encoded values and
    16.5% in linear light, so one calibration cannot serve both. Declining beats inventing a
    second one for a space nothing is solved in.
    """
    status, note = judge_pair(50.0, linearised=False)

    assert status is GateStatus.NOT_EVALUATED
    assert "--linearise" in note


def test_a_report_takes_the_worst_grade_any_pair_received() -> None:
    """A project is as consistent as its least consistent join."""
    good = pair(3.0, status=GateStatus.PASS)
    review = pair(20.0, status=GateStatus.REVIEW, block_b="B3")
    bad = pair(40.0, status=GateStatus.FAIL, block_b="B4")

    assert report(good).status is GateStatus.PASS
    assert report(good, review).status is GateStatus.REVIEW
    assert report(good, review, bad).status is GateStatus.FAIL
    assert report(good, bad).status is GateStatus.FAIL, "one bad pair is not averaged away"


def test_a_report_of_encoded_pairs_has_no_grade() -> None:
    unjudged = pair(50.0, status=GateStatus.NOT_EVALUATED)

    assert report(unjudged).status is GateStatus.NOT_EVALUATED


def test_the_counts_cover_every_measured_pair() -> None:
    counted = report(
        pair(3.0, status=GateStatus.PASS),
        pair(4.0, status=GateStatus.PASS, block_b="B3"),
        pair(20.0, status=GateStatus.REVIEW, block_b="B4"),
        pair(40.0, status=GateStatus.FAIL, block_b="B5"),
    ).count_by_status()

    assert counted[GateStatus.PASS] == 2
    assert counted[GateStatus.REVIEW] == 1
    assert counted[GateStatus.FAIL] == 1
    assert sum(counted.values()) == 4
