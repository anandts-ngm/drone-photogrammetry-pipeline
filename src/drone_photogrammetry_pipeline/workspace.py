"""Workspace layout and the source-protection guard.

Every path that the pipeline writes to is resolved here. That is deliberate: "never write
into a source block" is an invariant, and an invariant enforced in one module can be
tested, whereas one enforced by every call site remembering it cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class SourceProtectionError(RuntimeError):
    """Raised when an operation would write generated data into a source location."""


def make_run_id(block_id: str, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return f"{block_id}_{moment:%Y%m%dT%H%M%SZ}_{uuid4().hex[:8]}"


@dataclass(frozen=True)
class RunPaths:
    """Absolute locations for one run. Nothing outside this tree is written."""

    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def inputs_dir(self) -> Path:
        return self.root / "inputs"

    @property
    def engine_dir(self) -> Path:
        return self.root / "engine"

    @property
    def master_dir(self) -> Path:
        return self.root / "master"

    @property
    def qa_dir(self) -> Path:
        return self.root / "qa"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def pipeline_log(self) -> Path:
        return self.logs_dir / "pipeline.jsonl"

    @property
    def engine_console_log(self) -> Path:
        return self.logs_dir / "engine_console.log"

    def create(self) -> None:
        for directory in (
            self.root,
            self.inputs_dir,
            self.engine_dir,
            self.master_dir,
            self.qa_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def run_paths(self, project_id: str, block_id: str, run_id: str) -> RunPaths:
        return RunPaths(self.root / self.project_slug(project_id) / block_id / "runs" / run_id)

    @staticmethod
    def project_slug(project_id: str) -> str:
        """Directory name for a project.

        Lower case with underscores, so that a project written `Buduunkhad` on one command
        line and `buduunkhad` on the next lands in one place rather than two. On a
        case-insensitive filesystem the difference is invisible until the tree is copied to a
        case-sensitive one, at which point it silently becomes two half-populated projects.
        """
        return "_".join(project_id.split()).lower()

    def project_dir(self, project_id: str) -> Path:
        return self.root / self.project_slug(project_id)

    def reports_dir(self, project_id: str, kind: str) -> Path:
        """Where a project-level report of a given kind lives.

        Grouped by kind because these accumulate: a project that has been measured and solved
        a few times ends up with dozens of siblings, and a flat directory gives no clue which
        radiometry report a given harmonisation came from.
        """
        return self.project_dir(project_id) / "reports" / kind

    def promoted_master_dir(self, project_id: str, block_id: str) -> Path:
        return self.root / self.project_slug(project_id) / block_id / "master"

    def guard_source(self, source: Path) -> None:
        """Refuse to run if generated data would land inside the source tree.

        Checked against the source's parent directory as well, because a caller may hand
        over a single file (an external orthophoto) rather than a block directory.
        """
        resolved = source.expanduser().resolve()
        source_root = resolved if resolved.is_dir() else resolved.parent
        if self.root == source_root or self.root.is_relative_to(source_root):
            raise SourceProtectionError(
                f"workspace root {self.root} is inside the source location {source_root}; "
                "generated data must never be written into a source tree"
            )
