"""Master raster QA tests.

Every contract clause has a conforming case and a violating case, and a violating case must
fail with its own check named. A test that only asserts `status == FAIL` would pass even if
QA failed for an unrelated reason, which is exactly the confusion the contract exists to
prevent.

Clauses whose violation cannot be produced by GDAL on a valid file (a four-band JPEG, for
example) are driven through a fake backend, so the QA logic is still covered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from drone_photogrammetry_pipeline.models.enums import CheckOutcome, GateStatus
from drone_photogrammetry_pipeline.models.qa import RasterQAResult
from drone_photogrammetry_pipeline.packaging.gdal_backend import (
    PackagingPlan,
    RasterDescription,
)
from drone_photogrammetry_pipeline.packaging.raster import package_master
from drone_photogrammetry_pipeline.qa.raster import run_raster_qa
from tests.fixtures import make_rasters


class FakeBackend:
    """Serves one prepared description, so QA logic can be tested without GDAL."""

    name = "fake"

    def __init__(self, description: RasterDescription) -> None:
        self._description = description

    def gdal_version(self) -> str:
        return "0.0.0"

    def describe(self, path: Path) -> RasterDescription:
        return self._description

    def package(self, source: Path, destination: Path, plan: PackagingPlan) -> Any:
        raise NotImplementedError


def description(**overrides: Any) -> RasterDescription:
    base: dict[str, Any] = {
        "path": Path("master.tif"),
        "driver": "GTiff",
        "width": 64,
        "height": 48,
        "band_count": 4,
        "dtype": "uint8",
        "crs": "EPSG:32648",
        "transform": (0.017, 0.0, 500000.0, 0.0, -0.017, 5000000.0),
        "colorinterp": ("red", "green", "blue", "alpha"),
        "nodatavals": (None, None, None, None),
        "compression": "DEFLATE",
        "tiled": True,
        "block_shape": (512, 512),
        "overview_counts": (0, 0, 0, 0),
        "tiff_version": 43,
    }
    base.update(overrides)
    return RasterDescription(**base)


def qa_of(**overrides: Any) -> RasterQAResult:
    return run_raster_qa(Path("master.tif"), backend=FakeBackend(description(**overrides)))


def failed_check_names(result: RasterQAResult) -> set[str]:
    return {check.name for check in result.failures}


def test_a_packaged_master_passes(tmp_path: Path) -> None:
    source = make_rasters.rgba_source(tmp_path / "source.tif")
    result = package_master(source, tmp_path / "master.tif")

    qa_result = run_raster_qa(result.master_path)

    assert qa_result.status is GateStatus.PASS
    assert qa_result.failures == []
    assert qa_result.checks["rgba"] is True
    assert qa_result.checks["compression"] == "DEFLATE"
    assert qa_result.checks["overview_count"] == 0


def test_conforming_description_passes() -> None:
    assert qa_of().status is GateStatus.PASS


def test_rejects_compression_other_than_deflate() -> None:
    result = qa_of(compression="LZW")
    assert result.status is GateStatus.FAIL
    assert failed_check_names(result) == {"compression"}


def test_rejects_lossy_compression() -> None:
    result = qa_of(compression="JPEG")
    assert result.status is GateStatus.FAIL
    assert failed_check_names(result) == {"compression", "no_lossy_compression"}


def test_rejects_striped_storage() -> None:
    result = qa_of(tiled=False)
    assert failed_check_names(result) == {"tiled"}


def test_rejects_classic_tiff() -> None:
    result = qa_of(tiff_version=42)
    assert failed_check_names(result) == {"bigtiff"}


def test_rejects_untagged_alpha_band() -> None:
    result = qa_of(colorinterp=("red", "green", "blue", "undefined"))
    assert failed_check_names(result) == {"colorinterp", "alpha_present"}


def test_rejects_three_band_raster() -> None:
    result = qa_of(band_count=3, colorinterp=("red", "green", "blue"))
    assert failed_check_names(result) == {"band_count", "colorinterp", "alpha_present"}


def test_rejects_nodata_alongside_alpha() -> None:
    result = qa_of(nodatavals=(0.0, 0.0, 0.0, 0.0))
    assert failed_check_names(result) == {"nodata_policy"}


def test_rejects_embedded_overviews() -> None:
    result = qa_of(overview_counts=(4, 4, 4, 4))
    assert failed_check_names(result) == {"overview_count"}
    assert result.checks["overview_count"] == 16


def test_rejects_missing_crs() -> None:
    result = qa_of(crs=None)
    assert failed_check_names(result) == {"crs_present"}


def test_rejects_rotated_geotransform() -> None:
    result = qa_of(transform=(0.017, 0.002, 500000.0, 0.003, -0.017, 5000000.0))
    assert failed_check_names(result) == {"pixel_size_present"}


def test_rejects_non_geotiff() -> None:
    result = qa_of(driver="PNG")
    assert "is_geotiff" in failed_check_names(result)


def test_unreadable_file_fails_rather_than_raising(tmp_path: Path) -> None:
    not_a_raster = tmp_path / "notes.txt"
    not_a_raster.write_text("this is not a raster", encoding="utf-8")

    result = run_raster_qa(not_a_raster)

    assert result.status is GateStatus.FAIL
    assert failed_check_names(result) == {"readable"}


def test_failure_states_the_clause_and_what_was_observed() -> None:
    result = qa_of(compression="LZW")
    check = next(c for c in result.failures if c.name == "compression")

    assert check.clause == "Compression DEFLATE"
    assert check.expected == "DEFLATE"
    assert check.observed == "LZW"
    assert "DEFLATE" in check.message and "LZW" in check.message


def test_alpha_derived_from_nodata_is_review_not_pass(tmp_path: Path) -> None:
    source = make_rasters.nodata_rgb_source(tmp_path / "source.tif")
    result = package_master(
        source, tmp_path / "master.tif", plan=PackagingPlan(allow_alpha_from_nodata=True)
    )

    qa_result = run_raster_qa(result.master_path, alpha_provenance=result.record.alpha_provenance)

    assert qa_result.status is GateStatus.REVIEW
    assert qa_result.failures == []
    assert [c.name for c in qa_result.reviews] == ["alpha_provenance"]


def test_alpha_from_a_real_mask_is_a_clean_pass(tmp_path: Path) -> None:
    source = make_rasters.masked_rgb_source(tmp_path / "source.tif")
    result = package_master(source, tmp_path / "master.tif")

    qa_result = run_raster_qa(result.master_path, alpha_provenance=result.record.alpha_provenance)

    assert qa_result.status is GateStatus.PASS


def test_a_terra_dom_as_delivered_fails_on_nodata_and_nothing_else(tmp_path: Path) -> None:
    """Records exactly how far a real Terra export is from the contract.

    All 79 Buduunkhad zones are already GeoTIFF/RGBA/DEFLATE/tiled/BigTIFF with a CRS and no
    overviews. The single defect is the NoData value carried alongside the alpha band.
    """
    source = make_rasters.terra_dom_source(tmp_path / "dom.tif")

    result = run_raster_qa(source)

    assert result.status is GateStatus.FAIL
    assert failed_check_names(result) == {"nodata_policy"}


def test_a_packaged_terra_dom_passes(tmp_path: Path) -> None:
    source = make_rasters.terra_dom_source(tmp_path / "dom.tif")
    result = package_master(source, tmp_path / "master.tif")

    assert run_raster_qa(result.master_path).status is GateStatus.PASS


def test_an_unpackaged_odm_style_source_does_not_satisfy_the_contract(tmp_path: Path) -> None:
    """A raster that looks fine to a viewer is still not a master until it is packaged."""
    source = make_rasters.masked_rgb_source(tmp_path / "source.tif")

    result = run_raster_qa(source)

    assert result.status is GateStatus.FAIL
    assert {"band_count", "alpha_present"} <= failed_check_names(result)


@pytest.mark.parametrize(
    "overrides",
    [
        {"compression": "LZW"},
        {"tiled": False},
        {"tiff_version": 42},
        {"overview_counts": (4, 4, 4, 4)},
    ],
)
def test_every_violation_is_reported_as_a_failed_check(overrides: dict[str, Any]) -> None:
    result = qa_of(**overrides)
    assert result.status is GateStatus.FAIL
    assert all(c.outcome is CheckOutcome.FAIL for c in result.failures)
    assert result.failures
