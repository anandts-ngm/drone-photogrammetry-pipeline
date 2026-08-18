"""Command line interface.

Everything printed here is presentation. The evidence for a run is the manifest, the QA
result and the JSONL processing log written under the workspace, never this output.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from . import __version__
from .config import get_settings
from .harmonisation import HarmonisationError, solve_gains
from .log import console, error_console
from .models.enums import CheckOutcome, GateStatus, HeightType, SensorId, SourceType
from .models.harmonisation import HarmonisationSolution
from .models.manifest import BlockRunSummary, ProjectRunSummary
from .models.qa import RadiometricOverlapReport, RadiometricPairResult, RasterQAResult
from .orchestration import (
    IngestOutcome,
    IngestRequest,
    discover_blocks,
    ingest_external_to_master,
)
from .packaging.gdal_backend import PackagingError, PackagingPlan
from .packaging.raster import package_master
from .qa.radiometry import DEFAULT_PATCH_METRES, DEFAULT_PATCHES, measure_project
from .qa.raster import run_raster_qa
from .reporting.manifest import (
    latest_radiometry_report,
    read_radiometry_report,
    write_harmonisation,
    write_harmonisation_gains_csv,
    write_project_summary,
    write_radiometry_report,
)
from .workspace import Workspace

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


class ExternalSource(StrEnum):
    TERRA = "terra"


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

    console().print(f"[bold]{project_id}[/bold]: {len(blocks)} blocks from {root}")
    started_at = datetime.now(UTC)
    outcomes: list[IngestOutcome] = []

    for index, (block_id, source_path) in enumerate(blocks, start=1):
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
            workspace=workspace,
            reuse_completed=not force,
        )
        outcomes.append(outcome)
        _render_block_line(index, len(blocks), outcome)

    finished_at = datetime.now(UTC)
    summary = ProjectRunSummary(
        project_id=project_id,
        source_root=str(root),
        started_at=started_at,
        finished_at=finished_at,
        blocks=[_block_summary(outcome) for outcome in outcomes],
    )
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    summary_path = write_project_summary(
        workspace.project_dir(project_id) / f"run-project_{stamp}.json", summary
    )

    _render_project_summary(summary, summary_path)

    if summary.failed:
        raise typer.Exit(EXIT_FAIL)
    if summary.review:
        raise typer.Exit(EXIT_REVIEW)
    raise typer.Exit(EXIT_PASS)


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
        progress=show,
    )
    stamp = report.generated_at.strftime("%Y%m%dT%H%M%SZ")
    path = write_radiometry_report(
        workspace.project_dir(project_id) / f"radiometry_{stamp}.json", report
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
) -> None:
    """Solve one radiometric gain per block per band from measured overlaps.

    Writes the coefficients and the before/after residuals. It does not modify any master:
    applying the gains produces a separate derived product.
    """
    settings = get_settings()
    workspace = Workspace(settings.workspace_root)
    directory = workspace.project_dir(project_id)

    chosen = report_path or latest_radiometry_report(directory)
    if chosen is None or not chosen.is_file():
        error_console().print(
            f"no radiometry report for {project_id} in {directory}; "
            "run 'drone-photo radiometry' first"
        )
        raise typer.Exit(EXIT_FAIL)

    report = read_radiometry_report(chosen)
    try:
        solution = solve_gains(report, weight_by_samples=weight_by_samples)
    except HarmonisationError as error:
        error_console().print(f"[bold red]FAILED[/bold red] {error}")
        raise typer.Exit(EXIT_FAIL) from error

    stamp = solution.generated_at.strftime("%Y%m%dT%H%M%SZ")
    solution_path = write_harmonisation(directory / f"harmonisation_{stamp}.json", solution)
    csv_path = write_harmonisation_gains_csv(directory / "harmonisation_gains.csv", solution)
    _render_harmonisation(solution, solution_path, csv_path)


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
    table.add_column("band")
    for column in ("median before", "median after", "90th before", "90th after", "gain range"):
        table.add_column(column, justify="right")
    for residual in solution.residuals:
        table.add_row(
            residual.band,
            f"{residual.median_before_pct:.1f} %",
            f"[green]{residual.median_after_pct:.1f} %[/green]",
            f"{residual.p90_before_pct:.1f} %",
            f"{residual.p90_after_pct:.1f} %",
            f"{residual.gain_min:.2f} - {residual.gain_max:.2f}",
        )
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
