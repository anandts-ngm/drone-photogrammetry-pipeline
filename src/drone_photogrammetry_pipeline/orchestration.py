"""Run sequencing.

One block at a time, in this process. Deliberately not a scheduler: a project run is a loop
that can be interrupted and restarted without any external state, because a completed block
is recognised from its own manifest rather than from a queue someone has to keep alive.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .integrity import sha256_file
from .log import configure, get_logger
from .models.enums import GateStatus, HeightType, SensorId, SourceType, WorkflowStatus
from .models.manifest import RunManifest
from .models.profile import load_profile
from .packaging.gdal_backend import PackagingError, PackagingPlan
from .packaging.raster import master_filename, package_master
from .processing.external import ExternalIngestError, ingest_external_ortho
from .qa.raster import run_raster_qa
from .reporting.manifest import read_manifest, write_manifest, write_qa_result
from .workspace import Workspace, make_run_id

_TRAILING_NUMBER = re.compile(r"(\d+)$")


@dataclass(frozen=True)
class IngestRequest:
    source_path: Path
    source_type: SourceType
    project_id: str
    block_id: str
    profile_id: str = "external_terra"
    allow_alpha_from_nodata: bool = False
    verify_pixels: bool = False
    declare_crs: str | None = None
    height_type: HeightType = HeightType.UNKNOWN

    # An external product carries no sensor identification in the file itself, so when a
    # delivery documents one it has to be supplied here. Overrides the profile, which is
    # null for external ingest.
    sensor: SensorId | None = None


@dataclass(frozen=True)
class IngestOutcome:
    block_id: str
    manifest_path: Path
    manifest: RunManifest | None = None
    master_path: Path | None = None
    source_bytes: int = 0
    master_bytes: int = 0
    seconds: float = 0.0
    reused: bool = False
    error: str | None = None

    @property
    def gate_status(self) -> GateStatus:
        return self.manifest.gate_status if self.manifest else GateStatus.FAIL


def natural_key(name: str) -> tuple[str, int]:
    """Sort B9 before B10, which a plain string sort would not."""
    match = _TRAILING_NUMBER.search(name)
    if match is None:
        return (name, 0)
    return (name[: match.start()], int(match.group(1)))


def discover_blocks(root: Path, asset_name: str) -> list[tuple[str, Path]]:
    """Directories directly under `root` that contain the named asset."""
    found = [
        (entry.name, entry / asset_name)
        for entry in root.iterdir()
        if entry.is_dir() and (entry / asset_name).is_file()
    ]
    return sorted(found, key=lambda item: natural_key(item[0]))


def find_completed_run(
    workspace: Workspace, project_id: str, block_id: str, source_sha256: str
) -> tuple[Path, RunManifest] | None:
    """An earlier run of this exact source whose master still exists.

    Matched on the source checksum rather than on the path, so a re-delivered file with the
    same name is correctly treated as new work rather than skipped.
    """
    runs = workspace.root / project_id / block_id / "runs"
    if not runs.is_dir():
        return None
    for run_dir in sorted(runs.iterdir(), reverse=True):
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = read_manifest(manifest_path)
        except ValueError:
            continue
        if manifest.gate_status not in (GateStatus.PASS, GateStatus.REVIEW):
            continue
        if manifest.packaging is None or manifest.packaging.source_sha256 != source_sha256:
            continue
        master = manifest.outputs.get("orthophoto_master")
        if master and Path(master).is_file():
            return manifest_path, manifest
    return None


def ingest_external_to_master(
    request: IngestRequest,
    settings: Settings,
    *,
    workspace: Workspace | None = None,
    reuse_completed: bool = True,
) -> IngestOutcome:
    """Ingest one external orthophoto, package it, QA it, and write its manifest."""
    space = workspace or Workspace(settings.workspace_root)
    space.guard_source(request.source_path)

    started = time.monotonic()
    source_bytes = request.source_path.stat().st_size
    source_sha256 = sha256_file(request.source_path)

    if reuse_completed:
        completed = find_completed_run(space, request.project_id, request.block_id, source_sha256)
        if completed is not None:
            manifest_path, manifest = completed
            master = Path(manifest.outputs["orthophoto_master"])
            return IngestOutcome(
                block_id=request.block_id,
                manifest_path=manifest_path,
                manifest=manifest,
                master_path=master,
                source_bytes=source_bytes,
                master_bytes=master.stat().st_size,
                seconds=time.monotonic() - started,
                reused=True,
            )

    run_id = make_run_id(request.block_id)
    paths = space.run_paths(request.project_id, request.block_id, run_id)
    paths.create()
    configure(settings.log_level, paths.pipeline_log)
    logger = get_logger("orchestration.ingest")

    loaded = load_profile(settings.profiles_dir, request.profile_id)
    manifest = RunManifest(
        run_id=run_id,
        project_id=request.project_id,
        block_id=request.block_id,
        sensor=request.sensor or loaded.profile.sensor,
        lens=loaded.profile.lens,
        profile_id=loaded.profile.profile_id,
        profile_version=loaded.profile.profile_version,
        profile_hash=loaded.profile_hash,
        started_at=datetime.now(UTC),
        processing_status=WorkflowStatus.PACKAGING,
        height_type=request.height_type,
        radiometry_policy=loaded.profile.radiometry.policy,
        radiometry_history=loaded.profile.radiometry.history,
    )
    logger.info(
        "run started",
        extra={"run_id": run_id, "block_id": request.block_id, "source": str(request.source_path)},
    )

    try:
        ortho = ingest_external_ortho(request.source_path, request.source_type)
        manifest.source_type = ortho.source_type
        manifest.processing_engine = ortho.processing_engine
        manifest.crs = ortho.crs

        spec = loaded.profile.packaging
        selection = spec.band_selection
        plan = PackagingPlan(
            band_selection=(
                (selection[0], selection[1], selection[2])
                if selection and len(selection) == 3
                else None
            ),
            allow_alpha_from_nodata=(
                request.allow_alpha_from_nodata or spec.allow_alpha_from_nodata
            ),
            verify_pixels=request.verify_pixels,
            declare_crs=request.declare_crs,
        )

        destination = paths.master_dir / master_filename(request.block_id)
        result = package_master(ortho.path, destination, plan=plan, source_sha256=source_sha256)
        manifest.packaging = result.record
        manifest.pixel_size_x = result.description.pixel_size_x
        manifest.pixel_size_y = result.description.pixel_size_y
        manifest.crs = result.description.crs
        manifest.processing_status = WorkflowStatus.PACKAGED

        qa_result = run_raster_qa(destination, alpha_provenance=result.record.alpha_provenance)
        write_qa_result(paths.qa_dir / "raster_qa.json", qa_result)

        manifest.raster_qa = qa_result
        manifest.gate_status = qa_result.status
        manifest.outputs = {"orthophoto_master": str(destination)}
        manifest.output_hashes = {"orthophoto_master": sha256_file(destination)}
        manifest.processing_status = WorkflowStatus.QA_COMPLETE
    except (ExternalIngestError, PackagingError) as error:
        manifest.processing_status = WorkflowStatus.FAILED
        manifest.finished_at = datetime.now(UTC)
        write_manifest(paths.manifest, manifest)
        logger.exception("run failed", extra={"run_id": run_id, "error": str(error)})
        return IngestOutcome(
            block_id=request.block_id,
            manifest_path=paths.manifest,
            manifest=manifest,
            source_bytes=source_bytes,
            seconds=time.monotonic() - started,
            error=str(error),
        )

    manifest.finished_at = datetime.now(UTC)
    write_manifest(paths.manifest, manifest)
    logger.info("run complete", extra={"run_id": run_id, "gate_status": manifest.gate_status.value})

    return IngestOutcome(
        block_id=request.block_id,
        manifest_path=paths.manifest,
        manifest=manifest,
        master_path=destination,
        source_bytes=source_bytes,
        master_bytes=destination.stat().st_size,
        seconds=time.monotonic() - started,
    )
