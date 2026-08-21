"""One browsable image of a whole project, assembled from destriped block renders.

The virtual mosaic addresses the masters at full resolution and is the right thing for
inspection, but it is 97 gigapixels and needs a pyramid before a viewer can open it. This is
the opposite trade: a single small raster, destriped, that opens instantly and shows the survey
as one picture.

Two choices worth stating:

* **Overlaps are averaged, not overwritten.** The blocks have been harmonised, so where two of
  them image the same ground their values agree to about 7.5%; averaging uses both and softens
  the join. Taking whichever block was written last would put a hard edge along every seam and
  discard half the data.
* **Destriping happens per block, before assembly.** Banding is a per-block property with a
  per-block orientation -- 42 of the 79 Buduunkhad blocks band vertically and 37 horizontally --
  so correcting it after assembly would mean fitting a mixture of two orientations at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from numpy.typing import NDArray
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject

from .destripe import DEFAULT_TREND_WINDOW, destripe
from .preview import PreviewError, composite_on_white

# The most an automatically chosen resolution will put on the overview's long edge. Large
# enough that a screen-filling view of a whole survey still has detail to zoom into, small
# enough to open at once: 10,000 px is 63 megapixels on a survey shaped like Buduunkhad, about
# 100 MB. A cap rather than a target, because the failure worth avoiding is a browse image too
# big to browse.
MAX_LONG_EDGE_PIXELS = 10_000

_MASTER_CREATION = {
    "driver": "GTiff",
    "compress": "DEFLATE",
    "predictor": 2,
    "tiled": True,
    "blockxsize": 512,
    "blockysize": 512,
    "num_threads": "ALL_CPUS",
}


@dataclass(frozen=True)
class Overview:
    path: Path
    width: int
    height: int
    gsd: float
    blocks: int
    destriped: int
    mean_ripple_reduction_pct: float
    bytes_written: int


def choose_gsd(
    longest_extent_m: float,
    *,
    finest_native: float,
    max_pixels: int = MAX_LONG_EDGE_PIXELS,
) -> float:
    """A browsable resolution for a survey of this size.

    Exists so that a new area needs no number chosen for it, and no fixed value can do that
    job: measured on the real deliveries, 0.5 m gives Buduunkhad 19531 x 12869 px and 404 MB,
    while the same 0.5 m over a 350 ha P1 block would give under 4000 px. Both are the wrong
    product for opposite reasons.

    The coarsest of the candidates that still fits, rather than the closest: overshooting the
    cap is the failure that matters, since a browse image too large to open is not a browse
    image. Snapped to a 1-2-5 ladder because these values end up in filenames, reports and
    conversations, where "0.977 m" invites a question about where it came from. Never finer
    than the finest master present: past that it is upsampling, which adds bytes and no detail.
    """
    if longest_extent_m <= 0:
        raise PreviewError(f"a survey {longest_extent_m} m across has no extent to render")

    coarsest_needed = longest_extent_m / max(1, max_pixels)
    ladder = sorted(
        multiple * 10.0**exponent for exponent in range(-4, 6) for multiple in (1.0, 2.0, 5.0)
    )
    fitting = next((step for step in ladder if step >= coarsest_needed), ladder[-1])
    return max(fitting, finest_native)


def _read_for_overview(
    master: Path, gsd: float
) -> tuple[NDArray[np.uint8], NDArray[np.uint8], object]:
    """Read one master down to roughly the overview grid, keeping alpha and georeferencing."""
    with rasterio.open(master) as ds:
        if ds.count < 4:
            raise PreviewError(f"{master.name} has {ds.count} bands; a master carries 4")
        scale = max(1.0, gsd / float(ds.transform.a))
        width = max(1, int(ds.width / scale))
        height = max(1, int(ds.height / scale))
        rgb = ds.read([1, 2, 3], out_shape=(3, height, width), resampling=Resampling.average)
        alpha = ds.read(4, out_shape=(height, width), resampling=Resampling.average)
        transform = ds.transform * ds.transform.scale(ds.width / width, ds.height / height)
    return rgb, alpha, transform


def build_overview(
    masters: list[Path],
    destination: Path,
    *,
    gsd: float | None = None,
    apply_destripe: bool = True,
    window: int = DEFAULT_TREND_WINDOW,
    progress: object = None,
) -> Overview:
    """Assemble a destriped, browsable overview of every master in a project."""
    if not masters:
        raise PreviewError("no masters given; an overview of nothing is not a product")

    bounds = []
    native: list[float] = []
    seen: dict[str, Path] = {}
    crs = None
    for master in masters:
        with rasterio.open(master) as ds:
            bounds.append(ds.bounds)
            native.append(abs(float(ds.transform.a)))
            crs = crs or ds.crs
            seen.setdefault(str(ds.crs), master)

    # Refused here rather than left to the caller. Taking the first block's CRS and pooling
    # everything else's bounds into it places a zone-49 block by its raw easting on a zone-47
    # grid: hundreds of kilometres out, and the result is a picture rather than an error, so
    # nothing downstream would question it.
    if len(seen) > 1:
        described = ", ".join(f"{name} ({path.name})" for name, path in sorted(seen.items()))
        raise PreviewError(
            f"masters declare {len(seen)} different coordinate reference systems: {described}. "
            "An overview of two reference systems would place one of them by numbers that mean "
            "something else; give each its own project id"
        )

    left = min(b.left for b in bounds)
    right = max(b.right for b in bounds)
    bottom = min(b.bottom for b in bounds)
    top = max(b.top for b in bounds)

    if gsd is None:
        gsd = choose_gsd(max(right - left, top - bottom), finest_native=min(native))

    width = max(1, round((right - left) / gsd))
    height = max(1, round((top - bottom) / gsd))
    canvas_transform = from_origin(left, top, gsd, gsd)

    # Accumulate a weighted sum so overlapping blocks average rather than overwrite.
    total = np.zeros((3, height, width), dtype=np.float32)
    weight = np.zeros((height, width), dtype=np.float32)

    destriped = 0
    reductions: list[float] = []
    for index, master in enumerate(masters, start=1):
        rgb, alpha, source_transform = _read_for_overview(master, gsd)

        if apply_destripe:
            rgb, result = destripe(rgb, alpha, window=window)
            if result.ripple_before_pct > 0:
                destriped += 1
                reductions.append(result.reduction_pct)
        else:
            result = None

        placed = np.zeros((3, height, width), dtype=np.float32)
        placed_alpha = np.zeros((height, width), dtype=np.float32)
        reproject(
            rgb,
            placed,
            src_transform=source_transform,
            src_crs=crs,
            dst_transform=canvas_transform,
            dst_crs=crs,
            resampling=Resampling.average,
        )
        reproject(
            alpha,
            placed_alpha,
            src_transform=source_transform,
            src_crs=crs,
            dst_transform=canvas_transform,
            dst_crs=crs,
            resampling=Resampling.average,
        )

        coverage = np.clip(placed_alpha / 255.0, 0.0, 1.0)
        total += placed * coverage[None, :, :]
        weight += coverage

        if callable(progress):
            progress(index, len(masters), master, result)

    covered = weight > 0.004
    rgb_out = np.zeros((3, height, width), dtype=np.uint8)
    for band in range(3):
        values = np.zeros((height, width), dtype=np.float32)
        np.divide(total[band], weight, out=values, where=covered)
        rgb_out[band] = np.clip(np.rint(values), 0, 255).astype(np.uint8)
    alpha_out = np.where(covered, 255, 0).astype(np.uint8)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        destination,
        "w",
        width=width,
        height=height,
        count=4,
        dtype="uint8",
        crs=crs,
        transform=canvas_transform,
        nodata=None,
        photometric="RGB",
        alpha="NON-PREMULTIPLIED",
        **_MASTER_CREATION,
    ) as dst:
        dst.write(rgb_out, indexes=[1, 2, 3])
        dst.write(alpha_out, 4)
        dst.build_overviews([2, 4, 8, 16], Resampling.average)
        dst.update_tags(
            note="derived viewing overview; destriped, resampled, not a master",
            destriped=str(apply_destripe),
        )

    return Overview(
        path=destination,
        width=width,
        height=height,
        gsd=gsd,
        blocks=len(masters),
        destriped=destriped,
        mean_ripple_reduction_pct=float(np.mean(reductions)) if reductions else 0.0,
        bytes_written=destination.stat().st_size,
    )


def write_overview_jpeg(overview: Path, destination: Path, *, quality: int = 88) -> Path:
    """A flat JPEG of the overview, for anything that cannot open a GeoTIFF."""
    from PIL import Image

    with rasterio.open(overview) as ds:
        rgb = ds.read([1, 2, 3])
        alpha = ds.read(4)

    flattened = composite_on_white(rgb, alpha)
    image = Image.fromarray(np.ascontiguousarray(flattened.transpose(1, 2, 0)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, "JPEG", quality=quality, optimize=True, progressive=True)
    return destination
