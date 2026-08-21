"""Turning a validated block into an orthophoto via ODM.

This is the adapter: it knows how a block becomes a NodeODM task and how the result becomes
a `SourceOrtho`. It holds no HTTP — that lives in `nodeodm.client` — and no raster knowledge,
which lives in `packaging`.

Two things learned from a running engine rather than assumed, both of which shape this
module:

* **Ground control is a file, not an option.** `gcp` and `geo` are ODM command-line arguments
  but are not among NodeODM's 81 processing options. They reach ODM by being uploaded
  alongside the images under the names ODM looks for.
* **Only `all.zip` can be downloaded.** Selective retrieval is done by restricting what goes
  *into* the archive via the `outputs` parameter, then extracting the members wanted.
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..integrity import sha256_file
from ..log import get_logger
from ..models.block import ValidatedBlock
from ..models.enums import ProcessingEngine, SourceType
from ..models.profile import ProcessingProfile
from ..nodeodm.client import NodeODMClient, NodeODMError, TaskRequest
from ..nodeodm.schemas import TaskInfo
from .external import SourceOrtho

logger = get_logger("processing.odm")

# Where ODM writes the products this repository cares about, verified against ODM's output
# documentation. Which orthophoto is authoritative is a profile decision: `odm_orthophoto.tif`
# is cropped by --crop, `.original.tif` is not.
ORTHO_ASSET = "odm_orthophoto/odm_orthophoto.tif"
ORTHO_UNCROPPED_ASSET = "odm_orthophoto/odm_orthophoto.original.tif"
DSM_ASSET = "odm_dem/dsm.tif"
DTM_ASSET = "odm_dem/dtm.tif"
POINT_CLOUD_ASSET = "odm_georeferencing/odm_georeferenced_model.laz"
REPORT_ASSET = "odm_report/report.pdf"


class OdmProcessingError(RuntimeError):
    pass


@dataclass(frozen=True)
class OdmResult:
    """What a completed ODM run produced, and how to trace it."""

    task_uuid: str
    nodeodm_version: str
    odm_version: str
    archive: Path
    extracted: dict[str, Path]
    task_info: TaskInfo
    console_log: Path | None = None


def wanted_outputs(profile: ProcessingProfile) -> list[str]:
    """Which ODM products to include in the archive, from the profile's outputs section.

    Restricting this is the only lever against downloading every asset ODM produced — a full
    archive for a P1 block includes the textured mesh and every intermediate.
    """
    wanted = [profile.packaging.source_asset or ORTHO_ASSET]
    if profile.outputs.dsm:
        wanted.append(DSM_ASSET)
    if profile.outputs.point_cloud:
        wanted.append(POINT_CLOUD_ASSET)
    wanted.append(REPORT_ASSET)
    return wanted


def odm_options(profile: ProcessingProfile) -> dict[str, Any]:
    """Merge the profile's processing and radiometry options into one option set.

    Radiometry wins on a clash. Those settings are the ones under scientific control, and a
    tuning value must never quietly override a radiometric decision.
    """
    options: dict[str, Any] = dict(profile.processing.odm)
    options.update(profile.radiometry.odm)
    return options


def _control_uploads(block: ValidatedBlock) -> list[Path]:
    """Ground control and geolocation files, which ODM detects by name."""
    return list(block.block.control)


def submit(
    block: ValidatedBlock,
    profile: ProcessingProfile,
    client: NodeODMClient,
    *,
    validate_options: bool = True,
    extra_uploads: Sequence[Path] = (),
    overrides: dict[str, Any] | None = None,
) -> str:
    """Create a NodeODM task for a block. Returns the task uuid.

    `extra_uploads` carries files ODM detects by name that are not in the source folder — a
    `geo.txt` this pipeline generated, for instance. They go to the engine as uploads because
    the alternative is writing them next to the imagery, and a source tree stays as delivered.

    `overrides` carries options that describe the machine rather than the product, such as
    `max-concurrency`. They are merged over the profile's own options and validated with them,
    so an override the engine does not recognise fails here rather than being ignored.
    """
    if not block.is_processable:
        reasons = "; ".join(f"{f.name}: {f.detail}" for f in block.fatal)
        raise OdmProcessingError(f"{block.block.block_id} did not pass validation — {reasons}")

    options = odm_options(profile)
    options.update(overrides or {})
    if validate_options and options:
        unknown = client.unknown_options(options)
        if unknown:
            raise OdmProcessingError(
                f"the running engine does not recognise these options from profile "
                f"'{profile.profile_id}': {unknown}. Profiles must not name options the "
                "engine does not have, because ODM ignores them silently"
            )

    # Ground control travels with the imagery, not as an option. A generated file wins over one
    # of the same name in the source folder: it is the one this run can account for.
    generated = {path.name for path in extra_uploads}
    uploads = [
        *block.block.images,
        *(path for path in _control_uploads(block) if path.name not in generated),
        *extra_uploads,
    ]
    request = TaskRequest(
        name=block.block.block_id,
        images=uploads,
        options=options,
        outputs=wanted_outputs(profile),
    )
    uuid = client.create_task(request)
    logger.info(
        "odm task submitted",
        extra={
            "uuid": uuid,
            "block_id": block.block.block_id,
            "images": len(block.block.images),
            "control_files": len(uploads) - len(block.block.images),
            "profile_id": profile.profile_id,
        },
    )
    return uuid


def extract_assets(archive: Path, destination: Path, wanted: list[str]) -> dict[str, Path]:
    """Pull the named members out of all.zip.

    Members are matched by suffix so that an archive rooted at a task directory still
    resolves. Anything absent is reported by omission rather than by an invented path.
    """
    destination.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.namelist()
        for asset in wanted:
            match = next(
                (name for name in members if name == asset or name.endswith("/" + asset)), None
            )
            if match is None:
                continue
            target = destination / Path(asset).name
            with bundle.open(match) as source, target.open("wb") as handle:
                while chunk := source.read(1 << 20):
                    handle.write(chunk)
            found[asset] = target
    return found


def retrieve(
    uuid: str,
    profile: ProcessingProfile,
    client: NodeODMClient,
    *,
    engine_dir: Path,
) -> OdmResult:
    """Download and unpack a completed task."""
    info = client.task_info(uuid)
    if not info.is_terminal:
        raise OdmProcessingError(f"task {uuid} has not finished (progress {info.progress:.0f}%)")
    if not info.is_success:
        raise OdmProcessingError(
            f"task {uuid} failed: {info.status.errorMessage or 'no reason reported'}"
        )

    node = client.info()
    archive = client.download_archive(uuid, engine_dir / "all.zip")
    extracted = extract_assets(archive, engine_dir / "extracted", wanted_outputs(profile))

    console: Path | None = engine_dir / "engine_console.log"
    try:
        assert console is not None
        console.write_text("\n".join(client.task_output(uuid)), encoding="utf-8")
    except NodeODMError:
        console = None

    return OdmResult(
        task_uuid=uuid,
        nodeodm_version=node.version,
        odm_version=node.engineVersion,
        archive=archive,
        extracted=extracted,
        task_info=info,
        console_log=console,
    )


def source_ortho(result: OdmResult, profile: ProcessingProfile) -> SourceOrtho:
    """The orthophoto from an ODM run, in the same contract an external product arrives in.

    This is where the two producing paths converge: everything downstream of here treats an
    ODM product and a Terra product identically.
    """
    asset = profile.packaging.source_asset or ORTHO_ASSET
    path = result.extracted.get(asset)
    if path is None:
        raise OdmProcessingError(
            f"task {result.task_uuid} produced no {asset}; the archive contained "
            f"{sorted(result.extracted) or 'none of the requested assets'}"
        )

    from ..packaging.gdal_backend import RasterioGdalBackend

    description = RasterioGdalBackend().describe(path)
    return SourceOrtho(
        path=path,
        source_type=SourceType.ODM,
        processing_engine=ProcessingEngine.ODM,
        sha256=sha256_file(path),
        crs=description.crs,
        pixel_size_x=description.pixel_size_x,
        pixel_size_y=description.pixel_size_y,
        band_count=description.band_count,
    )
