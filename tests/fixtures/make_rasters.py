"""Deterministic synthetic rasters for the QA and packaging tests.

Fixtures are generated, not committed. A committed binary that is supposed to assert "this
file is striped, not tiled" cannot be reviewed; the few lines that build it can. It also
keeps the repository free of blobs.

Every raster here is 64x48 at 1.7 cm, a real block resolution, so that the native-resolution
rules are exercised on a value that does not round to anything tidy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from numpy.typing import NDArray
from rasterio.enums import Resampling
from rasterio.transform import from_origin

WIDTH = 64
HEIGHT = 48
CRS = "EPSG:32648"
PIXEL_SIZE = 0.017
TRANSFORM = from_origin(500000.0, 5000000.0, PIXEL_SIZE, PIXEL_SIZE)

# Whether a GeoTIFF's fourth band counts as alpha is decided by the TIFF ExtraSamples tag,
# which the GTiff driver writes at creation time. Measured against GDAL 3.12.4:
#
#   no creation options                     -> band 4 is alpha (the driver's default)
#   PHOTOMETRIC=RGB alone                   -> band 4 is undefined
#   PHOTOMETRIC=RGB + ALPHA=NON-PREMULTIPLIED -> band 4 is alpha
#   PHOTOMETRIC=RGB + ALPHA=UNSPECIFIED     -> band 4 is undefined
#
# Assigning colorinterp to an already-created dataset does NOT survive when PHOTOMETRIC=RGB
# was given without an ALPHA option: the assignment is accepted and then silently lost on
# reopen. So these fixtures control alpha through the creation option only, never after the
# fact, which is also what the packager does.


def gradient(count: int, dtype: str = "uint8") -> NDArray[Any]:
    rows = np.arange(HEIGHT, dtype=np.uint32)[:, None]
    cols = np.arange(WIDTH, dtype=np.uint32)[None, :]
    base = (rows * 3 + cols * 5) % 251
    return np.stack([((base + band * 7) % 251).astype(dtype) for band in range(count)])


def border_mask(border: int = 4) -> NDArray[np.uint8]:
    """Valid in the middle, invalid around the edge, like a real cropped orthophoto."""
    mask = np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)
    mask[:border, :] = 0
    mask[-border:, :] = 0
    mask[:, :border] = 0
    mask[:, -border:] = 0
    return mask


def write_raster(
    path: Path,
    *,
    count: int = 3,
    dtype: str = "uint8",
    data: NDArray[Any] | None = None,
    crs: str | None = CRS,
    transform: Any = TRANSFORM,
    nodata: float | None = None,
    alpha: str | None = None,
    mask: NDArray[np.uint8] | None = None,
    overviews: list[int] | None = None,
    compress: str | None = "deflate",
    predictor: int | None = 2,
    tiled: bool = True,
    blocksize: int = 32,
    bigtiff: str = "YES",
    photometric: str | None = None,
) -> Path:
    # Dimensions follow the data when it is supplied, so a fixture can be any size without
    # having to restate its shape.
    if data is not None:
        count = int(data.shape[0])
        height, width = int(data.shape[1]), int(data.shape[2])
    else:
        width, height = WIDTH, HEIGHT

    profile: dict[str, Any] = {
        "driver": "GTiff",
        "width": width,
        "height": height,
        "count": count,
        "dtype": dtype,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
        "bigtiff": bigtiff,
    }
    if compress is not None:
        profile["compress"] = compress
        if predictor is not None and compress.lower() in {"deflate", "lzw"}:
            profile["predictor"] = predictor
    if tiled:
        profile.update(tiled=True, blockxsize=blocksize, blockysize=blocksize)
    else:
        profile["tiled"] = False
    if photometric is not None:
        profile["photometric"] = photometric
    if alpha is not None:
        profile["alpha"] = alpha

    payload = data if data is not None else gradient(count, dtype)
    with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(path, "w", **profile) as dst:
        dst.write(payload)
        if mask is not None:
            dst.write_mask(mask)
        if overviews:
            dst.build_overviews(overviews, Resampling.average)
    return path


def rgba_source(path: Path, **overrides: Any) -> Path:
    """Four bands with band 4 genuinely tagged alpha — what an ODM orthophoto looks like."""
    data = np.concatenate([gradient(3), border_mask()[None, :, :]])
    options: dict[str, Any] = {
        "count": 4,
        "data": data,
        "photometric": "RGB",
        "alpha": "NON-PREMULTIPLIED",
    }
    options.update(overrides)
    return write_raster(path, **options)


def terra_dom_source(path: Path, **overrides: Any) -> Path:
    """A raster shaped like a real DJI Terra DOM.

    Measured from the Buduunkhad delivery, where all 79 zones share one signature: four
    bands with band 4 correctly tagged alpha, DEFLATE, tiled 256x256, BigTIFF, no overviews,
    EPSG:32647 — and a NoData of 0 set on all four bands at the same time.

    That redundant NoData is the only thing standing between a Terra export and the master
    contract, and it is why this fixture exists: alpha must win, and the NoData must be
    dropped rather than used.
    """
    options: dict[str, Any] = {"nodata": 0, "blocksize": 256, "crs": "EPSG:32647"}
    options.update(overrides)
    return rgba_source(path, **options)


def four_band_untagged_source(path: Path) -> Path:
    """Four bands where the fourth carries validity but is not declared alpha."""
    data = np.concatenate([gradient(3), border_mask()[None, :, :]])
    return write_raster(path, count=4, data=data, photometric="RGB", alpha="UNSPECIFIED")


def masked_rgb_source(path: Path) -> Path:
    """Three bands carrying an internal GDAL mask band."""
    return write_raster(path, count=3, mask=border_mask())


def nodata_rgb_source(path: Path) -> Path:
    """Three bands whose only validity signal is NoData=0 — the ambiguous case."""
    data = gradient(3)
    invalid = border_mask() == 0
    data[:, invalid] = 0
    return write_raster(path, count=3, data=data, nodata=0)


def bare_rgb_source(path: Path) -> Path:
    """Three bands with no alpha, no mask and no NoData: validity cannot be established."""
    return write_raster(path, count=3)


def no_crs_source(path: Path) -> Path:
    return rgba_source(path, crs=None)


def georeferenced_pattern(
    path: Path,
    *,
    origin_x: float,
    origin_y: float,
    width: int,
    height: int,
    pixel: float,
    gain: float = 1.0,
    offset: float = 0.0,
    crs: str | None = CRS,
    valid_border: int = 0,
) -> Path:
    """A raster whose pixel values are a function of world position.

    Two of these agree exactly on the ground they share, whatever their origins or pixel
    sizes, so a radiometric comparison between them measures only the gain and offset
    applied here. That is what makes a known answer possible.
    """
    transform = from_origin(origin_x, origin_y, pixel, pixel)
    cols = np.arange(width, dtype=np.float64)
    rows = np.arange(height, dtype=np.float64)
    xs = origin_x + (cols + 0.5) * pixel
    ys = origin_y - (rows + 0.5) * pixel
    grid_x, grid_y = np.meshgrid(xs, ys)

    base = 120.0 + 40.0 * np.sin(grid_x / 3.0) + 30.0 * np.cos(grid_y / 2.5)
    bands = [base, base * 0.90, base * 0.80]
    data = np.stack(
        [np.clip(b * gain + offset, 0, 255).astype("uint8") for b in bands]
        + [np.full((height, width), 255, dtype="uint8")]
    )
    if valid_border:
        data[3, :valid_border, :] = 0
        data[3, -valid_border:, :] = 0
        data[3, :, :valid_border] = 0
        data[3, :, -valid_border:] = 0

    return write_raster(
        path,
        count=4,
        data=data,
        crs=crs,
        transform=transform,
        photometric="RGB",
        alpha="NON-PREMULTIPLIED",
    )


def overlapping_pair(
    directory: Path,
    *,
    gain: float = 1.0,
    offset: float = 0.0,
    shift_m: float = 20.0,
    pixel_a: float = 0.05,
    pixel_b: float = 0.05,
    crs_b: str | None = CRS,
    valid_border: int = 0,
) -> tuple[Path, Path]:
    """Two blocks sharing ground, where B differs from A by a known gain and offset."""
    a_dir, b_dir = directory / "A", directory / "B"
    a_dir.mkdir(parents=True, exist_ok=True)
    b_dir.mkdir(parents=True, exist_ok=True)
    span = 40.0
    a = georeferenced_pattern(
        a_dir / "dom.tif",
        origin_x=500000.0,
        origin_y=5000000.0,
        width=int(span / pixel_a),
        height=int(span / pixel_a),
        pixel=pixel_a,
        valid_border=valid_border,
    )
    b = georeferenced_pattern(
        b_dir / "dom.tif",
        origin_x=500000.0 + shift_m,
        origin_y=5000000.0,
        width=int(span / pixel_b),
        height=int(span / pixel_b),
        pixel=pixel_b,
        gain=gain,
        offset=offset,
        crs=crs_b,
        valid_border=valid_border,
    )
    return a, b


def conforming_master(path: Path, **overrides: Any) -> Path:
    """A raster that already satisfies the master contract.

    Used to test that QA passes what it should. Deliberate violations are produced by
    overriding one property at a time.
    """
    return rgba_source(path, **overrides)
