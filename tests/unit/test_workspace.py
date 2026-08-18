from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from drone_photogrammetry_pipeline.workspace import (
    SourceProtectionError,
    Workspace,
    make_run_id,
)


def test_run_id_carries_the_block_and_a_utc_timestamp() -> None:
    moment = datetime(2026, 8, 18, 9, 30, 0, tzinfo=UTC)
    run_id = make_run_id("B064", now=moment)

    assert run_id.startswith("B064_20260818T093000Z_")


def test_run_ids_are_unique_for_the_same_instant() -> None:
    moment = datetime(2026, 8, 18, 9, 30, 0, tzinfo=UTC)
    assert make_run_id("B064", now=moment) != make_run_id("B064", now=moment)


def test_run_paths_are_all_inside_the_run_directory(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace")
    paths = workspace.run_paths("Buduunkhad", "B064", "B064_run")
    paths.create()

    for path in (
        paths.manifest,
        paths.inputs_dir,
        paths.engine_dir,
        paths.master_dir,
        paths.qa_dir,
        paths.logs_dir,
        paths.pipeline_log,
    ):
        assert path.is_relative_to(paths.root)

    assert paths.root.is_relative_to(workspace.root)


def test_workspace_inside_a_source_block_is_refused(tmp_path: Path) -> None:
    block = tmp_path / "B064"
    block.mkdir()
    workspace = Workspace(block / "workspace")

    with pytest.raises(SourceProtectionError, match="must never be written into a source tree"):
        workspace.guard_source(block)


def test_workspace_beside_the_source_is_allowed(tmp_path: Path) -> None:
    block = tmp_path / "B064"
    block.mkdir()
    workspace = Workspace(tmp_path / "workspace")

    workspace.guard_source(block)


def test_guard_protects_the_folder_a_source_file_lives_in(tmp_path: Path) -> None:
    """A single delivered file still implies a delivery folder that stays clean."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    dom = incoming / "BLK001_DOM.tif"
    dom.write_bytes(b"")
    workspace = Workspace(incoming / "runs")

    with pytest.raises(SourceProtectionError):
        workspace.guard_source(dom)
