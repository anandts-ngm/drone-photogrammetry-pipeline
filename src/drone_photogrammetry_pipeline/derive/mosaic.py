"""A virtual mosaic over a project's masters.

A VRT is a small XML file describing where each master sits on one common grid. GDAL reads
the masters on demand, so the mosaic costs kilobytes rather than the 97 gigapixels it
addresses, and it opens in QGIS as a single raster layer.

Three properties this has to have, and why:

* **The grid is the finest native pixel size in the project.** These masters run from 2.54 cm
  to 5.11 cm across 47 distinct values, and a VRT has one geotransform, so some resampling on
  read is unavoidable. Choosing the finest means coarser blocks are interpolated up, which
  invents no detail; choosing anything coarser would discard detail the finest blocks really
  have.
* **Alpha composites rather than overwrites.** Each source is written with `UseMaskBand`, so a
  transparent pixel in a later block does not erase valid ground from an earlier one. Without
  it, the mosaic loses data wherever block footprints interlock.
* **Paths are relative to the VRT.** An absolute path breaks the moment the outputs tree is
  copied or moved, which for a file whose entire purpose is to reference other files is a
  short life. Sources outside the VRT's tree fall back to absolute, since nothing else is
  correct.

Written directly rather than through `gdalbuildvrt` because the GDAL Python bindings are not
a dependency here, and because writing it gives control over the path form.
"""

from __future__ import annotations

import math
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import rasterio
from rasterio.enums import Resampling

from ..packaging.gdal_backend import crs_identifier

BAND_COLOUR_INTERP = ("Red", "Green", "Blue", "Alpha")

# Overviews start at 8 rather than 2. Levels 2 and 4 of a 97-gigapixel mosaic are 24 and 6
# gigapixels, which is most of the storage spent on zoom ranges that do not need help: at 1:4
# a screen shows about 150 m of ground and reads from the masters in about a second. The
# expensive case is the full extent, around 1:256, and that is what the deep levels serve.
FIRST_OVERVIEW_FACTOR = 8

# Stop once an overview is smaller than a window anyone would look at.
_SMALLEST_OVERVIEW_PIXELS = 256

# Lossless. An overview is what *any* reduced-resolution read returns, including a
# measurement, so a lossy pyramid silently changes numbers rather than only appearances --
# which is exactly how a separate pipeline's viewing overviews corrupted this project's
# overlap statistics on 2026-08-19.
_OVERVIEW_ENV = {
    "COMPRESS_OVERVIEW": "DEFLATE",
    "PREDICTOR_OVERVIEW": 2,
    "GDAL_NUM_THREADS": "ALL_CPUS",
}


class MosaicError(RuntimeError):
    pass


@dataclass(frozen=True)
class MosaicSource:
    path: Path
    width: int
    height: int
    pixel_size: float
    left: float
    top: float
    band_count: int
    dtype: str


@dataclass(frozen=True)
class Mosaic:
    path: Path
    width: int
    height: int
    pixel_size: float
    crs: str | None
    sources: int

    @property
    def gigapixels(self) -> float:
        return self.width * self.height / 1e9


def read_sources(masters: list[Path]) -> list[MosaicSource]:
    """Describe every master, refusing anything that cannot share one grid."""
    if not masters:
        raise MosaicError("no masters given; a mosaic of nothing is not a product")

    found: list[MosaicSource] = []
    crs_seen: set[str] = set()
    for path in masters:
        with rasterio.open(path) as ds:
            transform = ds.transform
            # A rotated or sheared source cannot be placed on an axis-aligned grid by
            # offsetting alone, and silently ignoring the rotation would misplace every pixel.
            if transform.b != 0.0 or transform.d != 0.0:
                raise MosaicError(f"{path.name} is not axis-aligned; a VRT cannot place it")
            if abs(transform.a + transform.e) > 1e-9:
                raise MosaicError(
                    f"{path.name} has non-square pixels ({transform.a}, {-transform.e}); "
                    "the mosaic grid assumes square"
                )
            crs_seen.add(str(ds.crs))
            found.append(
                MosaicSource(
                    path=path,
                    width=int(ds.width),
                    height=int(ds.height),
                    pixel_size=float(transform.a),
                    left=float(transform.c),
                    top=float(transform.f),
                    band_count=int(ds.count),
                    dtype=str(ds.dtypes[0]),
                )
            )

    if len(crs_seen) > 1:
        raise MosaicError(
            f"masters declare {len(crs_seen)} different coordinate reference systems "
            f"({sorted(crs_seen)}); mosaicking would require reprojection, which this does not do"
        )
    dtypes = {s.dtype for s in found}
    if len(dtypes) > 1:
        raise MosaicError(f"masters mix data types {sorted(dtypes)}")
    bands = {s.band_count for s in found}
    if bands != {4}:
        raise MosaicError(f"expected 4-band RGBA masters, found band counts {sorted(bands)}")
    return found


def _source_element(
    source: MosaicSource, band: int, vrt_path: Path, grid_size: float, left: float, top: float
) -> ET.Element:
    element = ET.Element("ComplexSource")

    # os.path.relpath rather than Path.relative_to: the masters sit in a sibling branch of the
    # tree, not under the VRT's directory, so the relative path needs `..` segments and
    # relative_to refuses to produce those. Falls back to absolute across drives, where no
    # relative path exists.
    try:
        filename = os.path.relpath(source.path, vrt_path.parent).replace("\\", "/")
        is_relative = "1"
    except ValueError:
        filename = str(source.path)
        is_relative = "0"

    name = ET.SubElement(element, "SourceFilename")
    name.set("relativeToVRT", is_relative)
    name.text = filename
    ET.SubElement(element, "SourceBand").text = str(band)

    src = ET.SubElement(element, "SrcRect")
    src.set("xOff", "0")
    src.set("yOff", "0")
    src.set("xSize", str(source.width))
    src.set("ySize", str(source.height))

    scale = source.pixel_size / grid_size
    dst = ET.SubElement(element, "DstRect")
    dst.set("xOff", f"{(source.left - left) / grid_size:.6f}")
    dst.set("yOff", f"{(top - source.top) / grid_size:.6f}")
    dst.set("xSize", f"{source.width * scale:.6f}")
    dst.set("ySize", f"{source.height * scale:.6f}")

    # Alpha is the validity mask, so compositing must respect it or interlocking footprints
    # lose ground to whichever block happens to be written last.
    ET.SubElement(element, "UseMaskBand").text = "true"
    return element


def overview_factors(width: int, height: int) -> list[int]:
    """Decimation factors to build, from `FIRST_OVERVIEW_FACTOR` down to a small thumbnail.

    Adapted to the mosaic's size so a small project does not get levels finer than itself.
    """
    factors: list[int] = []
    factor = FIRST_OVERVIEW_FACTOR
    while max(width, height) // factor >= _SMALLEST_OVERVIEW_PIXELS:
        factors.append(factor)
        factor *= 2
    return factors


def add_overviews(mosaic: Path, factors: list[int]) -> list[int]:
    """Build an external pyramid beside the VRT.

    Without this a zoomed-out read has nothing coarse to fall back on and GDAL must touch every
    pixel of every master: measured on this survey, a 200x300 read of the full extent did not
    complete in ten minutes. The pyramid is what makes the mosaic openable.

    Written next to the VRT, inside `derived/`, so the masters keep no overviews as the raster
    contract requires and the derived data stays where derived data belongs.
    """
    if not factors:
        return []
    with rasterio.Env(**_OVERVIEW_ENV):
        with rasterio.open(mosaic, "r+") as ds:
            ds.build_overviews(factors, Resampling.average)
        with rasterio.open(mosaic) as ds:
            return [int(f) for f in ds.overviews(1)]


def build_mosaic(masters: list[Path], destination: Path) -> Mosaic:
    """Write a VRT addressing every master on one grid."""
    sources = read_sources(masters)

    grid_size = min(s.pixel_size for s in sources)
    left = min(s.left for s in sources)
    top = max(s.top for s in sources)
    right = max(s.left + s.width * s.pixel_size for s in sources)
    bottom = min(s.top - s.height * s.pixel_size for s in sources)

    width = math.ceil((right - left) / grid_size)
    height = math.ceil((top - bottom) / grid_size)

    with rasterio.open(sources[0].path) as ds:
        wkt = ds.crs.to_wkt() if ds.crs else None
        identifier = crs_identifier(ds.crs)

    root = ET.Element("VRTDataset")
    root.set("rasterXSize", str(width))
    root.set("rasterYSize", str(height))
    if wkt:
        ET.SubElement(root, "SRS").text = wkt
    ET.SubElement(root, "GeoTransform").text = (
        f"  {left:.10e},  {grid_size:.10e},  0.0000000000e+00,  "
        f"{top:.10e},  0.0000000000e+00, -{grid_size:.10e}"
    )

    for index, colour in enumerate(BAND_COLOUR_INTERP, start=1):
        band = ET.SubElement(root, "VRTRasterBand")
        band.set("dataType", "Byte" if sources[0].dtype == "uint8" else "UInt16")
        band.set("band", str(index))
        ET.SubElement(band, "ColorInterp").text = colour
        for source in sources:
            band.append(_source_element(source, index, destination, grid_size, left, top))

    destination.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(destination, encoding="utf-8", xml_declaration=False)

    return Mosaic(
        path=destination,
        width=width,
        height=height,
        pixel_size=grid_size,
        crs=identifier,
        sources=len(sources),
    )
