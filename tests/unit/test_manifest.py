from __future__ import annotations

import json
from pathlib import Path

from drone_photogrammetry_pipeline.models.enums import GateStatus, WorkflowStatus
from drone_photogrammetry_pipeline.models.manifest import MANIFEST_SCHEMA_VERSION, RunManifest
from drone_photogrammetry_pipeline.reporting.manifest import read_manifest, write_manifest

# The fields named in the kickoff specification. The delivered manifest carries more, but it
# must never carry fewer.
REQUIRED_FIELDS = {
    "project_id",
    "block_id",
    "sensor",
    "lens",
    "source_type",
    "processing_engine",
    "profile_id",
    "profile_version",
    "profile_hash",
    "image_count",
    "input_manifest_hash",
    "nodeodm_task_id",
    "nodeodm_version",
    "odm_version",
    "started_at",
    "finished_at",
    "processing_status",
    "crs",
    "height_type",
    "pixel_size_x",
    "pixel_size_y",
    "raster_qa",
    "checkpoint_rmse_xy",
    "checkpoint_rmse_z",
    "lidar_median_dz",
    "radiometric_overlap_score",
    "outputs",
    "output_hashes",
    "gate_status",
}


def test_manifest_carries_every_specified_field() -> None:
    document = json.loads(RunManifest(run_id="B064_run").model_dump_json())
    assert set(document) >= REQUIRED_FIELDS


def test_a_new_manifest_claims_nothing() -> None:
    """Absence of evidence is recorded as absence, never as a plausible default."""
    manifest = RunManifest(run_id="B064_run")

    assert manifest.gate_status is GateStatus.NOT_EVALUATED
    assert manifest.processing_status is WorkflowStatus.VALIDATION_PENDING
    assert manifest.pixel_size_x is None
    assert manifest.pixel_size_y is None
    assert manifest.raster_qa is None
    assert manifest.checkpoint_rmse_xy is None
    assert manifest.lidar_median_dz is None
    assert manifest.outputs == {}
    assert manifest.schema_version == MANIFEST_SCHEMA_VERSION


def test_manifest_round_trips_through_disk(tmp_path: Path) -> None:
    manifest = RunManifest(
        run_id="B064_20260818T093000Z_abcdef12",
        project_id="Buduunkhad",
        block_id="B064",
        profile_id="p1_35_master",
        profile_version=1,
        profile_hash="0" * 64,
        pixel_size_x=0.017,
        pixel_size_y=0.017,
    )
    path = write_manifest(tmp_path / "manifest.json", manifest)

    assert read_manifest(path) == manifest


def test_native_pixel_size_survives_serialisation_unrounded(tmp_path: Path) -> None:
    manifest = RunManifest(run_id="run", pixel_size_x=0.0174, pixel_size_y=0.0246)
    restored = read_manifest(write_manifest(tmp_path / "manifest.json", manifest))

    assert restored.pixel_size_x == 0.0174
    assert restored.pixel_size_y == 0.0246
