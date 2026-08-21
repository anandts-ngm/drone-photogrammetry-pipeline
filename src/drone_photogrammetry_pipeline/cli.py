"""Command line interface.

Everything printed here is presentation. The evidence for a run is the manifest, the QA
result and the JSONL processing log written under the workspace, never this output.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from . import __version__
from .config import Settings, get_settings
from .derive.mosaic import (
    MAX_GSD_RATIO,
    MosaicError,
    add_overviews,
    build_mosaic,
    overview_factors,
)
from .derive.overview import DEFAULT_GSD, build_overview, write_overview_jpeg
from .derive.preview import (
    DEFAULT_LONGEST_SIDE,
    DEFAULT_QUALITY,
    PreviewError,
    write_contact_sheet,
    write_preview,
)
from .harmonisation import HarmonisationError, solve_gain_offset, solve_gains
from .ingest.p1_geo import (
    GEO_FILENAME,
    NADIR_TOLERANCE_DEG,
    P1GeoError,
    describe,
    match_exposures,
    read_mark_file,
    read_metadata_csv,
    write_geo_file,
)
from .ingest.scan import scan_block
from .ingest.validate import validate_block
from .log import configure, console, error_console
from .models.enums import (
    CheckOutcome,
    GateStatus,
    HeightType,
    SensorId,
    SourceType,
    ValidationSeverity,
)
from .models.harmonisation import HarmonisationSolution
from .models.manifest import BlockRunSummary, ProjectRunSummary
from .models.profile import load_profile
from .models.project import ProjectConfigError, load_project, resolve_source_root
from .models.qa import RadiometricOverlapReport, RadiometricPairResult, RasterQAResult
from .nodeodm.client import NodeODMClient, NodeODMError
from .orchestration import (
    IngestOutcome,
    IngestRequest,
    SourceShape,
    describe_sources,
    discover_blocks,
    ingest_external_to_master,
    natural_key,
    package_source_ortho,
)
from .packaging.correction import correction_for
from .packaging.gdal_backend import PackagingError, PackagingPlan
from .packaging.raster import package_master
from .processing.odm import OdmProcessingError, retrieve, source_ortho, submit
from .qa.radiometry import DEFAULT_PATCH_METRES, DEFAULT_PATCHES, measure_project
from .qa.raster import run_raster_qa
from .reporting.manifest import (
    latest_radiometry_report,
    read_harmonisation_solution,
    read_radiometry_report,
    write_harmonisation,
    write_harmonisation_gains_csv,
    write_project_summary,
    write_radiometry_report,
)
from .workspace import Workspace, make_run_id

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Reproducible drone RGB photogrammetry and master orthophoto production.",
)

# PASS and FAIL are the obvious ones. REVIEW gets its own code so that a caller cannot treat
# "a human still has to look at this" as success.
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_REVIEW = 2

_EXIT_FOR_STATUS = {
    GateStatus.PASS: EXIT_PASS,
    GateStatus.REVIEW: EXIT_REVIEW,
    GateStatus.FAIL: EXIT_FAIL,
}

_STATUS_COLOUR = {
    GateStatus.PASS: "green",
    GateStatus.REVIEW: "yellow",
    GateStatus.FAIL: "red",
}

_GIB = 1024**3

# Measured over the Buduunkhad (92.3 -> 74.4 GiB, 96 min) and Sant (46.3 -> 33.4 GiB, 19 min)
# deliveries. Used only for the estimate a caller sees before committing to a run.
_MASTER_BYTES_PER_SOURCE_BYTE = 0.81
_PACKAGING_SECONDS_PER_GIB = 62.0


def _check_one_grid(sources: list[SourceShape]) -> None:
    """Refuse a delivery whose resolutions cannot share one mosaic grid.

    Checked here, on the sources, rather than only when the mosaic is built: the limit is a
    property of the delivery, and finding out about it after 96 minutes of packaging is finding
    out too late. One folder holding two cameras' orthophotos is the way this happens.
    """
    if not sources:
        return
    finest = min(sources, key=lambda s: s.pixel_size)
    coarsest = max(sources, key=lambda s: s.pixel_size)
    ratio = coarsest.pixel_size / finest.pixel_size if finest.pixel_size > 0 else float("inf")
    if ratio <= MAX_GSD_RATIO:
        return
    error_console().print(
        f"\n[bold red]FAILED[/bold red] the sources' pixel sizes span {ratio:.1f}x "
        f"({finest.pixel_size * 100:.2f} cm in {finest.block_id} to "
        f"{coarsest.pixel_size * 100:.2f} cm in {coarsest.block_id}), above the "
        f"{MAX_GSD_RATIO:.0f}x limit.\nThis usually means one folder holds two cameras' "
        "orthophotos. A single mosaic grid must be the finest size present, so a mixed folder "
        f"would need {ratio**2:.0f}x more pixels than the coarser blocks have. Put each camera "
        "in its own directory with its own project id."
    )
    raise typer.Exit(EXIT_FAIL)


def _free_bytes(path: Path) -> int:
    """Free space on the volume `path` will live on, whether or not it exists yet."""
    for candidate in (path, *path.parents):
        if candidate.exists():
            return shutil.disk_usage(candidate).free
    return 0


class ExternalSource(StrEnum):
    TERRA = "terra"


class HarmonisationModel(StrEnum):
    GAIN = "gain"
    GAIN_OFFSET = "gain-offset"


_SOURCE_TYPES = {ExternalSource.TERRA: SourceType.DJI_TERRA}


@app.command()
def version() -> None:
    """Print the pipeline version."""
    console().print(__version__)


@app.command("ingest-ortho")
def ingest_ortho(
    source_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="Externally produced orthophoto."),
    ],
    source: Annotated[
        ExternalSource, typer.Option("--source", help="Which external system produced it.")
    ],
    project_id: Annotated[str, typer.Option("--project-id", help="Project identifier.")],
    block_id: Annotated[str, typer.Option("--block-id", help="Acquisition block identifier.")],
    profile_id: Annotated[
        str, typer.Option("--profile", help="Processing profile id.")
    ] = "external_terra",
    allow_alpha_from_nodata: Annotated[
        bool,
        typer.Option(
            "--allow-alpha-from-nodata",
            help="Permit deriving alpha from an ambiguous NoData value. Flags the product REVIEW.",
        ),
    ] = False,
    verify_pixels: Annotated[
        bool,
        typer.Option("--verify-pixels", help="Compare RGB band checksums before and after."),
    ] = False,
    declare_crs: Annotated[
        str | None,
        typer.Option(
            "--declare-crs",
            help=(
                "CRS to write on the master, e.g. EPSG:32647+5705. Use when the delivery "
                "documents a vertical reference that the file header does not carry. The "
                "horizontal component must match the source."
            ),
        ),
    ] = None,
    height_type: Annotated[
        HeightType,
        typer.Option(
            "--height-type", help="Vertical reference of the delivery, from its metadata."
        ),
    ] = HeightType.UNKNOWN,
    sensor: Annotated[
        SensorId | None,
        typer.Option("--sensor", help="Sensor the delivery documents, if any."),
    ] = None,
) -> None:
    """Ingest an external orthophoto and package it to the master contract."""
    settings = get_settings()
    outcome = ingest_external_to_master(
        IngestRequest(
            source_path=source_path,
            source_type=_SOURCE_TYPES[source],
            project_id=project_id,
            block_id=block_id,
            profile_id=profile_id,
            allow_alpha_from_nodata=allow_alpha_from_nodata,
            verify_pixels=verify_pixels,
            declare_crs=declare_crs,
            height_type=height_type,
            sensor=sensor,
        ),
        settings,
        reuse_completed=False,
    )

    if outcome.error is not None or outcome.manifest is None:
        error_console().print(f"[bold red]FAILED[/bold red] {outcome.error}")
        error_console().print(f"manifest: {outcome.manifest_path}")
        raise typer.Exit(EXIT_FAIL)

    if outcome.manifest.raster_qa is not None:
        _render_qa(outcome.manifest.raster_qa)
    console().print(f"master:   {outcome.master_path}")
    console().print(f"manifest: {outcome.manifest_path}")
    raise typer.Exit(_EXIT_FOR_STATUS[outcome.gate_status])


@app.command()
def validate(
    root: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Block directory.")],
) -> None:
    """Inventory a block and report what is present, missing, or missing and fatal."""
    block = scan_block(root)
    validated = validate_block(block)

    console().print(
        f"[bold]{block.block_id}[/bold]  layout={block.layout}  "
        f"{block.image_count} images, {len(block.navigation)} navigation, "
        f"{len(block.control)} control, {len(block.checkpoints)} check points"
    )

    colour = {
        ValidationSeverity.REQUIRED_PRESENT: "green",
        ValidationSeverity.OPTIONAL_MISSING: "dim",
        ValidationSeverity.MISSING_ACCEPTABLE: "yellow",
        ValidationSeverity.MISSING_FATAL: "red",
    }
    table = Table(title=f"Validation: {block.block_id}")
    table.add_column("check")
    table.add_column("severity")
    table.add_column("detail")
    for finding in validated.findings:
        shade = colour[finding.severity]
        table.add_row(finding.name, f"[{shade}]{finding.severity.value}[/{shade}]", finding.detail)
    console().print(table)

    if not validated.suitable_for_absolute_z:
        console().print(
            "[yellow]not suitable for absolute-Z QA[/yellow]: no vertical reference is "
            "declared, so no downstream step may claim an absolute height result"
        )
    if not validated.is_processable:
        error_console().print("[bold red]NOT PROCESSABLE[/bold red]")
        raise typer.Exit(EXIT_FAIL)
    console().print("[green]processable[/green]")
    raise typer.Exit(EXIT_PASS)


@app.command("p1-geo")
def p1_geo(
    root: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, help="DJI P1 flight folder."),
    ],
    project_id: Annotated[str, typer.Option("--project-id", help="Project identifier.")],
    block_id: Annotated[
        str | None,
        typer.Option("--block-id", help="Block identifier; defaults to the folder name."),
    ] = None,
    write_geo: Annotated[
        bool,
        typer.Option(
            "--write-geo",
            help=(
                "Also write a geo.txt. Off by default: ODM already reads each image's RTK "
                "standard deviations from its XMP, and a positions-only geo file replaces them "
                "with ODM's 3 m default."
            ),
        ),
    ] = False,
    apply_lever_arm: Annotated[
        bool,
        typer.Option(
            "--apply-lever-arm",
            help=(
                "Add the mark file's antenna-to-camera offset to every position, and write the "
                "geo.txt that carries it. UNVERIFIED: whether DJI has already applied it is "
                "unsettled. See docs/decisions-and-verification.md section 2.16."
            ),
        ),
    ] = False,
) -> None:
    """Check a P1 flight folder's geolocation, and optionally write an ODM geo.txt.

    Reads the whole mark file, keeps only the images actually in the folder, and checks each
    match against that image's EXIF position before reporting anything.
    """
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)
    workspace.guard_source(root)

    block = scan_block(root, block_id=block_id)
    marks_files = [path for path in block.navigation if path.suffix.upper() == ".MRK"]
    if not marks_files:
        error_console().print(
            f"no .MRK mark file in {root}. A P1 folder carries one per flight; without it there "
            "is nothing here that the images' own EXIF does not already say"
        )
        raise typer.Exit(EXIT_FAIL)
    if not block.images:
        error_console().print(f"no images in {root}")
        raise typer.Exit(EXIT_FAIL)

    metadata = root / "metadata.csv"
    try:
        marks = read_mark_file(marks_files[0])
        exif = read_metadata_csv(metadata) if metadata.is_file() else None
        exposures = match_exposures(block.images, marks, exif)
    except P1GeoError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    found = describe(exposures)
    console().print(
        f"[bold]{block.block_id}[/bold]  {len(block.images)} images, "
        f"{len(marks)} exposures in {marks_files[0].name}"
    )
    console().print(f"matched:   {found.images}")
    if exif is None:
        console().print(
            "[yellow]no metadata.csv[/yellow]: the filename-to-exposure match could not be "
            "checked against EXIF, and the gimbal attitude is unknown"
        )
    else:
        console().print(
            f"nadir:     {found.nadir_within_tolerance}/{found.images} within "
            f"{NADIR_TOLERANCE_DEG:g} degree of straight down"
        )
    console().print(f"rtk flags: {', '.join(found.flags)}")
    console().print(
        f"accuracy:  {found.horizontal_accuracy_m * 100:.1f} cm horizontal, "
        f"{found.vertical_accuracy_m * 100:.1f} cm vertical (95th percentile). "
        "ODM reads these per image from the XMP on its own"
    )
    if found.lever_arm.worth_reporting:
        console().print(
            f"lever arm: {found.lever_arm.median_horizontal_m:.3f} m horizontal, "
            f"{found.lever_arm.median_up_m:+.3f} m up  "
            + ("[green]applied[/green]" if apply_lever_arm else "[dim]not applied[/dim]")
        )

    if not (write_geo or apply_lever_arm):
        console().print(
            f"\nno geo.txt written: nothing here improves on the EXIF. Submit with\n"
            f'  drone-photo process "{root}" --profile p1_35'
        )
        return

    try:
        written = write_geo_file(
            exposures,
            workspace.block_inputs_dir(project_id, block.block_id) / GEO_FILENAME,
            apply_lever_arm=apply_lever_arm,
            source=marks_files[0],
        )
    except P1GeoError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    console().print(f"geo:       {written.path}")
    console().print(
        f"\n[yellow]this file clears the per-image RTK weighting[/yellow]: set ODM's "
        f"--gps-accuracy to {found.horizontal_accuracy_m:.3f} in the profile, or it will use "
        f"its 3 m default.\nSubmit with\n"
        f'  drone-photo process "{root}" --profile p1_35 --geo "{written.path}"'
    )


@app.command()
def process(
    root: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Block directory.")],
    profile_id: Annotated[str, typer.Option("--profile", help="Processing profile id.")],
    geo: Annotated[
        Path | None,
        typer.Option(
            "--geo",
            exists=True,
            dir_okay=False,
            help="A geo.txt to upload with the imagery, as written by 'p1-geo'.",
        ),
    ] = None,
    node_url: Annotated[
        str | None, typer.Option("--node", help="NodeODM base URL; defaults to settings.")
    ] = None,
) -> None:
    """Submit a block to NodeODM for reconstruction. Returns immediately with the task id."""
    settings = get_settings()
    validated = validate_block(scan_block(root))
    loaded = load_profile(settings.profiles_dir, profile_id)

    with NodeODMClient(node_url or settings.nodeodm_url) as client:
        if not client.health():
            error_console().print(
                f"no NodeODM at {node_url or settings.nodeodm_url}; "
                "run 'docker compose up -d' first"
            )
            raise typer.Exit(EXIT_FAIL)
        node = client.info()
        try:
            uuid = submit(
                validated,
                loaded.profile,
                client,
                extra_uploads=[geo] if geo is not None else [],
            )
        except (OdmProcessingError, NodeODMError) as error:
            error_console().print(f"[bold red]FAILED[/bold red] {error}")
            raise typer.Exit(EXIT_FAIL) from error

    console().print(f"engine:  {node.engine} {node.engineVersion} (NodeODM {node.version})")
    console().print(f"task:    {uuid}")
    console().print(f"monitor: drone-photo status {uuid}")


@app.command()
def status(
    task_uuid: Annotated[str, typer.Argument(help="NodeODM task id.")],
    node_url: Annotated[str | None, typer.Option("--node")] = None,
    follow: Annotated[
        bool, typer.Option("--follow", help="Poll until the task reaches a terminal state.")
    ] = False,
) -> None:
    """Report a task's progress, optionally following it to completion."""
    settings = get_settings()
    with NodeODMClient(node_url or settings.nodeodm_url) as client:
        try:
            if follow:
                info = client.wait(
                    task_uuid,
                    poll_seconds=15.0,
                    on_progress=lambda i: console().print(
                        f"  {i.progress:5.1f}%  {i.code.name if i.code else 'UNKNOWN'}"
                    ),
                )
            else:
                info = client.task_info(task_uuid)
        except NodeODMError as error:
            error_console().print(f"[bold red]FAILED[/bold red] {error}")
            raise typer.Exit(EXIT_FAIL) from error

    state = info.code.name if info.code else "UNKNOWN"
    console().print(f"{task_uuid}  {state}  {info.progress:.1f}%  images={info.imagesCount}")
    if info.status.errorMessage:
        error_console().print(f"error: {info.status.errorMessage}")
    raise typer.Exit(EXIT_PASS if info.is_success else EXIT_FAIL)


@app.command()
def fetch(
    task_uuid: Annotated[str, typer.Argument(help="NodeODM task id.")],
    project_id: Annotated[str, typer.Option("--project-id")],
    block_id: Annotated[str, typer.Option("--block-id")],
    profile_id: Annotated[str, typer.Option("--profile")],
    node_url: Annotated[str | None, typer.Option("--node")] = None,
    declare_crs: Annotated[str | None, typer.Option("--declare-crs")] = None,
    height_type: Annotated[HeightType, typer.Option("--height-type")] = HeightType.UNKNOWN,
    verify_pixels: Annotated[bool, typer.Option("--verify-pixels")] = False,
) -> None:
    """Download a finished task, package its orthophoto and run QA."""
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)
    loaded = load_profile(settings.profiles_dir, profile_id)

    run_id = make_run_id(block_id)
    paths = workspace.run_paths(project_id, block_id, run_id)
    paths.create()
    configure(settings.log_level, paths.pipeline_log)

    with NodeODMClient(node_url or settings.nodeodm_url) as client:
        try:
            result = retrieve(task_uuid, loaded.profile, client, engine_dir=paths.engine_dir)
            ortho = source_ortho(result, loaded.profile)
        except (OdmProcessingError, NodeODMError) as error:
            error_console().print(f"[bold red]FAILED[/bold red] {error}")
            raise typer.Exit(EXIT_FAIL) from error

    outcome = package_source_ortho(
        ortho,
        loaded,
        paths,
        project_id=project_id,
        block_id=block_id,
        run_id=run_id,
        declare_crs=declare_crs,
        height_type=height_type,
        verify_pixels=verify_pixels,
        odm_result=result,
    )
    if outcome.manifest is not None and outcome.manifest.raster_qa is not None:
        _render_qa(outcome.manifest.raster_qa)
    console().print(f"engine:   odm {result.odm_version}")
    console().print(f"master:   {outcome.master_path}")
    console().print(f"manifest: {outcome.manifest_path}")
    raise typer.Exit(_EXIT_FOR_STATUS[outcome.gate_status])


@app.command("run-project")
def run_project(
    root: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            help="Directory holding one sub-directory per block.",
        ),
    ],
    source: Annotated[
        ExternalSource, typer.Option("--source", help="Which external system produced them.")
    ],
    project_id: Annotated[str, typer.Option("--project-id", help="Project identifier.")],
    asset: Annotated[
        str,
        typer.Option("--asset", help="Orthophoto filename inside each block directory."),
    ] = "dom.tif",
    profile_id: Annotated[str, typer.Option("--profile")] = "external_terra",
    allow_alpha_from_nodata: Annotated[bool, typer.Option("--allow-alpha-from-nodata")] = False,
    verify_pixels: Annotated[bool, typer.Option("--verify-pixels")] = False,
    declare_crs: Annotated[str | None, typer.Option("--declare-crs")] = None,
    height_type: Annotated[HeightType, typer.Option("--height-type")] = HeightType.UNKNOWN,
    sensor: Annotated[
        SensorId | None,
        typer.Option("--sensor", help="Sensor the delivery documents, if any."),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Process at most this many blocks.")
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Reprocess blocks that already have a completed run."),
    ] = False,
    harmonisation: Annotated[
        Path | None,
        typer.Option(
            "--harmonise-with",
            exists=True,
            dir_okay=False,
            help=(
                "Harmonisation solution to apply while packaging. Gains are applied in the "
                "space the solution declares. Incompatible with --verify-pixels."
            ),
        ),
    ] = None,
) -> None:
    """Package every block under a project directory to the master contract.

    Sequential and resumable: a block whose source checksum already has a completed run with
    its master still on disk is reused rather than reprocessed, so an interrupted project run
    can simply be restarted.
    """
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)

    blocks = discover_blocks(root, asset)
    if not blocks:
        error_console().print(f"no sub-directory of {root} contains {asset}")
        raise typer.Exit(EXIT_FAIL)
    if limit is not None:
        blocks = blocks[:limit]

    # Checked before any block is read. The backend refuses this combination too, but by then
    # it has hashed a multi-gigabyte source, and over 79 blocks that is an hour spent to learn
    # something knowable from the arguments alone.
    if harmonisation is not None and verify_pixels:
        error_console().print(
            "[bold red]--verify-pixels and --harmonise-with are contradictory[/bold red]: "
            "verification asserts the pixels are unchanged, correction changes them"
        )
        raise typer.Exit(EXIT_FAIL)

    solution = None
    if harmonisation is not None:
        solution = read_harmonisation_solution(harmonisation)
        solved = {b.block_id for b in solution.blocks}
        unsolved = sorted({block_id for block_id, _ in blocks} - solved, key=natural_key)
        console().print(
            f"correcting with {harmonisation.name}: "
            f"[bold]{solution.space.value}[/bold] space, {len(solved)} blocks solved"
        )
        # Named rather than counted. A block with no measured overlap is packaged unchanged,
        # and which blocks those are decides whether the set is seamless or merely mostly so.
        if unsolved:
            console().print(f"[yellow]uncorrected (not in solution): {' '.join(unsolved)}[/yellow]")

    console().print(f"[bold]{project_id}[/bold]: {len(blocks)} blocks from {root}")
    summary = _package_blocks(
        blocks,
        settings=settings,
        workspace=workspace,
        root=root,
        source_type=_SOURCE_TYPES[source],
        project_id=project_id,
        profile_id=profile_id,
        allow_alpha_from_nodata=allow_alpha_from_nodata,
        verify_pixels=verify_pixels,
        declare_crs=declare_crs,
        height_type=height_type,
        sensor=sensor,
        solution=solution,
        force=force,
    )
    raise typer.Exit(_project_exit_code(summary))


def _project_exit_code(summary: ProjectRunSummary) -> int:
    if summary.failed:
        return EXIT_FAIL
    if summary.review:
        return EXIT_REVIEW
    return EXIT_PASS


def _package_blocks(
    blocks: list[tuple[str, Path]],
    *,
    settings: Settings,
    workspace: Workspace,
    root: Path,
    source_type: SourceType,
    project_id: str,
    profile_id: str,
    allow_alpha_from_nodata: bool,
    verify_pixels: bool,
    declare_crs: str | None,
    height_type: HeightType,
    sensor: SensorId | None,
    solution: HarmonisationSolution | None,
    force: bool,
) -> ProjectRunSummary:
    """Package every block and write the run summary.

    Extracted so that `run-project` and `process-project` share one implementation rather than
    two that drift. Returns the summary instead of exiting, because a caller that has further
    stages to run must not be terminated by this one.
    """
    started_at = datetime.now(UTC)
    outcomes: list[IngestOutcome] = []

    for index, (block_id, source_path) in enumerate(blocks, start=1):
        outcome = ingest_external_to_master(
            IngestRequest(
                source_path=source_path,
                source_type=source_type,
                project_id=project_id,
                block_id=block_id,
                profile_id=profile_id,
                allow_alpha_from_nodata=allow_alpha_from_nodata,
                verify_pixels=verify_pixels,
                declare_crs=declare_crs,
                height_type=height_type,
                sensor=sensor,
                correction=(correction_for(solution, block_id) if solution is not None else None),
            ),
            settings,
            workspace=workspace,
            reuse_completed=not force,
        )
        outcomes.append(outcome)
        _render_block_line(index, len(blocks), outcome)

    summary = ProjectRunSummary(
        project_id=project_id,
        source_root=str(root),
        started_at=started_at,
        finished_at=datetime.now(UTC),
        blocks=[_block_summary(outcome) for outcome in outcomes],
    )
    summary_path = write_project_summary(
        workspace.reports_dir(project_id, "runs") / f"run_project_{started_at:%Y%m%dT%H%M%SZ}.json",
        summary,
    )
    _render_project_summary(summary, summary_path)
    return summary


def _render_previews(
    workspace: Workspace,
    project_id: str,
    masters: list[tuple[str, Path]],
    *,
    longest_side: int = DEFAULT_LONGEST_SIDE,
    quality: int = DEFAULT_QUALITY,
    apply_destripe: bool = True,
    contact_sheet: bool = True,
) -> None:
    """Render one preview per master, plus a contact sheet of the project.

    Shared with `process-project` so both write the same files under the same names. A derived
    product whose location depends on which command produced it is not much of a product.
    """
    directory = workspace.derived_dir(project_id) / "previews"
    console().print(f"[bold]{project_id}[/bold]: rendering {len(masters)} previews")

    rendered: list[tuple[str, Any]] = []
    written = 0
    reductions: list[float] = []
    for index, (block_id, master) in enumerate(masters, start=1):
        try:
            preview, image, destriped = write_preview(
                master,
                directory / f"{block_id}_preview.jpg",
                longest_side=longest_side,
                quality=quality,
                apply_destripe=apply_destripe,
            )
        except PreviewError as error:
            # One unreadable master costs its own preview, not the other 78. That block still
            # has its master and its manifest; only the picture is missing.
            error_console().print(f"[yellow]{block_id}: {error}[/yellow]")
            continue
        written += preview.bytes_written
        rendered.append((block_id, image))
        # Reported per block rather than only in aggregate: a block whose banding barely moved
        # is worth seeing, because it means the ripple there was not full-length coherent.
        banding = ""
        if destriped is not None:
            reductions.append(destriped.reduction_pct)
            banding = (
                f"  ripple {destriped.ripple_before_pct:.2f}% -> "
                f"{destriped.ripple_after_pct:.2f}% ({destriped.reduction_pct:+.0f}%)"
            )
        console().print(
            f"[dim]{index:>4}/{len(masters)}[/dim] {block_id:<5} "
            f"{preview.width}x{preview.height}  {preview.bytes_written / 1024:.0f} KB{banding}"
        )

    console().print(
        f"previews: {len(rendered)} files, {written / 1024 / 1024:.1f} MB in {directory}"
    )
    if reductions:
        console().print(
            f"destriped: {len(reductions)} blocks, mean ripple reduction "
            f"{sum(reductions) / len(reductions):.0f}%"
        )

    if contact_sheet and rendered:
        sheet = write_contact_sheet(
            rendered,
            workspace.derived_dir(project_id)
            / f"{Workspace.project_slug(project_id)}_contact_sheet.jpg",
        )
        console().print(
            f"sheet:    {sheet.path} ({sheet.width}x{sheet.height}, "
            f"{sheet.bytes_written / 1024 / 1024:.1f} MB)"
        )


def _build_project_overview(
    workspace: Workspace,
    project_id: str,
    masters: list[tuple[str, Path]],
    *,
    gsd: float = DEFAULT_GSD,
    apply_destripe: bool = True,
    jpeg: bool = True,
) -> None:
    """Assemble one small browsable raster covering the whole project."""
    slug = Workspace.project_slug(project_id)
    console().print(
        f"[bold]{project_id}[/bold]: assembling {len(masters)} blocks at {gsd:g} m"
        + ("  (destriping)" if apply_destripe else "")
    )

    # Labelled from the block list rather than from the path: a master sits at
    # <block>/runs/<run_id>/master/<file>.tif, and counting parents to reach the block id is
    # off by one in a way that labels every line "runs".
    block_of = {path: block_id for block_id, path in masters}

    def show(index: int, total: int, master: Path, result: Any) -> None:
        detail = ""
        if result is not None:
            detail = (
                f"  ripple {result.ripple_before_pct:.2f}% -> "
                f"{result.ripple_after_pct:.2f}% ({result.reduction_pct:+.0f}%)"
            )
        console().print(
            f"[dim]{index:>4}/{total}[/dim] {block_of.get(master, master.stem):<5}{detail}"
        )

    try:
        built = build_overview(
            [path for _, path in masters],
            workspace.derived_dir(project_id) / f"{slug}_overview.tif",
            gsd=gsd,
            apply_destripe=apply_destripe,
            progress=show,
        )
    except PreviewError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    console().print(
        f"\noverview: {built.width:,} x {built.height:,} px at {built.gsd:g} m  "
        f"{built.bytes_written / 1024 / 1024:.1f} MB"
    )
    if built.destriped:
        console().print(
            f"destriped {built.destriped}/{built.blocks} blocks, "
            f"mean ripple reduction {built.mean_ripple_reduction_pct:.0f}%"
        )
    console().print(f"path:     {built.path}")

    if jpeg:
        flat = write_overview_jpeg(built.path, built.path.with_name(f"{slug}_overview.jpg"))
        console().print(f"jpeg:     {flat}  {flat.stat().st_size / 1024 / 1024:.1f} MB")


def _build_project_mosaic(
    workspace: Workspace,
    project_id: str,
    masters: list[tuple[str, Path]],
    *,
    overviews: bool,
) -> None:
    """Write the virtual mosaic, and optionally its pyramid."""
    destination = (
        workspace.derived_dir(project_id) / f"{Workspace.project_slug(project_id)}_mosaic.vrt"
    )
    try:
        built = build_mosaic([path for _, path in masters], destination)
    except MosaicError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    console().print(
        f"[bold]{project_id}[/bold]: {built.sources} masters -> "
        f"{built.width:,} x {built.height:,} px ({built.gigapixels:.1f} Gpx) "
        f"at {built.pixel_size * 100:.2f} cm"
    )
    console().print(f"crs:    {built.crs}")
    console().print(f"mosaic: {built.path}")

    if not overviews:
        console().print(
            "[yellow]no overviews[/yellow]: a viewer asked for the full extent must read every "
            "pixel of every master, which in practice does not finish. Build them later with "
            "'drone-photo mosaic', or browse the overview instead"
        )
        return

    factors = overview_factors(built.width, built.height)
    console().print(f"building overviews {factors} (one pass over the masters)...")
    present = add_overviews(built.path, factors)
    pyramid = built.path.with_suffix(built.path.suffix + ".ovr")
    size = pyramid.stat().st_size / _GIB if pyramid.is_file() else 0.0
    console().print(f"overviews: {present}   {pyramid.name}  {size:.2f} GiB")


@app.command()
def radiometry(
    root: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, help="Directory of block sub-directories."),
    ],
    project_id: Annotated[str, typer.Option("--project-id", help="Project identifier.")],
    asset: Annotated[str, typer.Option("--asset")] = "dom.tif",
    min_overlap_ha: Annotated[
        float, typer.Option("--min-overlap-ha", help="Ignore pairs sharing less than this.")
    ] = 1.0,
    patches: Annotated[
        int, typer.Option("--patches", help="Ground patches sampled per pair.")
    ] = DEFAULT_PATCHES,
    patch_metres: Annotated[
        float, typer.Option("--patch-metres", help="Side length of each ground patch.")
    ] = DEFAULT_PATCH_METRES,
    linearise: Annotated[
        bool,
        typer.Option(
            "--linearise/--no-linearise",
            help=(
                "Invert the sRGB transfer function before measuring, so the numbers describe "
                "light rather than display values. A gain solved from linearised medians is a "
                "gain in radiance."
            ),
        ),
    ] = False,
) -> None:
    """Measure how much overlapping blocks disagree radiometrically.

    Reports numbers only. No pair is passed or failed, because no threshold has been
    established yet — that is a benchmark exercise, not a judgement call.
    """
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)

    blocks = discover_blocks(root, asset)
    if len(blocks) < 2:
        error_console().print(f"need at least two blocks under {root} containing {asset}")
        raise typer.Exit(EXIT_FAIL)

    console().print(f"[bold]{project_id}[/bold]: {len(blocks)} blocks, measuring overlaps")

    def show(index: int, total: int, result: RadiometricPairResult) -> None:
        if result.sample_pixels == 0:
            console().print(
                f"[dim]{index:>4}/{total}[/dim] {result.block_a:>4} / {result.block_b:<4} "
                f"[dim]{result.note}[/dim]"
            )
            return
        per_band = "  ".join(
            f"{b.band[0].upper()}{b.relative_difference_pct:+6.1f}%" for b in result.bands
        )
        console().print(
            f"[dim]{index:>4}/{total}[/dim] {result.block_a:>4} / {result.block_b:<4} "
            f"{result.overlap_area_ha:7.1f} ha  {per_band}"
        )

    report = measure_project(
        project_id,
        blocks,
        min_overlap_ha=min_overlap_ha,
        patches=patches,
        patch_metres=patch_metres,
        linearise=linearise,
        progress=show,
    )
    stamp = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
    # The filename carries the encoding because a linear and an encoded report are not
    # interchangeable inputs to `harmonise`, and the difference is invisible in the numbers.
    suffix = "_linear" if linearise else ""
    path = write_radiometry_report(
        workspace.reports_dir(project_id, "radiometry") / f"radiometry_{stamp}{suffix}.json",
        report,
    )
    _render_radiometry(report, path)


@app.command()
def harmonise(
    project_id: Annotated[str, typer.Option("--project-id", help="Project identifier.")],
    report_path: Annotated[
        Path | None,
        typer.Option("--report", help="Radiometry report; defaults to the newest for the project."),
    ] = None,
    weight_by_samples: Annotated[
        bool,
        typer.Option(
            "--weight/--no-weight",
            help="Weight each constraint by the square root of its sample count.",
        ),
    ] = True,
    model: Annotated[
        HarmonisationModel,
        typer.Option(
            "--model",
            help=(
                "'gain' fixes a scale difference between blocks. 'gain-offset' also fixes a "
                "black-level difference, which no gain can reach."
            ),
        ),
    ] = HarmonisationModel.GAIN,
) -> None:
    """Solve one radiometric gain per block per band from measured overlaps.

    Writes the coefficients and the before/after residuals. It does not modify any master:
    applying the gains produces a separate derived product.
    """
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)
    measurements = workspace.reports_dir(project_id, "radiometry")
    solutions = workspace.reports_dir(project_id, "harmonisation")

    chosen = report_path or latest_radiometry_report(measurements)
    if chosen is None or not chosen.is_file():
        error_console().print(
            f"no radiometry report for {project_id} in {measurements}; "
            "run 'drone-photo radiometry' first"
        )
        raise typer.Exit(EXIT_FAIL)

    report = read_radiometry_report(chosen)
    try:
        solution = (
            solve_gain_offset(report, weight_by_samples=weight_by_samples)
            if model is HarmonisationModel.GAIN_OFFSET
            else solve_gains(report, weight_by_samples=weight_by_samples)
        )
    except HarmonisationError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    stamp = solution.generated_at.strftime("%Y%m%dT%H%M%SZ")
    # The model is in the filename because a gain solution and a gain+offset solution are not
    # interchangeable and are indistinguishable at a glance once written.
    name = f"harmonisation_{stamp}_{solution.model}_{solution.space.value}"
    solution_path = write_harmonisation(solutions / f"{name}.json", solution)
    csv_path = write_harmonisation_gains_csv(solutions / f"{name}.csv", solution)
    _render_harmonisation(solution, solution_path, csv_path)


@app.command("process-project")
def process_project(
    config_path: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, help="Project configuration YAML."),
    ],
    source_root: Annotated[
        Path | None,
        typer.Option(
            "--source-root",
            exists=True,
            file_okay=False,
            help=(
                "Directory holding this delivery's block directories. Defaults to the project "
                "configuration's source_root, then to DPP_INPUTS_ROOT/<project>."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would run, and what is already done."),
    ] = False,
    correct: Annotated[
        bool,
        typer.Option(
            "--correct/--no-correct",
            help="Measure overlaps, solve gains, and apply them while packaging.",
        ),
    ] = True,
    derived: Annotated[
        bool,
        typer.Option("--derived/--no-derived", help="Build previews, overview and mosaic."),
    ] = True,
    overviews: Annotated[
        bool,
        typer.Option(
            "--overviews/--no-overviews",
            help="Build the mosaic pyramid. Reads every master once; hours on a large survey.",
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Repackage blocks that already have a completed run.")
    ] = False,
) -> None:
    """Take a delivery from source orthophotos to finished products, in one command.

    The order matters for time rather than correctness. Overlaps are measured on the *sources*
    first, so the gains are known before any master is written and each block is packaged once.
    Packaging first and correcting afterwards would repackage the whole delivery: on the
    79-block Buduunkhad survey that is an extra 96 minutes.
    """
    settings = get_settings()
    try:
        project = load_project(config_path)
    except ProjectConfigError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    if project.source_type is None:
        error_console().print(
            f"{project.project_id}: source_type is unset, so this is a raw-imagery project. "
            "process-project ingests delivered orthophotos; use the ODM path for raw flights"
        )
        raise typer.Exit(EXIT_FAIL)

    workspace = Workspace(settings.workspace_root)
    try:
        root = resolve_source_root(
            project,
            inputs_root=settings.inputs_root,
            slug=Workspace.project_slug(project.project_id),
            override=source_root,
        )
    except ProjectConfigError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    blocks = discover_blocks(root, project.asset)
    if not blocks:
        error_console().print(
            f"no sub-directory of {root} contains {project.asset}. Each block belongs in its own "
            f"directory, named for the block, with the orthophoto inside it as {project.asset}"
        )
        raise typer.Exit(EXIT_FAIL)

    source_bytes = sum(path.stat().st_size for _, path in blocks)
    existing = len(workspace.find_masters(project.project_id))
    stages = [
        ("measure overlaps", correct),
        ("solve gains", correct),
        ("package masters", True),
        ("previews + contact sheet", derived),
        ("overview", derived),
        ("mosaic", derived),
        ("mosaic pyramid", derived and overviews),
    ]

    console().print(f"[bold]{project.project_id}[/bold]  {config_path}")
    console().print(f"source:    {root}")
    console().print(f"blocks:    {len(blocks)}   {source_bytes / _GIB:.1f} GiB")
    console().print(f"workspace: {workspace.project_dir(project.project_id)}")
    if project.declare_crs:
        console().print(f"crs:       {project.declare_crs}  heights {project.height_type.value}")
    else:
        console().print(
            "crs:       from the source headers; no vertical reference declared, so the "
            "products are not suitable for absolute-Z work"
        )
    if existing:
        console().print(
            f"[dim]{existing} block(s) already have a master; unchanged sources are reused "
            f"{'(overridden by --force)' if force else ''}[/dim]"
        )
    console().print("")
    for name, enabled in stages:
        console().print(f"  {'[green]run[/green] ' if enabled else '[dim]skip[/dim]'}  {name}")

    _check_one_grid(describe_sources(blocks))

    # Measured over both surveys: packaging writes 0.81 GiB per GiB read, at about 62 s per GiB.
    # Blocks that already have a master are skipped, so only the rest are estimated.
    remaining = len(blocks) if force else max(0, len(blocks) - existing)
    share = remaining / len(blocks)
    estimate = source_bytes * share * _MASTER_BYTES_PER_SOURCE_BYTE
    free = _free_bytes(workspace.root)
    console().print(
        f"\n[dim]estimate: {remaining} block(s) to package, about {estimate / _GIB:.0f} GiB "
        f"written in about {source_bytes * share / _GIB * _PACKAGING_SECONDS_PER_GIB / 60:.0f} "
        f"min. {free / _GIB:.0f} GiB free.[/dim]"
    )
    # Warned rather than refused: the estimate is a ratio measured elsewhere, and the caller
    # knows things it does not. Running out of room 80 minutes in is the failure worth avoiding.
    if free < estimate:
        error_console().print(
            f"[yellow]not enough room[/yellow]: {free / _GIB:.0f} GiB free on the workspace "
            f"volume against an estimated {estimate / _GIB:.0f} GiB of masters"
        )

    if dry_run:
        console().print("[dim]nothing was written.[/dim]")
        raise typer.Exit(EXIT_PASS)

    solution_path: Path | None = None
    if correct:
        console().print("\n[bold]measuring overlaps[/bold] (on the sources, before packaging)")

        usable = 0

        def measured(index: int, total: int, result: RadiometricPairResult) -> None:
            # A heartbeat rather than a line per pair: this survey has 231 of them, and they
            # would bury the per-block packaging lines a reader is waiting on. `radiometry`
            # prints every pair for anyone who wants them.
            nonlocal usable
            if result.bands:
                usable += 1
            if index % 25 == 0 or index == total:
                console().print(f"[dim]  {index:>4}/{total} pairs, {usable} usable[/dim]")

        report = measure_project(
            project.project_id,
            blocks,
            linearise=True,
            progress=measured,
        )
        stamp = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
        report_path = write_radiometry_report(
            workspace.reports_dir(project.project_id, "radiometry")
            / f"radiometry_{stamp}_linear.json",
            report,
        )
        console().print(f"  {report.measured_count} of {report.pair_count} pairs measured")

        try:
            solution = solve_gains(report)
        except HarmonisationError as error:
            error_console().print(f"[bold red]FAILED[/bold red] {error}")
            raise typer.Exit(EXIT_FAIL) from error
        name = f"harmonisation_{stamp}_{solution.model}_{solution.space.value}"
        solutions = workspace.reports_dir(project.project_id, "harmonisation")
        solution_path = write_harmonisation(solutions / f"{name}.json", solution)
        write_harmonisation_gains_csv(solutions / f"{name}.csv", solution)
        _render_harmonisation(solution, solution_path, solutions / f"{name}.csv")
        console().print(f"[dim]measured from {report_path.name}[/dim]")

    console().print("\n[bold]packaging masters[/bold]")
    summary = _package_blocks(
        blocks,
        settings=settings,
        workspace=workspace,
        root=root,
        source_type=project.source_type,
        project_id=project.project_id,
        profile_id=project.profile_id,
        allow_alpha_from_nodata=project.allow_alpha_from_nodata,
        # Verification asserts the pixels are unchanged, so it cannot coexist with a
        # correction. Honoured rather than refused here: the config asks for both only because
        # correcting is the default, and silently dropping the weaker guarantee is right.
        verify_pixels=project.verify_pixels and solution_path is None,
        declare_crs=project.declare_crs,
        height_type=project.height_type,
        sensor=project.sensor,
        solution=read_harmonisation_solution(solution_path) if solution_path else None,
        force=force,
    )
    if summary.failed:
        error_console().print(
            "\n[bold red]stopping[/bold red]: blocks failed to package, so the derived products "
            "would describe an incomplete set"
        )
        raise typer.Exit(EXIT_FAIL)

    if derived:
        masters = workspace.find_masters(project.project_id)

        console().print("\n[bold]previews[/bold]")
        _render_previews(
            workspace,
            project.project_id,
            masters,
            longest_side=project.preview_longest_side,
            apply_destripe=project.destripe_previews,
        )

        console().print("\n[bold]overview[/bold]")
        _build_project_overview(
            workspace,
            project.project_id,
            masters,
            gsd=project.overview_gsd,
            apply_destripe=project.destripe_previews,
        )

        console().print("\n[bold]mosaic[/bold]")
        _build_project_mosaic(workspace, project.project_id, masters, overviews=overviews)

    console().print(f"\n[bold]done[/bold]  {workspace.project_dir(project.project_id)}")
    raise typer.Exit(_project_exit_code(summary))


@app.command()
def mosaic(
    project_id: Annotated[str, typer.Option("--project-id", help="Project identifier.")],
    overviews: Annotated[
        bool,
        typer.Option(
            "--overviews/--no-overviews",
            help=(
                "Build an external pyramid beside the mosaic. Without it a zoomed-out view has "
                "to read every pixel of every master, which does not finish. Costs one full "
                "pass over the masters."
            ),
        ),
    ] = True,
) -> None:
    """Write a virtual mosaic (.vrt) addressing every master in a project.

    Opens in QGIS as a single raster layer. Costs kilobytes: GDAL reads the masters on demand
    rather than copying them.
    """
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)

    masters = workspace.find_masters(project_id)
    if not masters:
        error_console().print(
            f"no masters for {project_id} under {workspace.project_dir(project_id)}; "
            "run 'drone-photo run-project' first"
        )
        raise typer.Exit(EXIT_FAIL)

    _build_project_mosaic(workspace, project_id, masters, overviews=overviews)


@app.command()
def previews(
    project_id: Annotated[str, typer.Option("--project-id", help="Project identifier.")],
    longest_side: Annotated[
        int, typer.Option("--longest-side", help="Long edge of each preview, in pixels.")
    ] = DEFAULT_LONGEST_SIDE,
    quality: Annotated[
        int, typer.Option("--quality", help="JPEG quality, 1-95.")
    ] = DEFAULT_QUALITY,
    contact_sheet: Annotated[
        bool,
        typer.Option(
            "--contact-sheet/--no-contact-sheet",
            help="Also write one page showing every block, labelled.",
        ),
    ] = True,
    apply_destripe: Annotated[
        bool,
        typer.Option(
            "--destripe/--no-destripe",
            help=(
                "Remove flight-strip banding. Applied to previews only, never to a master: "
                "no directional filter can tell a stripe from real linear geology, and a "
                "preview is the one place where getting that wrong costs nothing."
            ),
        ),
    ] = True,
) -> None:
    """Render a small JPEG of every master, plus a contact sheet of the project."""
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)

    masters = workspace.find_masters(project_id)
    if not masters:
        error_console().print(f"no masters for {project_id}; run 'drone-photo run-project' first")
        raise typer.Exit(EXIT_FAIL)

    _render_previews(
        workspace,
        project_id,
        masters,
        longest_side=longest_side,
        quality=quality,
        apply_destripe=apply_destripe,
        contact_sheet=contact_sheet,
    )


@app.command()
def overview(
    project_id: Annotated[str, typer.Option("--project-id", help="Project identifier.")],
    gsd: Annotated[
        float, typer.Option("--gsd", help="Ground sample distance of the overview, in metres.")
    ] = DEFAULT_GSD,
    apply_destripe: Annotated[
        bool, typer.Option("--destripe/--no-destripe", help="Remove flight-strip banding.")
    ] = True,
    jpeg: Annotated[
        bool, typer.Option("--jpeg/--no-jpeg", help="Also write a flat JPEG beside it.")
    ] = True,
) -> None:
    """Assemble one destriped, browsable image of a whole project.

    Unlike the virtual mosaic this is a single small raster that opens instantly. Overlapping
    blocks are averaged rather than overwritten, so seams soften instead of stepping.
    """
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)

    masters = workspace.find_masters(project_id)
    if not masters:
        error_console().print(f"no masters for {project_id}; run 'drone-photo run-project' first")
        raise typer.Exit(EXIT_FAIL)

    _build_project_overview(
        workspace, project_id, masters, gsd=gsd, apply_destripe=apply_destripe, jpeg=jpeg
    )


@app.command()
def package(
    source_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    destination: Annotated[Path, typer.Option("--out", help="Master GeoTIFF to write.")],
    allow_alpha_from_nodata: Annotated[bool, typer.Option("--allow-alpha-from-nodata")] = False,
    verify_pixels: Annotated[bool, typer.Option("--verify-pixels")] = False,
    declare_crs: Annotated[str | None, typer.Option("--declare-crs")] = None,
) -> None:
    """Package one raster to the master contract, without a run manifest."""
    plan = PackagingPlan(
        allow_alpha_from_nodata=allow_alpha_from_nodata,
        verify_pixels=verify_pixels,
        declare_crs=declare_crs,
    )
    try:
        result = package_master(source_path, destination, plan=plan)
    except PackagingError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    console().print(f"alpha:  {result.record.alpha_provenance.value}")
    for operation in result.record.operations:
        console().print(f"  {operation.name}: {operation.detail}")
    console().print(f"master: {result.master_path}")


@app.command()
def qa(target: Annotated[Path, typer.Argument(exists=True)]) -> None:
    """Run master raster QA on a packaged orthophoto."""
    if target.is_dir():
        error_console().print(
            "block-directory QA arrives with block scanning in phase 3; "
            "pass a packaged master raster for now"
        )
        raise typer.Exit(EXIT_FAIL)

    result = run_raster_qa(target)
    _render_qa(result)
    raise typer.Exit(_EXIT_FOR_STATUS[result.status])


def _block_summary(outcome: IngestOutcome) -> BlockRunSummary:
    manifest = outcome.manifest
    return BlockRunSummary(
        block_id=outcome.block_id,
        gate_status=outcome.gate_status,
        reused=outcome.reused,
        pixel_size_x=manifest.pixel_size_x if manifest else None,
        source_bytes=outcome.source_bytes,
        master_bytes=outcome.master_bytes,
        seconds=round(outcome.seconds, 1),
        manifest_path=str(outcome.manifest_path),
        master_path=str(outcome.master_path) if outcome.master_path else None,
        error=outcome.error,
    )


def _render_block_line(index: int, total: int, outcome: IngestOutcome) -> None:
    prefix = f"[dim]{index:>3}/{total}[/dim] {outcome.block_id:<6}"

    if outcome.error is not None:
        console().print(f"{prefix} [red]{'FAIL':<6}[/red] {outcome.error[:88]}")
        return

    colour = _STATUS_COLOUR.get(outcome.gate_status, "white")
    status = f"[{colour}]{outcome.gate_status.value:<6}[/{colour}]"

    manifest = outcome.manifest
    pixel = f"{manifest.pixel_size_x * 100:.2f} cm" if manifest and manifest.pixel_size_x else "-"
    sizes = f"{outcome.source_bytes / _GIB:.2f} -> {outcome.master_bytes / _GIB:.2f} GiB"
    note = "[dim](reused)[/dim]" if outcome.reused else f"{outcome.seconds:.0f}s"
    console().print(f"{prefix} {status} {pixel:>8}  {sizes:>24}  {note}")


def _render_project_summary(summary: ProjectRunSummary, summary_path: Path) -> None:
    failed = summary.failed
    elapsed = (summary.finished_at - summary.started_at).total_seconds()

    table = Table(title=f"{summary.project_id}: {len(summary.blocks)} blocks")
    table.add_column("outcome")
    table.add_column("blocks", justify="right")
    for status, count in (
        (GateStatus.PASS, summary.passed),
        (GateStatus.REVIEW, summary.review),
        (GateStatus.FAIL, failed),
    ):
        colour = _STATUS_COLOUR[status]
        table.add_row(f"[{colour}]{status.value}[/{colour}]", str(count))
    console().print(table)

    console().print(
        f"{summary.source_bytes / _GIB:.1f} GiB in -> {summary.master_bytes / _GIB:.1f} GiB out"
        f"  |  {elapsed / 60:.0f} min"
    )
    if failed:
        console().print(
            "[red]failed blocks:[/red] "
            + ", ".join(block.block_id for block in summary.blocks if block.failed)
        )
    console().print(f"summary: {summary_path}")


def _render_harmonisation(
    solution: HarmonisationSolution, solution_path: Path, csv_path: Path
) -> None:
    table = Table(
        title=(
            f"{solution.project_id}: {solution.constraint_count} constraints, "
            f"{solution.block_count} blocks"
        )
    )
    offset_model = solution.model == "gain_offset"
    table.add_column("band")
    columns = ["before", "gain only"]
    if offset_model:
        columns.append("gain+offset")
    columns += ["90th before", "90th after", "gain range"]
    if offset_model:
        columns.append("offset range")
    for column in columns:
        table.add_column(column, justify="right")

    for residual in solution.residuals:
        after = (
            residual.median_after_offset_pct
            if offset_model and residual.median_after_offset_pct is not None
            else residual.median_after_pct
        )
        p90_after = (
            residual.p90_after_offset_pct
            if offset_model and residual.p90_after_offset_pct is not None
            else residual.p90_after_pct
        )
        row = [
            residual.band,
            f"{residual.median_before_pct:.1f} %",
            f"{residual.median_after_pct:.1f} %",
        ]
        if offset_model:
            row.append(f"[green]{after:.1f} %[/green]")
        row += [
            f"{residual.p90_before_pct:.1f} %",
            f"{p90_after:.1f} %",
            f"{residual.gain_min:.2f} - {residual.gain_max:.2f}",
        ]
        if offset_model:
            lo = residual.offset_min if residual.offset_min is not None else 0.0
            hi = residual.offset_max if residual.offset_max is not None else 0.0
            row.append(f"{lo:+.1f} to {hi:+.1f} DN")
        table.add_row(*row)
    console().print(table)

    if not solution.is_single_component:
        error_console().print(
            f"[yellow]{solution.component_count} separate groups of blocks[/yellow]: gains are "
            "only comparable within a group, because blocks sharing no chain of overlaps have "
            "no measured relationship"
        )

    stubborn = sorted(
        (b for b in solution.blocks if b.residual_pct is not None),
        key=lambda b: -(b.residual_pct or 0.0),
    )[:5]
    if stubborn:
        console().print("blocks a single gain cannot fix (highest residual):")
        for block in stubborn:
            console().print(
                f"  {block.block_id:<5} residual {block.residual_pct:5.1f} %"
                f"   ({block.overlap_count} overlaps)"
            )

    console().print(f"anchor: {solution.anchor}   weighting: {solution.weighting}")
    console().print(f"solution: {solution_path}")
    console().print(f"gains:    {csv_path}")


def _render_radiometry(report: RadiometricOverlapReport, path: Path) -> None:
    measured = report.measured
    if not measured:
        error_console().print("no pair had ground valid in both blocks")
        return

    values = sorted(p.max_abs_relative_difference_pct for p in measured)

    def percentile(fraction: float) -> float:
        return values[min(len(values) - 1, int(fraction * (len(values) - 1)))]

    table = Table(title=f"{report.project_id}: worst-band disagreement over {len(measured)} pairs")
    table.add_column("statistic")
    table.add_column("difference", justify="right")
    for label, value in (
        ("best pair", values[0]),
        ("25th percentile", percentile(0.25)),
        ("median", percentile(0.50)),
        ("75th percentile", percentile(0.75)),
        ("worst pair", values[-1]),
    ):
        table.add_row(label, f"{value:.1f} %")
    console().print(table)

    worst = sorted(measured, key=lambda p: -p.max_abs_relative_difference_pct)[:10]
    detail = Table(title="Most divergent pairs")
    detail.add_column("pair")
    detail.add_column("overlap", justify="right")
    for name in ("red", "green", "blue"):
        detail.add_column(name, justify="right")
    for pair in worst:
        detail.add_row(
            f"{pair.block_a}/{pair.block_b}",
            f"{pair.overlap_area_ha:.0f} ha",
            *(f"{b.relative_difference_pct:+.1f}%" for b in pair.bands),
        )
    console().print(detail)

    console().print(
        f"{report.measured_count} of {report.pair_count} overlapping pairs measured. "
        "No pair is passed or failed: thresholds are not established yet."
    )
    console().print(f"report: {path}")


def _render_qa(result: RasterQAResult) -> None:
    colour = _STATUS_COLOUR.get(result.status, "white")

    table = Table(title=f"Master raster QA: [{colour}]{result.status.value}[/{colour}]")
    table.add_column("check")
    table.add_column("outcome")
    table.add_column("observed")
    for check in result.details:
        outcome_colour = {
            CheckOutcome.PASS: "green",
            CheckOutcome.REVIEW: "yellow",
            CheckOutcome.FAIL: "red",
        }.get(check.outcome, "white")
        table.add_row(
            check.name,
            f"[{outcome_colour}]{check.outcome.value}[/{outcome_colour}]",
            str(check.observed),
        )
    console().print(table)

    for check in result.failures + result.reviews:
        console().print(f"  [{colour}]{check.name}[/{colour}]: {check.clause} — {check.message}")
