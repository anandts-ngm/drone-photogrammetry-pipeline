"""Overlap measurement must not change when someone adds overviews.

On 2026-08-19 a separate pipeline ran `gdaladdo -ro` across the delivered blocks to make them
pleasant to pan in QGIS. It changed no pixel, but it changed this measurement: asking GDAL for
a reduced `out_shape` is served from the overview pyramid once one exists, and averaged alpha
at reduced resolution falls below the fully-valid threshold. Measured pairs silently dropped
from 232 to 208 and the solve covered 75 blocks instead of 79, reported as an ordinary
geometric outcome with no error anywhere.

Reading at full resolution and averaging in-process makes the answer a property of the
imagery rather than of what optimisations happen to sit beside it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import from_origin

from drone_photogrammetry_pipeline.qa.radiometry import compare_pair, read_footprints


def write_block(path: Path, *, origin_x: float, value: int) -> Path:
    """A small four-band block whose alpha has ragged edges, as a real delivery does."""
    size = 512
    rgb = np.full((3, size, size), value, dtype=np.uint8)
    # Vary the interior so averaging is not trivially constant.
    rgb[:, ::7, :] = max(value - 20, 0)

    alpha = np.full((size, size), 255, dtype=np.uint8)
    alpha[:40, :] = 0
    alpha[-40:, :] = 0
    alpha[:, :40] = 0
    alpha[:, -40:] = 0

    profile = {
        "driver": "GTiff",
        "width": size,
        "height": size,
        "count": 4,
        "dtype": "uint8",
        "crs": "EPSG:32647",
        "transform": from_origin(origin_x, 5_000_000.0, 0.05, 0.05),
        "nodata": None,
        "tiled": True,
        "blockxsize": 128,
        "blockysize": 128,
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
        dst.write(rgb, indexes=[1, 2, 3])
        dst.write(alpha, 4)
    return path


def measure(a: Path, b: Path) -> tuple[int, int, list[float]]:
    footprints = read_footprints([("A", a), ("B", b)])
    result = compare_pair(footprints[0], footprints[1])
    return (
        result.sample_count,
        result.sample_pixels,
        [band.median_a for band in result.bands],
    )


def test_adding_overviews_does_not_change_the_measurement(tmp_path: Path) -> None:
    a = write_block(tmp_path / "a.tif", origin_x=500_000.0, value=140)
    b = write_block(tmp_path / "b.tif", origin_x=500_012.0, value=170)

    before = measure(a, b)
    assert before[1] > 0, "fixture must overlap on valid ground or the test proves nothing"

    for path in (a, b):
        with rasterio.open(path, "r+") as ds:
            ds.build_overviews([2, 4, 8], Resampling.average)

    assert (measure(a, b)) == before


def test_the_fixture_actually_gained_overviews(tmp_path: Path) -> None:
    """Guards the test above: if the build silently no-ops, it would pass for free."""
    path = write_block(tmp_path / "c.tif", origin_x=500_000.0, value=140)

    with rasterio.open(path) as ds:
        assert ds.overviews(1) == []

    with rasterio.open(path, "r+") as ds:
        ds.build_overviews([2, 4, 8], Resampling.average)
    with rasterio.open(path) as ds:
        assert ds.overviews(1) == [2, 4, 8]
