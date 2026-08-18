"""Packaging tests.

The grid invariants are asserted here as well as in production code. A test that packages a
fixture and compares dimensions, geotransform and pixel checksums is the guard against an
accidental resample, which would otherwise produce a plausible-looking wrong answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import rasterio

from drone_photogrammetry_pipeline.models.enums import AlphaProvenance
from drone_photogrammetry_pipeline.packaging.gdal_backend import (
    AmbiguousValidityError,
    MissingValidityError,
    PackagingError,
    PackagingPlan,
    RasterioGdalBackend,
)
from drone_photogrammetry_pipeline.packaging.raster import package_master
from tests.fixtures import make_rasters


def test_packaged_master_meets_the_raster_contract(tmp_path: Path) -> None:
    source = make_rasters.rgba_source(tmp_path / "source.tif")
    result = package_master(source, tmp_path / "master.tif")

    description = result.description
    assert description.driver == "GTiff"
    assert description.band_count == 4
    assert tuple(c.lower() for c in description.colorinterp) == ("red", "green", "blue", "alpha")
    assert description.compression == "DEFLATE"
    assert description.tiled is True
    assert description.is_bigtiff is True
    assert description.overview_counts == (0, 0, 0, 0)
    assert all(value is None for value in description.nodatavals)
    assert description.crs == "EPSG:32648"


def test_packaging_preserves_the_grid_exactly(tmp_path: Path) -> None:
    source = make_rasters.rgba_source(tmp_path / "source.tif")
    backend = RasterioGdalBackend()
    before = backend.describe(source)

    result = package_master(source, tmp_path / "master.tif", backend=backend)

    assert result.description.width == before.width
    assert result.description.height == before.height
    assert result.description.transform == before.transform
    assert result.record.grid_preserved is True


def test_native_pixel_size_is_not_rounded(tmp_path: Path) -> None:
    source = make_rasters.rgba_source(tmp_path / "source.tif")
    result = package_master(source, tmp_path / "master.tif")

    assert result.description.pixel_size_x == pytest.approx(make_rasters.PIXEL_SIZE)
    assert result.description.pixel_size_y == pytest.approx(make_rasters.PIXEL_SIZE)


def test_rgb_pixels_survive_packaging_unchanged(tmp_path: Path) -> None:
    source = make_rasters.rgba_source(tmp_path / "source.tif")
    destination = tmp_path / "master.tif"
    package_master(source, destination, plan=PackagingPlan(verify_pixels=True))

    with rasterio.open(source) as src, rasterio.open(destination) as dst:
        for band in (1, 2, 3):
            assert src.checksum(band) == dst.checksum(band)


def test_verify_pixels_is_recorded_in_the_manifest_record(tmp_path: Path) -> None:
    source = make_rasters.rgba_source(tmp_path / "source.tif")
    result = package_master(source, tmp_path / "master.tif", plan=PackagingPlan(verify_pixels=True))
    assert result.record.pixels_verified is True


def test_existing_alpha_band_is_passed_through(tmp_path: Path) -> None:
    source = make_rasters.rgba_source(tmp_path / "source.tif")
    result = package_master(source, tmp_path / "master.tif")

    assert result.record.alpha_provenance is AlphaProvenance.PASSTHROUGH
    assert result.record.operations == []


def test_untagged_fourth_band_is_retagged_without_changing_pixels(tmp_path: Path) -> None:
    source = make_rasters.four_band_untagged_source(tmp_path / "source.tif")
    destination = tmp_path / "master.tif"
    result = package_master(source, destination)

    assert result.record.alpha_provenance is AlphaProvenance.RETAGGED
    with rasterio.open(source) as src, rasterio.open(destination) as dst:
        assert src.checksum(4) == dst.checksum(4)


def test_alpha_is_derived_from_an_internal_mask(tmp_path: Path) -> None:
    source = make_rasters.masked_rgb_source(tmp_path / "source.tif")
    result = package_master(source, tmp_path / "master.tif")

    assert result.record.alpha_provenance is AlphaProvenance.FROM_MASK
    assert ("alpha", "from_mask") in [(op.name, op.detail) for op in result.record.operations]


def test_ambiguous_nodata_is_refused_without_explicit_opt_in(tmp_path: Path) -> None:
    source = make_rasters.nodata_rgb_source(tmp_path / "source.tif")

    with pytest.raises(AmbiguousValidityError, match="legitimately black"):
        package_master(source, tmp_path / "master.tif")


def test_ambiguous_nodata_is_allowed_with_explicit_opt_in(tmp_path: Path) -> None:
    source = make_rasters.nodata_rgb_source(tmp_path / "source.tif")
    result = package_master(
        source, tmp_path / "master.tif", plan=PackagingPlan(allow_alpha_from_nodata=True)
    )

    assert result.record.alpha_provenance is AlphaProvenance.FROM_NODATA
    assert all(value is None for value in result.description.nodatavals)


def test_source_without_any_validity_signal_is_refused(tmp_path: Path) -> None:
    source = make_rasters.bare_rgb_source(tmp_path / "source.tif")

    with pytest.raises(MissingValidityError, match="validity mask"):
        package_master(source, tmp_path / "master.tif")


def test_source_without_crs_is_refused(tmp_path: Path) -> None:
    source = make_rasters.no_crs_source(tmp_path / "source.tif")

    with pytest.raises(PackagingError, match="declares no CRS"):
        package_master(source, tmp_path / "master.tif")


def test_packaging_records_the_backend_and_real_gdal_version(tmp_path: Path) -> None:
    source = make_rasters.rgba_source(tmp_path / "source.tif")
    result = package_master(source, tmp_path / "master.tif")

    assert result.record.backend == "rasterio-bundled-gdal"
    assert result.record.gdal_version == rasterio.__gdal_version__
    assert result.record.source_sha256


def test_terra_dom_keeps_its_alpha_and_drops_the_redundant_nodata(tmp_path: Path) -> None:
    """A real Terra DOM sets NoData=0 *and* tags band 4 alpha. Alpha must win."""
    source = make_rasters.terra_dom_source(tmp_path / "dom.tif")
    result = package_master(source, tmp_path / "master.tif")

    assert result.record.alpha_provenance is AlphaProvenance.PASSTHROUGH
    assert all(value is None for value in result.description.nodatavals)
    assert ("nodata", "dropped [0.0]; alpha is the validity mask") in [
        (op.name, op.detail) for op in result.record.operations
    ]


def test_terra_dom_alpha_pixels_are_not_recomputed_from_nodata(tmp_path: Path) -> None:
    source = make_rasters.terra_dom_source(tmp_path / "dom.tif")
    destination = tmp_path / "master.tif"
    package_master(source, destination)

    with rasterio.open(source) as src, rasterio.open(destination) as dst:
        assert src.checksum(4) == dst.checksum(4)


def test_declaring_a_vertical_crs_changes_metadata_only(tmp_path: Path) -> None:
    """Buduunkhad's vertical reference lives in a document, not in the file header."""
    source = make_rasters.terra_dom_source(tmp_path / "dom.tif")
    destination = tmp_path / "master.tif"

    result = package_master(source, destination, plan=PackagingPlan(declare_crs="EPSG:32647+5705"))

    assert result.description.crs == "EPSG:32647+5705"
    assert result.record.grid_preserved is True
    assert result.record.grid_out.transform == result.record.grid_in.transform
    assert any(op.name == "crs" for op in result.record.operations)
    with rasterio.open(source) as src, rasterio.open(destination) as dst:
        assert [src.checksum(b) for b in (1, 2, 3)] == [dst.checksum(b) for b in (1, 2, 3)]


def test_declaring_a_different_horizontal_crs_is_refused(tmp_path: Path) -> None:
    """Adding a vertical reference is allowed; silently relocating the pixels is not."""
    source = make_rasters.terra_dom_source(tmp_path / "dom.tif")

    with pytest.raises(PackagingError, match="different horizontal component"):
        package_master(
            source, tmp_path / "master.tif", plan=PackagingPlan(declare_crs="EPSG:32648+5705")
        )


def test_striped_lzw_source_still_produces_a_conforming_master(tmp_path: Path) -> None:
    """The engine's own encoding choices must not leak into the product."""
    source = make_rasters.rgba_source(
        tmp_path / "source.tif", compress="lzw", tiled=False, bigtiff="NO"
    )
    result = package_master(source, tmp_path / "master.tif")

    assert result.description.compression == "DEFLATE"
    assert result.description.tiled is True
    assert result.description.is_bigtiff is True
