"""GDAL mechanism for reading and writing rasters.

This module knows how to talk to GDAL. It does not know what the master contract is —
that lives in `raster.py`, which drives this backend and asserts the invariants. Keeping
them apart is what allows the backend to be swapped for a containerised GDAL later, and
allows the contract to be tested against a fake backend.

The GDAL in use is the one bundled in the Rasterio wheel, pinned by uv.lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pyproj
import rasterio
from rasterio.crs import CRS
from rasterio.enums import ColorInterp, MaskFlags
from rasterio.windows import Window

from ..models.enums import AlphaProvenance
from .correction import BlockCorrection, apply_correction

# The master contract in GDAL terms. BIGTIFF is YES rather than ODM's IF_SAFER because the
# contract is unconditional: a 3.9 GB block and a 4.1 GB block must not be different
# formats. PREDICTOR=2 is horizontal differencing, which is lossless.
#
# ALPHA=NON-PREMULTIPLIED matters scientifically. Premultiplied alpha scales RGB by coverage
# at partially transparent edge pixels, which would darken block margins and corrupt the
# overlap comparisons in docs/radiometry.md.
MASTER_CREATION_OPTIONS: dict[str, Any] = {
    "compress": "DEFLATE",
    "predictor": 2,
    "tiled": True,
    "blockxsize": 512,
    "blockysize": 512,
    "bigtiff": "YES",
    "num_threads": "ALL_CPUS",
    "interleave": "pixel",
    "photometric": "RGB",
    "alpha": "NON-PREMULTIPLIED",
}

SUPPORTED_DTYPES = ("uint8", "uint16")

# GDAL persists metadata it cannot fit in the file itself into a .aux.xml sidecar written
# next to the raster. Next to a source file that means writing into a source tree, which is
# forbidden, so PAM is disabled for every operation this backend performs.
_GDAL_ENV: dict[str, Any] = {"GDAL_PAM_ENABLED": False}

_TIFF_CLASSIC = 42
_TIFF_BIG = 43


def crs_identifier(crs: Any) -> str | None:
    """Compact authority string for a CRS, e.g. `EPSG:32647` or `EPSG:32647+5705`.

    A compound CRS has no single EPSG code and its WKT runs to hundreds of characters, which
    is not something a manifest should carry as an identifier.
    """
    if crs is None:
        return None
    code = crs.to_epsg()
    if code:
        return f"EPSG:{code}"
    parsed = pyproj.CRS.from_user_input(crs.to_wkt())
    if parsed.is_compound:
        codes = [sub.to_epsg() for sub in parsed.sub_crs_list]
        if all(codes):
            return "EPSG:" + "+".join(str(code) for code in codes)
    return str(crs.to_string())


def horizontal_epsg(identifier: str | None) -> int | None:
    """The horizontal component of a possibly compound CRS."""
    if identifier is None:
        return None
    parsed = pyproj.CRS.from_user_input(identifier)
    horizontal = parsed.sub_crs_list[0] if parsed.is_compound else parsed
    epsg: int | None = horizontal.to_epsg()
    return epsg


class PackagingError(RuntimeError):
    """Packaging cannot proceed without altering the product in a way the contract forbids."""


class AmbiguousValidityError(PackagingError):
    pass


class MissingValidityError(PackagingError):
    pass


@dataclass(frozen=True)
class RasterDescription:
    path: Path
    driver: str
    width: int
    height: int
    band_count: int
    dtype: str
    crs: str | None
    transform: tuple[float, float, float, float, float, float]
    colorinterp: tuple[str, ...]
    nodatavals: tuple[float | None, ...]
    compression: str | None
    tiled: bool
    block_shape: tuple[int, int] | None
    overview_counts: tuple[int, ...]
    tiff_version: int | None

    @property
    def is_bigtiff(self) -> bool:
        return self.tiff_version == _TIFF_BIG

    @property
    def pixel_size_x(self) -> float:
        return self.transform[0]

    @property
    def pixel_size_y(self) -> float:
        return -self.transform[4]

    @property
    def is_axis_aligned(self) -> bool:
        return self.transform[1] == 0.0 and self.transform[3] == 0.0


@dataclass(frozen=True)
class PackagingPlan:
    band_selection: tuple[int, int, int] | None = None
    allow_alpha_from_nodata: bool = False
    verify_pixels: bool = False

    # A solved radiometric correction to apply while writing. Absent means the master carries
    # the delivered pixels unchanged, which is the default: correcting is a deliberate act
    # that changes what the product is, not a packaging detail.
    correction: BlockCorrection | None = None

    # A vertical reference that the source file does not carry can only come from a document,
    # never from the pixels, so declaring one is always explicit. Deliveries whose vertical
    # CRS lives in an accompanying metadata sheet rather than in the file header are the
    # reason this exists. The horizontal component must match what the source declares.
    declare_crs: str | None = None


@dataclass(frozen=True)
class AlphaPlan:
    provenance: AlphaProvenance
    band_index: int | None


@dataclass(frozen=True)
class PackagingOutcome:
    destination: Path
    alpha_provenance: AlphaProvenance
    rgb_bands: tuple[int, int, int]
    operations: list[tuple[str, str]] = field(default_factory=list)
    pixels_verified: bool = False


class RasterBackend(Protocol):
    name: str

    def gdal_version(self) -> str: ...

    def describe(self, path: Path) -> RasterDescription: ...

    def package(self, source: Path, destination: Path, plan: PackagingPlan) -> PackagingOutcome: ...


def _tiff_version(path: Path) -> int | None:
    """Read the TIFF header version field.

    BigTIFF is a format property, so it is read from the file rather than inferred from
    size. That is the entire point of requiring it unconditionally.
    """
    with path.open("rb") as handle:
        header = handle.read(4)
    if len(header) < 4:
        return None
    if header[:2] == b"II":
        endian = "little"
    elif header[:2] == b"MM":
        endian = "big"
    else:
        return None
    return int.from_bytes(header[2:4], endian)  # type: ignore[arg-type]


def _windows(width: int, height: int, block_x: int, block_y: int) -> Iterator[Window]:
    for row_off in range(0, height, block_y):
        rows = min(block_y, height - row_off)
        for col_off in range(0, width, block_x):
            cols = min(block_x, width - col_off)
            yield Window(col_off, row_off, cols, rows)


class RasterioGdalBackend:
    name = "rasterio-bundled-gdal"

    def gdal_version(self) -> str:
        return str(rasterio.__gdal_version__)

    def describe(self, path: Path) -> RasterDescription:
        with rasterio.Env(**_GDAL_ENV):
            return self._describe(path)

    @staticmethod
    def _describe(path: Path) -> RasterDescription:
        with rasterio.open(path) as dataset:
            structure = dataset.tags(ns="IMAGE_STRUCTURE")
            compression = structure.get("COMPRESSION")
            if compression is None and dataset.profile.get("compress"):
                compression = str(dataset.profile["compress"]).upper()
            block_shape = dataset.block_shapes[0] if dataset.block_shapes else None
            return RasterDescription(
                path=path,
                driver=str(dataset.driver),
                width=int(dataset.width),
                height=int(dataset.height),
                band_count=int(dataset.count),
                dtype=str(dataset.dtypes[0]),
                crs=crs_identifier(dataset.crs),
                transform=(
                    float(dataset.transform.a),
                    float(dataset.transform.b),
                    float(dataset.transform.c),
                    float(dataset.transform.d),
                    float(dataset.transform.e),
                    float(dataset.transform.f),
                ),
                colorinterp=tuple(ci.name for ci in dataset.colorinterp),
                nodatavals=tuple(dataset.nodatavals),
                compression=compression,
                tiled=bool(dataset.profile.get("tiled", False)),
                block_shape=(int(block_shape[0]), int(block_shape[1])) if block_shape else None,
                overview_counts=tuple(len(dataset.overviews(i)) for i in dataset.indexes),
                tiff_version=_tiff_version(path) if dataset.driver == "GTiff" else None,
            )

    def package(self, source: Path, destination: Path, plan: PackagingPlan) -> PackagingOutcome:
        with rasterio.Env(**_GDAL_ENV):
            return self._package(source, destination, plan)

    def _package(self, source: Path, destination: Path, plan: PackagingPlan) -> PackagingOutcome:
        # Verification asserts the RGB bands are bit-identical before and after packaging; a
        # correction changes them on purpose. Silently skipping the check would leave a run
        # claiming verification it never performed, so the contradiction is refused instead.
        if plan.correction is not None and plan.verify_pixels:
            raise PackagingError(
                "pixel verification and radiometric correction cannot both be requested: "
                "verification requires the pixels to be unchanged and correction changes them"
            )

        operations: list[tuple[str, str]] = []
        with rasterio.open(source) as src:
            if src.crs is None:
                raise PackagingError(
                    f"{source} declares no CRS; the master contract requires an explicit CRS "
                    "and packaging will not invent one"
                )
            dtype = str(src.dtypes[0])
            if dtype not in SUPPORTED_DTYPES:
                raise PackagingError(
                    f"{source} has dtype {dtype}; only {', '.join(SUPPORTED_DTYPES)} are "
                    "supported, because converting the dtype would alter pixel values"
                )

            rgb_bands = self._resolve_rgb_bands(src, plan, operations)
            alpha_plan = self._resolve_alpha(src, plan)
            if alpha_plan.provenance is not AlphaProvenance.PASSTHROUGH:
                operations.append(("alpha", alpha_plan.provenance.value))

            # Sources that carry a real alpha band AND a NoData value are common: DJI Terra
            # writes both. Alpha wins, and dropping the redundant NoData is a change to the
            # product, so it is recorded rather than done quietly.
            dropped_nodata = sorted({value for value in src.nodatavals if value is not None})
            if dropped_nodata:
                operations.append(
                    ("nodata", f"dropped {dropped_nodata}; alpha is the validity mask")
                )

            if plan.correction is not None:
                gains = ", ".join(f"{g:.4f}" for g in plan.correction.gains)
                operations.append(
                    (
                        "radiometric_correction",
                        f"gains ({gains}) applied in {plan.correction.space.value} space "
                        f"from {plan.correction.source_solution}",
                    )
                )

            output_crs = self._resolve_crs(src, plan, operations)
            alpha_max = 255 if dtype == "uint8" else 65535
            profile: dict[str, Any] = {
                "driver": "GTiff",
                "width": int(src.width),
                "height": int(src.height),
                "count": 4,
                "dtype": dtype,
                "crs": output_crs,
                "transform": src.transform,
                # Alpha is the validity mask. Carrying a NoData value as well is precisely
                # the ambiguity the contract exists to remove.
                "nodata": None,
                **MASTER_CREATION_OPTIONS,
            }

            destination.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(destination, "w", **profile) as dst:
                dst.colorinterp = (
                    ColorInterp.red,
                    ColorInterp.green,
                    ColorInterp.blue,
                    ColorInterp.alpha,
                )
                for window in _windows(int(src.width), int(src.height), 512, 512):
                    rgb = src.read(indexes=list(rgb_bands), window=window)
                    if plan.correction is not None:
                        rgb = apply_correction(rgb, plan.correction, max_value=alpha_max)
                    dst.write(rgb, indexes=[1, 2, 3], window=window)
                    dst.write(
                        self._alpha_for(src, alpha_plan, window, dtype, alpha_max),
                        4,
                        window=window,
                    )

        pixels_verified = False
        if plan.verify_pixels:
            pixels_verified = self._verify_pixels(source, destination, rgb_bands)

        return PackagingOutcome(
            destination=destination,
            alpha_provenance=alpha_plan.provenance,
            rgb_bands=rgb_bands,
            operations=operations,
            pixels_verified=pixels_verified,
        )

    @staticmethod
    def _resolve_crs(src: Any, plan: PackagingPlan, operations: list[tuple[str, str]]) -> Any:
        if plan.declare_crs is None:
            return src.crs

        source_identifier = crs_identifier(src.crs)
        if horizontal_epsg(plan.declare_crs) != horizontal_epsg(source_identifier):
            raise PackagingError(
                f"declared CRS {plan.declare_crs} has a different horizontal component than "
                f"the source CRS {source_identifier}; declaring a CRS may add a vertical "
                "reference but must never reinterpret the horizontal one, which would move "
                "the pixels without touching them"
            )
        operations.append(
            (
                "crs",
                f"declared {plan.declare_crs} (source declared {source_identifier}); "
                "metadata only, grid and pixels unchanged",
            )
        )
        return CRS.from_user_input(plan.declare_crs)

    @staticmethod
    def _resolve_rgb_bands(
        src: Any, plan: PackagingPlan, operations: list[tuple[str, str]]
    ) -> tuple[int, int, int]:
        if plan.band_selection is not None:
            operations.append(("bands", f"selected {list(plan.band_selection)}"))
            return plan.band_selection

        tagged = {ci: index for index, ci in enumerate(src.colorinterp, start=1)}
        wanted = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
        if all(ci in tagged for ci in wanted):
            return (tagged[wanted[0]], tagged[wanted[1]], tagged[wanted[2]])

        if src.count > 4:
            raise PackagingError(
                f"source has {src.count} bands with no red/green/blue colour interpretation; "
                "the profile must declare packaging.band_selection explicitly"
            )
        return (1, 2, 3)

    @staticmethod
    def _resolve_alpha(src: Any, plan: PackagingPlan) -> AlphaPlan:
        flags = src.mask_flag_enums
        has_alpha_flag = any(MaskFlags.alpha in band_flags for band_flags in flags)

        if src.count >= 4:
            for index, interpretation in enumerate(src.colorinterp, start=1):
                if interpretation == ColorInterp.alpha:
                    return AlphaPlan(AlphaProvenance.PASSTHROUGH, index)
            if has_alpha_flag:
                return AlphaPlan(AlphaProvenance.PASSTHROUGH, 4)
            return AlphaPlan(AlphaProvenance.RETAGGED, 4)

        if any(MaskFlags.per_dataset in band_flags for band_flags in flags):
            return AlphaPlan(AlphaProvenance.FROM_MASK, None)

        if any(MaskFlags.nodata in band_flags for band_flags in flags):
            if not plan.allow_alpha_from_nodata:
                raise AmbiguousValidityError(
                    "source has no alpha band and no mask band; its only validity signal is "
                    f"NoData={src.nodata}. Deriving alpha from that would mark every "
                    "legitimately black pixel invalid, so it requires explicit opt-in "
                    "(packaging.allow_alpha_from_nodata) and the product is flagged REVIEW"
                )
            return AlphaPlan(AlphaProvenance.FROM_NODATA, None)

        raise MissingValidityError(
            "source has no alpha band, no mask band and no NoData value, so the valid extent "
            "cannot be established; the master contract requires a validity mask"
        )

    @staticmethod
    def _alpha_for(
        src: Any, alpha_plan: AlphaPlan, window: Window, dtype: str, alpha_max: int
    ) -> Any:
        if alpha_plan.band_index is not None:
            return src.read(alpha_plan.band_index, window=window)
        mask = src.dataset_mask(window=window)
        return np.where(mask > 0, alpha_max, 0).astype(dtype)

    @staticmethod
    def _verify_pixels(source: Path, destination: Path, rgb_bands: tuple[int, int, int]) -> bool:
        """Confirm the RGB pixels survived packaging unchanged.

        Doubles the read cost, so it is opt-in. It is the direct guard against an accidental
        resample or dtype conversion, which a geotransform comparison alone would not catch.
        """
        with rasterio.open(source) as src, rasterio.open(destination) as dst:
            for out_index, in_index in enumerate(rgb_bands, start=1):
                if src.checksum(in_index) != dst.checksum(out_index):
                    raise PackagingError(
                        f"band {in_index} of {source.name} does not match band {out_index} of "
                        f"{destination.name} after packaging; pixels were altered"
                    )
        return True
