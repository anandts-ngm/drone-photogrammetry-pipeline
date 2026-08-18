"""Reading and writing run manifests, QA results and harmonisation solutions."""

from __future__ import annotations

import csv
from pathlib import Path

from ..models.harmonisation import HarmonisationSolution
from ..models.manifest import ProjectRunSummary, RunManifest
from ..models.qa import RadiometricOverlapReport, RasterQAResult


def write_manifest(path: Path, manifest: RunManifest) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return path


def read_manifest(path: Path) -> RunManifest:
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def write_project_summary(path: Path, summary: ProjectRunSummary) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
    return path


def write_radiometry_report(path: Path, report: RadiometricOverlapReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def read_radiometry_report(path: Path) -> RadiometricOverlapReport:
    return RadiometricOverlapReport.model_validate_json(path.read_text(encoding="utf-8"))


def latest_radiometry_report(directory: Path) -> Path | None:
    reports = sorted(directory.glob("radiometry_*.json"))
    return reports[-1] if reports else None


def write_harmonisation(path: Path, solution: HarmonisationSolution) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(solution.model_dump_json(indent=2), encoding="utf-8")
    return path


def write_harmonisation_gains_csv(path: Path, solution: HarmonisationSolution) -> Path:
    """A flat table for consumers that apply the gains themselves.

    The overlap count and residual travel with each row on purpose: a coefficient solved from
    three overlaps deserves less trust than one solved from eight, and a reader who only sees
    the number cannot know that.
    """
    bands = list(solution.blocks[0].gains) if solution.blocks else []
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "project",
                "block",
                *(f"gain_{band}" for band in bands),
                "overlap_count",
                "component",
                "residual_pct",
                "anchor",
                "weighting",
            ]
        )
        for block in solution.blocks:
            writer.writerow(
                [solution.project_id, block.block_id]
                + [f"{block.gains[band]:.5f}" for band in bands]
                + [
                    block.overlap_count,
                    block.component,
                    "" if block.residual_pct is None else f"{block.residual_pct:.2f}",
                    solution.anchor,
                    solution.weighting,
                ]
            )
    return path


def write_qa_result(path: Path, result: RasterQAResult) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path
