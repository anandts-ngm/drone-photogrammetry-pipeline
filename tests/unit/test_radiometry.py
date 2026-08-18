"""Radiometric overlap measurement tests.

These use synthetic blocks whose pixel values are a function of world position, so two
blocks agree exactly on shared ground and any measured difference is only what the fixture
deliberately introduced. That gives a known answer to check against, which a comparison of
two real orthophotos never can.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from drone_photogrammetry_pipeline.models.enums import GateStatus
from drone_photogrammetry_pipeline.models.qa import RadiometricPairResult
from drone_photogrammetry_pipeline.qa.radiometry import (
    RadiometryError,
    compare_pair,
    measure_project,
    read_footprints,
)
from tests.fixtures import make_rasters


def pair(tmp_path: Path, **kwargs: Any) -> tuple[Path, Path]:
    return make_rasters.overlapping_pair(tmp_path, **kwargs)


def measure(a: Path, b: Path) -> RadiometricPairResult:
    footprints = read_footprints([("A", a), ("B", b)])
    return compare_pair(footprints[0], footprints[1])


def expected_relative(gain: float) -> float:
    """The symmetric relative difference a pure gain must produce."""
    return 200.0 * (gain - 1.0) / (gain + 1.0)


def test_identical_blocks_show_no_disagreement(tmp_path: Path) -> None:
    result = measure(*pair(tmp_path, gain=1.0))

    assert result.sample_pixels > 0
    for band in result.bands:
        assert abs(band.relative_difference_pct) < 0.5


def test_a_known_gain_is_recovered(tmp_path: Path) -> None:
    result = measure(*pair(tmp_path, gain=1.1))

    for band in result.bands:
        assert band.relative_difference_pct == pytest.approx(expected_relative(1.1), abs=0.5)


def test_a_known_offset_is_recovered(tmp_path: Path) -> None:
    result = measure(*pair(tmp_path, offset=20.0))

    for band in result.bands:
        assert band.median_difference == pytest.approx(20.0, abs=1.0)


def test_the_sign_reflects_which_block_is_brighter(tmp_path: Path) -> None:
    brighter = measure(*pair(tmp_path / "up", gain=1.2))
    darker = measure(*pair(tmp_path / "down", gain=0.8))

    assert all(b.relative_difference_pct > 0 for b in brighter.bands)
    assert all(b.relative_difference_pct < 0 for b in darker.bands)


def test_the_robust_statistic_agrees_with_the_median_ratio(tmp_path: Path) -> None:
    result = measure(*pair(tmp_path, gain=1.15))

    for band in result.bands:
        assert band.robust_normalized_difference_pct == pytest.approx(
            expected_relative(1.15), abs=1.0
        )


def test_blocks_of_different_pixel_size_are_still_comparable(tmp_path: Path) -> None:
    """A 2 cm block and a 5 cm block must still yield the gain between them."""
    result = measure(*pair(tmp_path, gain=1.1, pixel_a=0.05, pixel_b=0.02))

    assert result.sample_pixels > 0
    for band in result.bands:
        assert band.relative_difference_pct == pytest.approx(expected_relative(1.1), abs=1.0)


def test_transparent_margins_do_not_contaminate_the_comparison(tmp_path: Path) -> None:
    result = measure(*pair(tmp_path, gain=1.1, valid_border=40))

    assert result.sample_pixels > 0
    for band in result.bands:
        assert band.relative_difference_pct == pytest.approx(expected_relative(1.1), abs=1.0)


def test_blocks_that_do_not_touch_report_no_overlap(tmp_path: Path) -> None:
    result = measure(*pair(tmp_path, shift_m=500.0))

    assert result.sample_pixels == 0
    assert result.overlap_area_ha == 0.0
    assert result.note == "no overlap"


def test_mismatched_crs_is_refused_rather_than_reprojected(tmp_path: Path) -> None:
    a, b = pair(tmp_path, crs_b="EPSG:32649")

    with pytest.raises(RadiometryError, match="reprojection"):
        measure(a, b)


def test_no_pair_is_judged_until_thresholds_exist(tmp_path: Path) -> None:
    result = measure(*pair(tmp_path, gain=1.5))

    assert result.status is GateStatus.NOT_EVALUATED


def test_overlap_area_is_reported(tmp_path: Path) -> None:
    result = measure(*pair(tmp_path, shift_m=20.0))

    # 20 m of shared width across the full 40 m height.
    assert result.overlap_area_ha == pytest.approx(20.0 * 40.0 / 1e4, rel=0.01)


def test_project_measurement_covers_every_qualifying_pair(tmp_path: Path) -> None:
    a, b = pair(tmp_path, gain=1.1)
    report = measure_project(
        "Test", [("A", a), ("B", b)], min_overlap_ha=0.0, patches=16, patch_metres=4.0
    )

    assert report.project_id == "Test"
    assert report.pair_count == 1
    assert report.measured_count == 1
    assert report.pairs[0].max_abs_relative_difference_pct == pytest.approx(
        expected_relative(1.1), abs=1.0
    )


def test_pairs_below_the_overlap_threshold_are_skipped(tmp_path: Path) -> None:
    a, b = pair(tmp_path, shift_m=20.0)
    report = measure_project("Test", [("A", a), ("B", b)], min_overlap_ha=10.0)

    assert report.pair_count == 0
    assert report.pairs == []
