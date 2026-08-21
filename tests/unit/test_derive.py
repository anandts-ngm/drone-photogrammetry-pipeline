"""Derived products: the virtual mosaic and the previews.

The mosaic tests pin three things that are silent when wrong: that the grid is the finest
native size rather than an average, that every source composites through its alpha instead of
overwriting, and that paths stay relative so the tree can be moved.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

from drone_photogrammetry_pipeline.derive.mosaic import (
    MosaicError,
    add_overviews,
    build_mosaic,
    overview_factors,
    read_sources,
)
from drone_photogrammetry_pipeline.derive.preview import (
    PreviewError,
    composite_on_white,
    render,
    write_contact_sheet,
    write_preview,
)


def write_master(
    path: Path,
    *,
    origin_x: float = 500_000.0,
    origin_y: float = 5_000_000.0,
    pixel: float = 0.05,
    size: int = 256,
    value: int = 140,
    crs: str = "EPSG:32647",
    alpha_value: int = 255,
) -> Path:
    profile = {
        "driver": "GTiff",
        "width": size,
        "height": size,
        "count": 4,
        "dtype": "uint8",
        "crs": crs,
        "transform": from_origin(origin_x, origin_y, pixel, pixel),
        "nodata": None,
        "tiled": True,
        "blockxsize": 128,
        "blockysize": 128,
        "photometric": "RGB",
        "alpha": "NON-PREMULTIPLIED",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.colorinterp = (
            ColorInterp.red,
            ColorInterp.green,
            ColorInterp.blue,
            ColorInterp.alpha,
        )
        dst.write(np.full((3, size, size), value, dtype=np.uint8), indexes=[1, 2, 3])
        dst.write(np.full((size, size), alpha_value, dtype=np.uint8), 4)
    return path


def test_the_grid_is_the_finest_native_pixel_size(tmp_path: Path) -> None:
    """Choosing anything coarser would discard detail the finest blocks really have."""
    fine = write_master(tmp_path / "fine.tif", pixel=0.025, origin_x=500_000.0)
    coarse = write_master(tmp_path / "coarse.tif", pixel=0.05, origin_x=500_020.0)

    built = build_mosaic([coarse, fine], tmp_path / "m.vrt")

    assert built.pixel_size == pytest.approx(0.025)
    with rasterio.open(built.path) as ds:
        assert ds.transform.a == pytest.approx(0.025)


def test_every_source_composites_through_its_alpha(tmp_path: Path) -> None:
    """Without this, interlocking footprints lose ground to whichever block is written last."""
    a = write_master(tmp_path / "a.tif", origin_x=500_000.0)
    b = write_master(tmp_path / "b.tif", origin_x=500_005.0)

    built = build_mosaic([a, b], tmp_path / "m.vrt")

    root = ET.parse(built.path).getroot()
    sources = root.findall(".//ComplexSource")
    assert len(sources) == 8, "four bands times two sources"
    assert all(s.findtext("UseMaskBand") == "true" for s in sources)


def test_a_transparent_block_does_not_erase_the_one_beneath_it(tmp_path: Path) -> None:
    """The behaviour UseMaskBand buys, asserted on pixels rather than on the XML."""
    solid = write_master(tmp_path / "solid.tif", origin_x=500_000.0, value=200, alpha_value=255)
    empty = write_master(tmp_path / "empty.tif", origin_x=500_000.0, value=0, alpha_value=0)

    # `empty` is listed last, so a naive mosaic would show its transparent pixels on top.
    built = build_mosaic([solid, empty], tmp_path / "m.vrt")

    with rasterio.open(built.path) as ds:
        assert int(ds.read(4).max()) == 255
        assert int(np.median(ds.read(1))) == 200


def test_paths_are_relative_across_sibling_directories(tmp_path: Path) -> None:
    """The real layout: masters under `B*/runs/...`, the VRT under `derived/`.

    These are siblings, so the relative path needs `..`. `Path.relative_to` refuses to
    produce that and silently sent every path down the absolute fallback, which defeats the
    portability the relative form exists for.
    """
    master = write_master(tmp_path / "B1" / "runs" / "r1" / "master" / "B1_ORTHO_MASTER.tif")

    built = build_mosaic([master], tmp_path / "derived" / "m.vrt")

    names = ET.parse(built.path).getroot().findall(".//SourceFilename")
    assert names
    for name in names:
        assert name.get("relativeToVRT") == "1"
        assert name.text is not None
        assert not Path(name.text).is_absolute()
        assert name.text.startswith("../"), "a sibling path must climb out of derived/"


def test_a_relative_mosaic_still_opens_after_the_tree_is_moved(tmp_path: Path) -> None:
    """The property the relative form buys, asserted by actually moving the tree."""
    import shutil

    original = tmp_path / "before"
    master = write_master(original / "B1" / "runs" / "r1" / "master" / "B1_ORTHO_MASTER.tif")
    build_mosaic([master], original / "derived" / "m.vrt")

    moved = tmp_path / "after"
    shutil.move(str(original), str(moved))

    with rasterio.open(moved / "derived" / "m.vrt") as ds:
        assert int(ds.read(4).max()) == 255


def test_mixed_reference_systems_are_refused(tmp_path: Path) -> None:
    a = write_master(tmp_path / "a.tif", crs="EPSG:32647")
    b = write_master(tmp_path / "b.tif", crs="EPSG:32648", origin_x=200_000.0)

    with pytest.raises(MosaicError, match="coordinate reference systems"):
        build_mosaic([a, b], tmp_path / "m.vrt")


def test_an_empty_project_is_an_error_not_an_empty_mosaic(tmp_path: Path) -> None:
    with pytest.raises(MosaicError):
        read_sources([])


def test_the_mosaic_covers_the_union_of_its_sources(tmp_path: Path) -> None:
    a = write_master(tmp_path / "a.tif", origin_x=500_000.0, pixel=0.05, size=200)
    b = write_master(tmp_path / "b.tif", origin_x=500_015.0, pixel=0.05, size=200)

    built = build_mosaic([a, b], tmp_path / "m.vrt")

    with rasterio.open(built.path) as ds:
        assert ds.bounds.left == pytest.approx(500_000.0)
        assert ds.bounds.right == pytest.approx(500_025.0)


def test_overview_factors_start_coarse_and_stop_at_a_thumbnail() -> None:
    """Levels 2 and 4 of a 97-gigapixel mosaic are most of the storage for no benefit."""
    factors = overview_factors(384_471, 253_326)

    assert factors[0] == 8
    assert 2 not in factors and 4 not in factors
    # The last level must still be big enough to look at, and small enough to be instant.
    assert 256 <= 384_471 // factors[-1] < 512


def test_a_small_mosaic_gets_no_overviews_finer_than_itself() -> None:
    assert overview_factors(400, 300) == []
    assert overview_factors(4000, 3000) == [8]


def test_overviews_make_a_full_extent_read_fast(tmp_path: Path) -> None:
    """The property this exists for.

    Without a pyramid, a zoomed-out read has to touch every pixel of every source; on the real
    survey a 200x300 read of the full extent did not complete in ten minutes.
    """
    masters = [
        write_master(tmp_path / f"b{i}.tif", origin_x=500_000.0 + i * 100.0, size=2600)
        for i in range(2)
    ]
    built = build_mosaic(masters, tmp_path / "m.vrt")

    factors = overview_factors(built.width, built.height)
    assert factors, "fixture must be big enough to need overviews"
    present = add_overviews(built.path, factors)

    assert present == factors
    assert built.path.with_suffix(".vrt.ovr").is_file()

    with rasterio.open(built.path) as ds:
        assert ds.overviews(1) == factors
        # Reading the whole extent small now comes from the pyramid, not from the sources.
        assert ds.read(1, out_shape=(64, 64)).mean() > 0


def test_overviews_are_lossless(tmp_path: Path) -> None:
    """A lossy pyramid changes what any reduced-resolution read returns, measurements included.

    That is precisely how another pipeline's viewing overviews corrupted this project's overlap
    statistics, so the compression here is asserted rather than assumed.
    """
    master = write_master(tmp_path / "b.tif", size=2600, value=137)
    built = build_mosaic([master], tmp_path / "m.vrt")
    add_overviews(built.path, overview_factors(built.width, built.height))

    with rasterio.open(built.path, "r") as ds:
        # A constant-valued source must survive averaging exactly; lossy coding would not.
        assert int(ds.read(1, out_shape=(64, 64)).min()) == 137
        assert int(ds.read(1, out_shape=(64, 64)).max()) == 137


def test_building_no_factors_is_a_no_op(tmp_path: Path) -> None:
    master = write_master(tmp_path / "b.tif", size=256)
    built = build_mosaic([master], tmp_path / "m.vrt")

    assert add_overviews(built.path, []) == []
    assert not built.path.with_suffix(".vrt.ovr").exists()


def test_alpha_flattens_onto_white_not_black() -> None:
    """Invalid ground should read as background; black would look like real dark terrain."""
    rgb = np.full((3, 4, 4), 120, dtype=np.uint8)
    alpha = np.zeros((4, 4), dtype=np.uint8)

    assert composite_on_white(rgb, alpha).min() == 255

    half = np.full((4, 4), 128, dtype=np.uint8)
    blended = composite_on_white(rgb, half)
    assert 180 < int(blended[0, 0, 0]) < 200


def test_a_preview_keeps_the_aspect_ratio(tmp_path: Path) -> None:
    master = write_master(tmp_path / "a.tif", size=256)

    image, destriped = render(master, longest_side=64)

    assert (image.width, image.height) == (64, 64)
    assert destriped is None, "destriping is opt-in, never the default for a bare render"


def test_a_wide_master_is_fitted_on_its_long_edge(tmp_path: Path) -> None:
    path = tmp_path / "wide.tif"
    profile = {
        "driver": "GTiff",
        "width": 400,
        "height": 100,
        "count": 4,
        "dtype": "uint8",
        "crs": "EPSG:32647",
        "transform": from_origin(500_000.0, 5_000_000.0, 0.05, 0.05),
        "nodata": None,
        "photometric": "RGB",
        "alpha": "NON-PREMULTIPLIED",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.colorinterp = (
            ColorInterp.red,
            ColorInterp.green,
            ColorInterp.blue,
            ColorInterp.alpha,
        )
        dst.write(np.full((3, 100, 400), 90, dtype=np.uint8), indexes=[1, 2, 3])
        dst.write(np.full((100, 400), 255, dtype=np.uint8), 4)

    image, _ = render(path, longest_side=200)

    assert (image.width, image.height) == (200, 50)


def test_a_three_band_raster_is_refused_as_a_master(tmp_path: Path) -> None:
    path = tmp_path / "rgb.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=32,
        height=32,
        count=3,
        dtype="uint8",
        crs="EPSG:32647",
        transform=from_origin(500_000.0, 5_000_000.0, 0.05, 0.05),
    ) as dst:
        dst.write(np.zeros((3, 32, 32), dtype=np.uint8))

    with pytest.raises(PreviewError, match="4"):
        render(path)


def test_a_preview_can_be_destriped_on_request(tmp_path: Path) -> None:
    """Opt-in, and it reports what it removed rather than doing it silently."""
    master = write_master(tmp_path / "b.tif", size=600)

    _, result = render(master, longest_side=256, apply_destripe=True)

    assert result is not None
    assert result.ripple_before_pct >= 0.0


def test_preview_and_contact_sheet_are_written(tmp_path: Path) -> None:
    masters = [write_master(tmp_path / f"b{i}.tif", origin_x=500_000.0 + i * 20) for i in range(3)]

    rendered = []
    for index, master in enumerate(masters):
        preview, image, _ = write_preview(
            master, tmp_path / "previews" / f"B{index}_preview.jpg", longest_side=96
        )
        assert preview.path.is_file() and preview.bytes_written > 0
        rendered.append((f"B{index}", image))

    sheet = write_contact_sheet(rendered, tmp_path / "sheet.jpg", columns=2, thumbnail=64)

    assert sheet.path.is_file()
    assert sheet.width == 128, "two columns of 64"
    assert sheet.bytes_written > 0


def test_a_contact_sheet_of_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PreviewError):
        write_contact_sheet([], tmp_path / "sheet.jpg")
