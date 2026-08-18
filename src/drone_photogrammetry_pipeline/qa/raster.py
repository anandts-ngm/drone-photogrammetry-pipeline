"""Master raster QA.

QA reads the delivered file. It never trusts the process that produced it, because the
whole purpose of the check is to catch the case where that process did something other than
what it reported.

Every check names the contract clause it verifies, so a failure says which contract was
violated and what was observed instead.
"""

from __future__ import annotations

from pathlib import Path

from ..models.enums import AlphaProvenance, CheckOutcome, GateStatus
from ..models.qa import RasterCheck, RasterQAResult
from ..packaging.gdal_backend import (
    RasterBackend,
    RasterDescription,
    RasterioGdalBackend,
)

REQUIRED_COMPRESSION = "DEFLATE"
REQUIRED_BAND_COUNT = 4
REQUIRED_COLORINTERP = ("red", "green", "blue", "alpha")

# Anything that does not round-trip pixel values. The master is an analytical source, so a
# lossy codec anywhere in the file disqualifies it regardless of how good it looks.
LOSSY_COMPRESSION = frozenset({"JPEG", "WEBP", "LERC", "JXL"})


def _check(
    name: str,
    clause: str,
    passed: bool,
    expected: object,
    observed: object,
    message: str = "",
) -> RasterCheck:
    return RasterCheck(
        name=name,
        clause=clause,
        outcome=CheckOutcome.PASS if passed else CheckOutcome.FAIL,
        expected=expected,
        observed=observed,
        message=message if not passed else "",
    )


def _describe_failure(expected: object, observed: object) -> str:
    return f"expected {expected}, observed {observed}"


def run_raster_qa(
    path: Path,
    *,
    alpha_provenance: AlphaProvenance | None = None,
    backend: RasterBackend | None = None,
) -> RasterQAResult:
    engine: RasterBackend = backend or RasterioGdalBackend()
    try:
        description = engine.describe(path)
    except Exception as error:
        unreadable = RasterCheck(
            name="readable",
            clause="Format GeoTIFF",
            outcome=CheckOutcome.FAIL,
            expected="a readable raster",
            observed=type(error).__name__,
            message=str(error),
        )
        return RasterQAResult(
            status=GateStatus.FAIL, checks={"readable": False}, details=[unreadable]
        )

    details = _build_checks(description)
    if alpha_provenance is AlphaProvenance.FROM_NODATA:
        details.append(
            RasterCheck(
                name="alpha_provenance",
                clause="Alpha channel required; NoData policy",
                outcome=CheckOutcome.REVIEW,
                expected=f"alpha from source alpha or mask, not {AlphaProvenance.FROM_NODATA}",
                observed=AlphaProvenance.FROM_NODATA.value,
                message=(
                    "alpha was derived from an ambiguous NoData value, so pixels that are "
                    "legitimately black may now be marked invalid; a human must confirm"
                ),
            )
        )

    return RasterQAResult(status=_status(details), checks=_summary(description), details=details)


def _build_checks(description: RasterDescription) -> list[RasterCheck]:
    colorinterp = description.colorinterp
    overview_total = sum(description.overview_counts)
    compression = (description.compression or "NONE").upper()
    pixel_sizes_present = description.pixel_size_x != 0.0 and description.pixel_size_y != 0.0

    return [
        _check(
            "readable",
            "Format GeoTIFF",
            True,
            "a readable raster",
            "readable",
        ),
        _check(
            "is_geotiff",
            "Format GeoTIFF",
            description.driver == "GTiff",
            "GTiff",
            description.driver,
            _describe_failure("GTiff", description.driver),
        ),
        _check(
            "band_count",
            "Bands 4",
            description.band_count == REQUIRED_BAND_COUNT,
            REQUIRED_BAND_COUNT,
            description.band_count,
            _describe_failure(REQUIRED_BAND_COUNT, description.band_count),
        ),
        _check(
            "colorinterp",
            "Band interpretation red, green, blue, alpha",
            tuple(c.lower() for c in colorinterp) == REQUIRED_COLORINTERP,
            list(REQUIRED_COLORINTERP),
            [c.lower() for c in colorinterp],
            _describe_failure(list(REQUIRED_COLORINTERP), [c.lower() for c in colorinterp]),
        ),
        _check(
            "compression",
            "Compression DEFLATE",
            compression == REQUIRED_COMPRESSION,
            REQUIRED_COMPRESSION,
            compression,
            _describe_failure(f"COMPRESS={REQUIRED_COMPRESSION}", compression),
        ),
        _check(
            "no_lossy_compression",
            "Lossy compression forbidden",
            compression not in LOSSY_COMPRESSION,
            f"none of {sorted(LOSSY_COMPRESSION)}",
            compression,
            f"{compression} is lossy and alters pixel values",
        ),
        _check(
            "tiled",
            "Tiled YES",
            description.tiled,
            True,
            description.tiled,
            _describe_failure("tiled storage", "striped storage"),
        ),
        _check(
            "bigtiff",
            "BigTIFF YES",
            description.is_bigtiff,
            "BigTIFF (TIFF version 43)",
            f"TIFF version {description.tiff_version}",
            _describe_failure("BigTIFF (version 43)", f"version {description.tiff_version}"),
        ),
        _check(
            "crs_present",
            "CRS explicitly defined",
            description.crs is not None,
            "an explicit CRS",
            description.crs,
            "no CRS is defined on the raster",
        ),
        _check(
            "pixel_size_present",
            "Pixel size recorded exactly",
            pixel_sizes_present and description.is_axis_aligned,
            "non-zero, axis-aligned pixel sizes",
            {
                "x": description.pixel_size_x,
                "y": description.pixel_size_y,
                "axis_aligned": description.is_axis_aligned,
            },
            "pixel size is zero or the geotransform carries rotation",
        ),
        _check(
            "alpha_present",
            "Alpha channel required",
            len(colorinterp) >= REQUIRED_BAND_COUNT and colorinterp[3].lower() == "alpha",
            "band 4 tagged alpha",
            colorinterp[3].lower() if len(colorinterp) >= 4 else None,
            "band 4 is not tagged as alpha, so the validity mask is not defined",
        ),
        _check(
            "nodata_policy",
            "No ambiguous NoData while alpha is the validity mask",
            all(value is None for value in description.nodatavals),
            "no NoData value on any band",
            list(description.nodatavals),
            "a NoData value is set while alpha is the validity mask, which is ambiguous",
        ),
        _check(
            "overview_count",
            "No overviews in the delivered master",
            overview_total == 0,
            0,
            overview_total,
            _describe_failure(0, overview_total),
        ),
    ]


def _summary(description: RasterDescription) -> dict[str, object]:
    colorinterp = tuple(c.lower() for c in description.colorinterp)
    return {
        "rgba": colorinterp == REQUIRED_COLORINTERP,
        "compression": (description.compression or "NONE").upper(),
        "tiled": description.tiled,
        "alpha": len(colorinterp) >= 4 and colorinterp[3] == "alpha",
        "overview_count": sum(description.overview_counts),
        "bigtiff": description.is_bigtiff,
        "crs": description.crs,
        "pixel_size_x": description.pixel_size_x,
        "pixel_size_y": description.pixel_size_y,
    }


def _status(details: list[RasterCheck]) -> GateStatus:
    if any(check.outcome is CheckOutcome.FAIL for check in details):
        return GateStatus.FAIL
    if any(check.outcome is CheckOutcome.REVIEW for check in details):
        return GateStatus.REVIEW
    return GateStatus.PASS
