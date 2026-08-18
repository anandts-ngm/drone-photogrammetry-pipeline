"""The master raster contract.

Packaging is a copy with re-encoding, never a warp. This module drives the backend and then
asserts that the grid survived. The assertions are the point: a resample that produced a
plausible-looking file would otherwise be indistinguishable from a correct run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..integrity import sha256_file
from ..models.manifest import GridRecord, PackagingOperation, PackagingRecord
from .gdal_backend import (
    MASTER_CREATION_OPTIONS,
    PackagingError,
    PackagingPlan,
    RasterBackend,
    RasterDescription,
    RasterioGdalBackend,
    horizontal_epsg,
)

MASTER_SUFFIX = "_ORTHO_MASTER.tif"


class GridChangedError(PackagingError):
    """The packaged raster does not sit on the same grid as its source."""


@dataclass(frozen=True)
class PackagingResult:
    master_path: Path
    record: PackagingRecord
    description: RasterDescription


def master_filename(block_id: str) -> str:
    return f"{block_id}{MASTER_SUFFIX}"


def _grid_record(description: RasterDescription) -> GridRecord:
    return GridRecord(
        width=description.width,
        height=description.height,
        transform=list(description.transform),
        crs=description.crs,
    )


def _assert_grid_preserved(
    before: RasterDescription,
    after: RasterDescription,
    *,
    declared_crs: str | None = None,
) -> None:
    differences: list[str] = []
    if before.width != after.width or before.height != after.height:
        differences.append(
            f"dimensions {before.width}x{before.height} -> {after.width}x{after.height}"
        )
    if before.transform != after.transform:
        differences.append(f"geotransform {before.transform} -> {after.transform}")

    # When a vertical reference has been declared the CRS string legitimately changes. The
    # horizontal component still must not, since changing that would relocate every pixel
    # while leaving the geotransform untouched.
    if declared_crs is None:
        if before.crs != after.crs:
            differences.append(f"CRS {before.crs} -> {after.crs}")
    elif horizontal_epsg(before.crs) != horizontal_epsg(after.crs):
        differences.append(f"horizontal CRS {before.crs} -> {after.crs}")

    if differences:
        raise GridChangedError(
            "packaging changed the spatial raster grid, which it must never do: "
            + "; ".join(differences)
        )


def package_master(
    source: Path,
    destination: Path,
    *,
    plan: PackagingPlan | None = None,
    backend: RasterBackend | None = None,
    source_sha256: str | None = None,
) -> PackagingResult:
    """Package a raster to the master contract.

    `source_sha256` may be supplied by a caller that has already hashed the source, so a
    multi-gigabyte file is not read twice for the same digest.
    """
    engine: RasterBackend = backend or RasterioGdalBackend()
    packaging_plan = plan or PackagingPlan()

    before = engine.describe(source)
    outcome = engine.package(source, destination, packaging_plan)
    after = engine.describe(destination)
    _assert_grid_preserved(before, after, declared_crs=packaging_plan.declare_crs)

    record = PackagingRecord(
        backend=engine.name,
        gdal_version=engine.gdal_version(),
        creation_options=dict(MASTER_CREATION_OPTIONS),
        source_path=str(source),
        source_sha256=source_sha256 or sha256_file(source),
        alpha_provenance=outcome.alpha_provenance,
        operations=[
            PackagingOperation(name=name, detail=detail) for name, detail in outcome.operations
        ],
        grid_in=_grid_record(before),
        grid_out=_grid_record(after),
        grid_preserved=True,
        pixels_verified=outcome.pixels_verified,
    )
    return PackagingResult(master_path=destination, record=record, description=after)
