"""Project-level batch tests.

The resume behaviour matters more than it looks: a 79-block project run takes hours, so an
interrupted run that silently reprocessed everything, or silently skipped a re-delivered
file, would both be expensive mistakes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from drone_photogrammetry_pipeline.cli import EXIT_FAIL, EXIT_PASS, app
from drone_photogrammetry_pipeline.config import get_settings
from drone_photogrammetry_pipeline.orchestration import discover_blocks, natural_key
from tests.fixtures import make_rasters

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DPP_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("DPP_PROFILES_DIR", str(REPO_ROOT / "profiles"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def build_project(tmp_path: Path, blocks: tuple[str, ...] = ("B1", "B2", "B10")) -> Path:
    root = tmp_path / "delivery"
    for block_id in blocks:
        (root / block_id).mkdir(parents=True)
        make_rasters.terra_dom_source(root / block_id / "dom.tif")
    return root


def run(root: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        ["run-project", str(root), "--source", "terra", "--project-id", "Buduunkhad", *extra],
    )


def read_summary(tmp_path: Path) -> dict[str, Any]:
    summaries = list((tmp_path / "workspace").rglob("run_project_*.json"))
    assert summaries, "no project summary was written"
    document: dict[str, Any] = json.loads(sorted(summaries)[-1].read_text(encoding="utf-8"))
    return document


def read_blocks(tmp_path: Path) -> list[dict[str, Any]]:
    blocks = read_summary(tmp_path)["blocks"]
    assert isinstance(blocks, list)
    return blocks


def test_blocks_are_ordered_numerically_not_lexically() -> None:
    assert natural_key("B9") < natural_key("B10")
    assert sorted(["B10", "B9", "B1"], key=natural_key) == ["B1", "B9", "B10"]


def test_discovery_finds_only_directories_holding_the_asset(tmp_path: Path) -> None:
    root = build_project(tmp_path)
    (root / "notes").mkdir()

    found = discover_blocks(root, "dom.tif")

    assert [block_id for block_id, _ in found] == ["B1", "B2", "B10"]


def test_every_block_is_packaged_and_summarised(tmp_path: Path) -> None:
    root = build_project(tmp_path)

    result = run(root)
    assert result.exit_code == EXIT_PASS, result.output

    assert read_summary(tmp_path)["project_id"] == "Buduunkhad"
    blocks = read_blocks(tmp_path)
    assert [b["block_id"] for b in blocks] == ["B1", "B2", "B10"]
    assert all(b["gate_status"] == "PASS" for b in blocks)
    assert all(Path(b["master_path"]).is_file() for b in blocks)
    assert all(b["master_bytes"] > 0 for b in blocks)


def test_each_block_gets_its_own_manifest(tmp_path: Path) -> None:
    root = build_project(tmp_path)
    assert run(root).exit_code == EXIT_PASS

    manifests = list((tmp_path / "workspace").rglob("manifest.json"))
    assert len(manifests) == 3


def test_a_second_run_reuses_completed_blocks(tmp_path: Path) -> None:
    root = build_project(tmp_path)
    assert run(root).exit_code == EXIT_PASS

    assert run(root).exit_code == EXIT_PASS

    assert all(b["reused"] for b in read_blocks(tmp_path))
    # Reuse must not create a second run directory for the same source.
    assert len(list((tmp_path / "workspace").rglob("manifest.json"))) == 3


def test_force_reprocesses_completed_blocks(tmp_path: Path) -> None:
    root = build_project(tmp_path)
    assert run(root).exit_code == EXIT_PASS

    assert run(root, "--force").exit_code == EXIT_PASS

    assert not any(b["reused"] for b in read_blocks(tmp_path))
    assert len(list((tmp_path / "workspace").rglob("manifest.json"))) == 6


def test_a_changed_source_is_reprocessed_rather_than_reused(tmp_path: Path) -> None:
    """Reuse is keyed on the source checksum, so a re-delivered file is new work."""
    root = build_project(tmp_path, blocks=("B1",))
    assert run(root).exit_code == EXIT_PASS

    make_rasters.terra_dom_source(root / "B1" / "dom.tif", data=make_rasters.gradient(4) // 2)
    assert run(root).exit_code == EXIT_PASS

    assert not any(b["reused"] for b in read_blocks(tmp_path))


def test_limit_processes_only_the_first_blocks(tmp_path: Path) -> None:
    root = build_project(tmp_path)

    assert run(root, "--limit", "2").exit_code == EXIT_PASS

    assert [b["block_id"] for b in read_blocks(tmp_path)] == ["B1", "B2"]


def test_one_bad_block_fails_the_project_without_stopping_the_others(tmp_path: Path) -> None:
    root = build_project(tmp_path, blocks=("B1", "B2"))
    make_rasters.bare_rgb_source(root / "B2" / "dom.tif")

    result = run(root)

    assert result.exit_code == EXIT_FAIL
    blocks = read_blocks(tmp_path)
    good = next(b for b in blocks if b["block_id"] == "B1")
    bad = next(b for b in blocks if b["block_id"] == "B2")

    assert good["gate_status"] == "PASS"
    assert Path(good["master_path"]).is_file()

    # The bad block stopped before QA, so its gate honestly stays NOT_EVALUATED and the
    # error carries the reason. Failure is read from the error, not from the gate.
    assert bad["gate_status"] == "NOT_EVALUATED"
    assert "validity mask" in bad["error"]
    assert bad["master_path"] is None


def test_a_directory_with_no_matching_asset_is_reported(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    result = run(empty)

    assert result.exit_code == EXIT_FAIL
    assert "dom.tif" in result.output


def test_declared_crs_and_height_type_reach_every_manifest(tmp_path: Path) -> None:
    root = build_project(tmp_path, blocks=("B1", "B2"))

    result = run(root, "--declare-crs", "EPSG:32647+5705", "--height-type", "NORMAL")
    assert result.exit_code == EXIT_PASS, result.output

    for manifest_path in (tmp_path / "workspace").rglob("manifest.json"):
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert document["crs"] == "EPSG:32647+5705"
        assert document["height_type"] == "NORMAL"
