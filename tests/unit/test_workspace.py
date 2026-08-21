from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from drone_photogrammetry_pipeline.workspace import (
    SourceProtectionError,
    Workspace,
    WorkspaceLocationError,
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


def test_a_workspace_inside_the_checkout_is_refused(tmp_path: Path) -> None:
    """The default root is a relative `workspace`, so a clone without a .env writes here.

    On this survey that is 108 GB of masters inside a git working tree. Refusing costs one
    error message; not refusing costs a repository nobody can commit from.
    """
    (tmp_path / ".git").mkdir()

    with pytest.raises(WorkspaceLocationError, match="DPP_WORKSPACE_ROOT"):
        Workspace(tmp_path / "workspace")


def test_the_check_looks_at_every_ancestor_not_just_the_parent(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    with pytest.raises(WorkspaceLocationError):
        Workspace(tmp_path / "outputs" / "photogrammetry" / "runs")


def test_a_workspace_outside_any_repository_is_accepted(tmp_path: Path) -> None:
    assert Workspace(tmp_path / "outputs").root == (tmp_path / "outputs").resolve()


def test_the_repository_check_can_be_waived_for_a_test_fixture(tmp_path: Path) -> None:
    """Kept explicit: a caller that wants this has to say so at the call site."""
    (tmp_path / ".git").mkdir()

    assert Workspace(tmp_path / "workspace", allow_repository=True).root.name == "workspace"


def test_find_masters_returns_the_newest_run_of_each_block(tmp_path: Path) -> None:
    """A mosaic holding two versions of the same ground is not a mosaic."""
    workspace = Workspace(tmp_path / "outputs")
    for block, runs in (("B1", ("B1_20260101T000000Z", "B1_20260819T000000Z")), ("B10", ("r1",))):
        for run in runs:
            paths = workspace.run_paths("Buduunkhad", block, run)
            paths.create()
            (paths.master_dir / f"{block}_ORTHO_MASTER.tif").write_bytes(b"")

    found = workspace.find_masters("Buduunkhad")

    assert [block for block, _ in found] == ["B1", "B10"], "B10 sorts after B1, not before"
    assert found[0][1].parent.parent.name == "B1_20260819T000000Z"


def test_guard_protects_the_folder_a_source_file_lives_in(tmp_path: Path) -> None:
    """A single delivered file still implies a delivery folder that stays clean."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    dom = incoming / "BLK001_DOM.tif"
    dom.write_bytes(b"")
    workspace = Workspace(incoming / "runs")

    with pytest.raises(SourceProtectionError):
        workspace.guard_source(dom)
