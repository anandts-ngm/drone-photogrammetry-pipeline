"""Radiometric overlap measurement.

Measures how much two blocks disagree about ground they both cover. This is the input to
any harmonisation: per-block gains and offsets are solved from these numbers, never chosen.

Method, and why it is built this way:

* **Ground patches, not pixel pairs.** Two independently processed orthophotos are not
  co-registered to the pixel, so a per-pixel difference would mix a radiometric signal with
  a geometric one. Instead a square of ground is read from both blocks and averaged down to
  the same small grid, which compares the same ground without assuming the pixels line up.
* **Different native resolutions are handled by construction.** Each patch is averaged to a
  fixed output size, so a 1.6 cm block and a 5 cm block contribute comparably. The
  resampling happens here, on temporary data; no master is ever touched.
* **Only ground valid in both blocks counts.** Alpha is read alongside the colour bands and
  a sample is used only where both blocks are fully valid, so block edges and their
  partially transparent pixels cannot contaminate the comparison.
* **Robust statistics.** Medians throughout. A handful of samples on shadow, water or a
  vehicle should not move the answer, and with hundreds of pairs the outliers are certain
  to be there.

No pass threshold is applied. Every pair is reported as NOT_EVALUATED until thresholds are
derived from the benchmark cases, which is a measurement exercise rather than a judgement
call — see docs/radiometry.md.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from numpy.typing import NDArray
from rasterio.enums import Resampling
from rasterio.windows import from_bounds

from ..colour import dn_to_linear
from ..models.qa import BandDifference, RadiometricOverlapReport, RadiometricPairResult

BAND_NAMES = ("red", "green", "blue")
PATCH_GRID = 32
DEFAULT_PATCHES = 48
DEFAULT_PATCH_METRES = 4.0

# Percentile levels at which the two blocks' distributions are matched. Spread across the
# range rather than clustered at the middle, because the question a single median cannot
# answer is whether the blocks differ by a scale factor or by a black level, and only the
# dark end distinguishes those two.
QQ_LEVELS = (5.0, 10.0, 25.0, 50.0, 75.0, 90.0, 95.0)

# After averaging, a patch edge that clipped the block boundary has fractional alpha. Only
# fully covered ground is compared.
_ALPHA_VALID = 250

# Below this combined brightness the symmetric ratio becomes numerically meaningless, so
# those samples are excluded from the robust statistic only.
_MIN_COMBINED_DN = 20.0


class RadiometryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Footprint:
    block_id: str
    path: Path
    left: float
    bottom: float
    right: float
    top: float
    crs: str | None
    pixel_size: float


def read_footprints(blocks: list[tuple[str, Path]]) -> list[Footprint]:
    found = []
    for block_id, path in blocks:
        with rasterio.open(path) as ds:
            bounds = ds.bounds
            found.append(
                Footprint(
                    block_id=block_id,
                    path=path,
                    left=float(bounds.left),
                    bottom=float(bounds.bottom),
                    right=float(bounds.right),
                    top=float(bounds.top),
                    crs=ds.crs.to_string() if ds.crs else None,
                    pixel_size=float(ds.transform.a),
                )
            )
    return found


def intersection(a: Footprint, b: Footprint) -> tuple[float, float, float, float] | None:
    left, bottom = max(a.left, b.left), max(a.bottom, b.bottom)
    right, top = min(a.right, b.right), min(a.top, b.top)
    if right <= left or top <= bottom:
        return None
    return (left, bottom, right, top)


def _patch_origins(
    box: tuple[float, float, float, float], count: int, patch_metres: float
) -> Iterator[tuple[float, float]]:
    """Spread patches over the overlap on a near-square grid.

    Spatial spread matters more than raw pixel count: neighbouring pixels are highly
    correlated, so many patches over the whole overlap beat a few large ones in a corner.
    """
    left, bottom, right, top = box
    width, height = right - left - patch_metres, top - bottom - patch_metres
    if width <= 0 or height <= 0:
        return
    columns = max(1, round((count * width / height) ** 0.5))
    rows = max(1, -(-count // columns))
    for row in range(rows):
        for column in range(columns):
            yield (
                left + width * (column + 0.5) / columns,
                bottom + height * (row + 0.5) / rows,
            )


def _bin_average(data: NDArray[Any], size: int) -> NDArray[np.float64]:
    """Average a patch down to `size` x `size` by block mean, in full.

    Done here rather than by asking GDAL for a smaller `out_shape`, because that request is
    served from the overview pyramid when one exists. A measurement that changes when someone
    adds pyramids for viewing is not a measurement; this keeps the answer a property of the
    imagery alone.
    """
    bands, height, width = data.shape
    rows, columns = height // size, width // size
    trimmed = data[:, : rows * size, : columns * size]
    return trimmed.reshape(bands, size, rows, size, columns).mean(axis=(2, 4)).astype(np.float64)


def _read_patch(dataset: Any, box: tuple[float, float, float, float]) -> NDArray[Any] | None:
    window = from_bounds(*box, transform=dataset.transform)
    if window.width < 1 or window.height < 1:
        return None

    # Full-resolution read. A ground patch is a few metres across, so even at 2.5 cm this is
    # a couple of hundred pixels a side -- cheap enough that avoiding the overview path costs
    # nothing worth measuring.
    data = np.asarray(
        dataset.read(indexes=[1, 2, 3, 4], window=window, boundless=False), dtype=np.float64
    )
    if data.shape[1] < PATCH_GRID or data.shape[2] < PATCH_GRID:
        # Coarser than the target grid, so there is nothing to average down; resampling up
        # reads full resolution regardless of what pyramids exist.
        return np.asarray(
            dataset.read(
                indexes=[1, 2, 3, 4],
                window=window,
                out_shape=(4, PATCH_GRID, PATCH_GRID),
                resampling=Resampling.average,
                boundless=False,
            ),
            dtype=np.float64,
        )
    return _bin_average(data, PATCH_GRID)


def compare_pair(
    a: Footprint,
    b: Footprint,
    *,
    patches: int = DEFAULT_PATCHES,
    patch_metres: float = DEFAULT_PATCH_METRES,
    linearise: bool = False,
) -> RadiometricPairResult:
    # Checked before the bounds, because comparing coordinates expressed in two different
    # reference systems is meaningless rather than merely inaccurate.
    if a.crs != b.crs:
        raise RadiometryError(
            f"{a.block_id} is {a.crs} but {b.block_id} is {b.crs}; comparing across coordinate "
            "reference systems would require reprojection, which QA does not do silently"
        )

    box = intersection(a, b)
    if box is None:
        return RadiometricPairResult(
            block_a=a.block_id,
            block_b=b.block_id,
            overlap_area_ha=0.0,
            sample_count=0,
            sample_pixels=0,
            patch_metres=patch_metres,
            note="no overlap",
        )

    area_ha = (box[2] - box[0]) * (box[3] - box[1]) / 1e4
    samples_a: list[NDArray[Any]] = []
    samples_b: list[NDArray[Any]] = []
    used = 0

    with rasterio.open(a.path) as ds_a, rasterio.open(b.path) as ds_b:
        for x, y in _patch_origins(box, patches, patch_metres):
            patch_box = (x, y, x + patch_metres, y + patch_metres)
            pa = _read_patch(ds_a, patch_box)
            pb = _read_patch(ds_b, patch_box)
            if pa is None or pb is None:
                continue
            valid = (pa[3] >= _ALPHA_VALID) & (pb[3] >= _ALPHA_VALID)
            if not valid.any():
                continue
            samples_a.append(pa[:3][:, valid])
            samples_b.append(pb[:3][:, valid])
            used += 1

    if not samples_a:
        return RadiometricPairResult(
            block_a=a.block_id,
            block_b=b.block_id,
            overlap_area_ha=area_ha,
            sample_count=0,
            sample_pixels=0,
            patch_metres=patch_metres,
            note="overlap exists but no ground is valid in both blocks",
        )

    stacked_a = np.concatenate(samples_a, axis=1)
    stacked_b = np.concatenate(samples_b, axis=1)

    # Linearising here rather than at read time means every statistic below -- medians,
    # quantiles and the robust ratio alike -- describes light rather than display values, so
    # the gain solved from them is a gain in radiance. Deliveries are display-referred sRGB;
    # a scale factor in radiance is only a scale factor in encoded values where the encoding
    # is a pure power law, which sRGB is not near black. See `colour`.
    if linearise:
        stacked_a = dn_to_linear(stacked_a)
        stacked_b = dn_to_linear(stacked_b)

    bands = []
    for index, name in enumerate(BAND_NAMES):
        va, vb = stacked_a[index], stacked_b[index]
        median_a, median_b = float(np.median(va)), float(np.median(vb))
        centre = (median_a + median_b) / 2.0
        relative = 100.0 * (median_b - median_a) / centre if centre > 0 else 0.0

        combined = va + vb
        usable = combined >= _MIN_COMBINED_DN
        robust = (
            float(np.median(200.0 * (vb[usable] - va[usable]) / combined[usable]))
            if usable.any()
            else 0.0
        )
        bands.append(
            BandDifference(
                band=name,
                median_a=median_a,
                median_b=median_b,
                median_difference=median_b - median_a,
                relative_difference_pct=relative,
                robust_normalized_difference_pct=robust,
                qq_a=[float(v) for v in np.percentile(va, QQ_LEVELS)],
                qq_b=[float(v) for v in np.percentile(vb, QQ_LEVELS)],
            )
        )

    return RadiometricPairResult(
        block_a=a.block_id,
        block_b=b.block_id,
        overlap_area_ha=area_ha,
        sample_count=used,
        sample_pixels=int(stacked_a.shape[1]),
        patch_metres=patch_metres,
        bands=bands,
    )


def measure_project(
    project_id: str,
    blocks: list[tuple[str, Path]],
    *,
    min_overlap_ha: float = 1.0,
    patches: int = DEFAULT_PATCHES,
    patch_metres: float = DEFAULT_PATCH_METRES,
    linearise: bool = False,
    progress: Any = None,
) -> RadiometricOverlapReport:
    footprints = read_footprints(blocks)
    candidates = [
        (a, b)
        for a, b in itertools.combinations(footprints, 2)
        if (box := intersection(a, b)) is not None
        and (box[2] - box[0]) * (box[3] - box[1]) / 1e4 >= min_overlap_ha
    ]

    results = []
    for index, (a, b) in enumerate(candidates, start=1):
        result = compare_pair(a, b, patches=patches, patch_metres=patch_metres, linearise=linearise)
        results.append(result)
        if progress is not None:
            progress(index, len(candidates), result)

    return RadiometricOverlapReport(
        project_id=project_id,
        generated_at=datetime.now(UTC),
        pair_count=len(candidates),
        measured_count=sum(1 for r in results if r.sample_pixels > 0),
        qq_levels=list(QQ_LEVELS),
        linearised=linearise,
        pairs=results,
    )
