"""Ingestion of orthophotos produced outside this pipeline.

No reconstruction happens here. An external product is accepted as-is, its provenance is
recorded as external, and it then goes through exactly the same packaging and QA as an ODM
product. That shared path is what makes the two comparable.

The radiometric history of an external product is not controlled by this repository and may
not be recoverable. Where it cannot be recovered, that is recorded as a fact about the
product rather than left blank.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..integrity import sha256_file
from ..models.enums import ProcessingEngine, SourceType
from ..packaging.gdal_backend import RasterBackend, RasterioGdalBackend

_ENGINE_FOR_SOURCE = {
    SourceType.DJI_TERRA: ProcessingEngine.DJI_TERRA,
    SourceType.ODM: ProcessingEngine.ODM,
}


@dataclass(frozen=True)
class SourceOrtho:
    """The common contract that every producing path converges on."""

    path: Path
    source_type: SourceType
    processing_engine: ProcessingEngine
    sha256: str
    crs: str | None
    pixel_size_x: float
    pixel_size_y: float
    band_count: int


class ExternalIngestError(RuntimeError):
    pass


def ingest_external_ortho(
    path: Path,
    source_type: SourceType,
    *,
    backend: RasterBackend | None = None,
) -> SourceOrtho:
    engine: RasterBackend = backend or RasterioGdalBackend()
    if not path.is_file():
        raise ExternalIngestError(f"{path} is not a file")

    try:
        description = engine.describe(path)
    except Exception as error:
        raise ExternalIngestError(f"{path} could not be opened as a raster: {error}") from error

    return SourceOrtho(
        path=path,
        source_type=source_type,
        processing_engine=_ENGINE_FOR_SOURCE[source_type],
        sha256=sha256_file(path),
        crs=description.crs,
        pixel_size_x=description.pixel_size_x,
        pixel_size_y=description.pixel_size_y,
        band_count=description.band_count,
    )
