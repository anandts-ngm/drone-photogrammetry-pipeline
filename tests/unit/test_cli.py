"""End-to-end tests for the milestone-1 raster deliverable.

These exercise the whole external-ingest path with no ODM involved: ingest, package to the
master contract, QA, checksum, manifest.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from drone_photogrammetry_pipeline.cli import EXIT_FAIL, EXIT_PASS, EXIT_REVIEW, app
from drone_photogrammetry_pipeline.config import get_settings
from drone_photogrammetry_pipeline.integrity import sha256_file
from drone_photogrammetry_pipeline.models.enums import (
    GateStatus,
    ProcessingEngine,
    SourceType,
    WorkflowStatus,
)
from drone_photogrammetry_pipeline.reporting.manifest import read_manifest
from drone_photogrammetry_pipeline.workspace import SourceProtectionError
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


def incoming_dom(tmp_path: Path, builder: str = "rgba_source") -> Path:
    incoming = tmp_path / "incoming"
    incoming.mkdir(exist_ok=True)
    build = getattr(make_rasters, builder)
    return Path(build(incoming / "BLK001_DOM.tif"))


def only_manifest(tmp_path: Path) -> Path:
    manifests = list((tmp_path / "workspace").rglob("manifest.json"))
    assert len(manifests) == 1, manifests
    return manifests[0]


def ingest(source: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "ingest-ortho",
            str(source),
            "--source",
            "terra",
            "--project-id",
            "Sant",
            "--block-id",
            "N003",
            *extra,
        ],
    )


def test_terra_ingest_produces_a_passing_master_with_a_full_manifest(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path)

    result = runner.invoke(
        app,
        [
            "ingest-ortho",
            str(source),
            "--source",
            "terra",
            "--project-id",
            "Sant",
            "--block-id",
            "N003",
            "--verify-pixels",
        ],
    )
    assert result.exit_code == EXIT_PASS, result.output

    manifest = read_manifest(only_manifest(tmp_path))
    assert manifest.gate_status is GateStatus.PASS
    assert manifest.processing_status is WorkflowStatus.QA_COMPLETE
    assert manifest.source_type is SourceType.DJI_TERRA
    assert manifest.processing_engine is ProcessingEngine.DJI_TERRA
    assert manifest.profile_id == "external_terra"
    assert manifest.profile_hash
    assert manifest.crs == "EPSG:32648"
    assert manifest.pixel_size_x == pytest.approx(make_rasters.PIXEL_SIZE)
    assert manifest.raster_qa is not None
    assert manifest.packaging is not None
    assert manifest.packaging.grid_preserved is True
    assert manifest.packaging.pixels_verified is True
    assert manifest.started_at is not None
    assert manifest.finished_at is not None


def test_the_master_and_its_checksum_are_recorded(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path)
    assert ingest(source).exit_code == EXIT_PASS

    manifest = read_manifest(only_manifest(tmp_path))
    master = Path(manifest.outputs["orthophoto_master"])

    assert master.is_file()
    assert master.name == "N003_ORTHO_MASTER.tif"
    assert manifest.output_hashes["orthophoto_master"] == sha256_file(master)


def test_the_qa_result_is_written_as_its_own_document(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path)
    assert ingest(source).exit_code == EXIT_PASS

    qa_documents = list((tmp_path / "workspace").rglob("raster_qa.json"))
    assert [p.name for p in qa_documents] == ["raster_qa.json"]


def test_the_processing_log_is_machine_readable(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path)
    assert ingest(source).exit_code == EXIT_PASS

    logs = list((tmp_path / "workspace").rglob("pipeline.jsonl"))
    assert len(logs) == 1
    lines = [line for line in logs[0].read_text(encoding="utf-8").splitlines() if line]
    assert lines
    import json

    for line in lines:
        record = json.loads(line)
        assert {"ts", "level", "logger", "message"} <= set(record)


def test_the_source_file_is_left_untouched(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path)
    before = sha256_file(source)
    listing_before = sorted(p.name for p in source.parent.iterdir())

    assert ingest(source).exit_code == EXIT_PASS

    assert sha256_file(source) == before
    assert sorted(p.name for p in source.parent.iterdir()) == listing_before


def test_ambiguous_nodata_fails_and_still_writes_a_manifest(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path, "nodata_rgb_source")

    result = ingest(source)
    assert result.exit_code == EXIT_FAIL

    manifest = read_manifest(only_manifest(tmp_path))
    assert manifest.processing_status is WorkflowStatus.FAILED
    assert manifest.gate_status is GateStatus.NOT_EVALUATED
    assert manifest.outputs == {}


def test_ambiguous_nodata_with_opt_in_is_review_not_pass(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path, "nodata_rgb_source")

    result = ingest(source, "--allow-alpha-from-nodata")
    assert result.exit_code == EXIT_REVIEW

    manifest = read_manifest(only_manifest(tmp_path))
    assert manifest.gate_status is GateStatus.REVIEW


def test_a_source_without_a_validity_mask_is_refused(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path, "bare_rgb_source")

    result = ingest(source)

    assert result.exit_code == EXIT_FAIL
    assert read_manifest(only_manifest(tmp_path)).processing_status is WorkflowStatus.FAILED


def test_writing_into_the_source_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = incoming_dom(tmp_path)
    monkeypatch.setenv("DPP_WORKSPACE_ROOT", str(source.parent / "workspace"))
    get_settings.cache_clear()

    result = ingest(source)

    assert isinstance(result.exception, SourceProtectionError)


def test_qa_command_passes_a_packaged_master(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path)
    assert ingest(source).exit_code == EXIT_PASS
    master = Path(read_manifest(only_manifest(tmp_path)).outputs["orthophoto_master"])

    assert runner.invoke(app, ["qa", str(master)]).exit_code == EXIT_PASS


def test_qa_command_rejects_an_unpackaged_raster(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path, "masked_rgb_source")

    result = runner.invoke(app, ["qa", str(source)])

    assert result.exit_code == EXIT_FAIL


def test_package_command_writes_a_master_without_a_manifest(tmp_path: Path) -> None:
    source = incoming_dom(tmp_path)
    destination = tmp_path / "out" / "master.tif"

    result = runner.invoke(app, ["package", str(source), "--out", str(destination)])

    assert result.exit_code == EXIT_PASS, result.output
    assert destination.is_file()
    assert runner.invoke(app, ["qa", str(destination)]).exit_code == EXIT_PASS
