"""Workspace layout and the source-protection guard.

Every path that the pipeline writes to is resolved here. That is deliberate: "never write
into a source block" is an invariant, and an invariant enforced in one module can be
tested, whereas one enforced by every call site remembering it cannot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_TRAILING_NUMBER = re.compile(r"(\d+)$")


class SourceProtectionError(RuntimeError):
    """Raised when an operation would write generated data into a source location."""


def natural_key(name: str) -> tuple[str, int]:
    """Sort B9 before B10, which a plain string sort would not.

    Lives here rather than in orchestration because the workspace enumerates blocks too, and
    two block orderings that disagree would be worse than one in an awkward place.
    """
    match = _TRAILING_NUMBER.search(name)
    if match is None:
        return (name, 0)
    return (name[: match.start()], int(match.group(1)))


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


class WorkspaceLocationError(RuntimeError):
    """Raised when the workspace root is somewhere products must not be written."""


def _enclosing_repository(path: Path) -> Path | None:
    """The nearest ancestor holding a `.git`, or None."""
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


class Workspace:
    def __init__(self, root: Path, *, allow_repository: bool = False) -> None:
        self.root = root.expanduser().resolve()

        # `DPP_WORKSPACE_ROOT` defaults to a relative `workspace`, so a fresh clone that skips
        # copying .env writes every product inside the checkout. On this survey that is 108 GB
        # of masters in a git working tree, discovered late and awkwardly. Refuse instead.
        if not allow_repository:
            repository = _enclosing_repository(self.root)
            if repository is not None:
                raise WorkspaceLocationError(
                    f"workspace root {self.root} is inside the git repository at {repository}. "
                    "Products belong outside the checkout: copy .env.example to .env and set "
                    "DPP_WORKSPACE_ROOT to a path on a data volume"
                )

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

    def derived_dir(self, project_id: str) -> Path:
        """Products built from finished masters: mosaics, previews.

        Kept in its own directory because these are lossy or resampled by design. A master and
        a preview of a master must never be confusable by location.
        """
        return self.project_dir(project_id) / "derived"

    def find_masters(self, project_id: str) -> list[tuple[str, Path]]:
        """The current master for every block in a project, newest run per block.

        Newest rather than all, because a block reprocessed after a source change has more
        than one, and a mosaic containing two versions of the same ground is not a mosaic.
        """
        found: list[tuple[str, Path]] = []
        for block_dir in sorted(self.project_dir(project_id).glob("*/runs")):
            block_id = block_dir.parent.name
            masters = sorted(block_dir.glob("*/master/*_ORTHO_MASTER.tif"))
            if masters:
                found.append((block_id, masters[-1]))
        return sorted(found, key=lambda item: natural_key(item[0]))

    def block_inputs_dir(self, project_id: str, block_id: str) -> Path:
        """Generated inputs for a block that are not tied to one run.

        A `geo.txt` derived from a flight's mark file belongs here rather than beside the
        imagery: it is generated data, and generated data never lands in a source tree. It sits
        outside `runs/` because it is an input to every run of that block, not a product of one.
        """
        return self.root / self.project_slug(project_id) / block_id / "inputs"

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
