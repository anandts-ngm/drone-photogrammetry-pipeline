"""ODM adapter tests, with NodeODM mocked at the transport layer.

Two behaviours here were learned from a running engine rather than assumed, and both are
pinned by tests so they cannot quietly regress: ground control travels as an uploaded file
rather than an option, and only `all.zip` can be retrieved so `outputs` is the sole lever
over what it contains.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from drone_photogrammetry_pipeline.ingest.scan import scan_block
from drone_photogrammetry_pipeline.ingest.validate import validate_block
from drone_photogrammetry_pipeline.models.profile import ProcessingProfile
from drone_photogrammetry_pipeline.nodeodm.client import NodeODMClient
from drone_photogrammetry_pipeline.processing.odm import (
    DSM_ASSET,
    ORTHO_ASSET,
    POINT_CLOUD_ASSET,
    OdmProcessingError,
    extract_assets,
    odm_options,
    submit,
    wanted_outputs,
)
from tests.unit.test_block_scan import flat_flight
from tests.unit.test_nodeodm_client import Recorder

ENGINE_OPTIONS = [
    {"name": "dsm"},
    {"name": "dtm"},
    {"name": "crop"},
    {"name": "orthophoto-compression"},
    {"name": "orthophoto-cutline"},
    {"name": "build-overviews"},
    {"name": "texturing-skip-global-seam-leveling"},
    {"name": "radiometric-calibration"},
]


def profile(**overrides: Any) -> ProcessingProfile:
    base: dict[str, Any] = {
        "profile_id": "test_profile",
        "profile_version": 1,
        "processing": {"engine": "ODM", "odm": {"dsm": True}},
        "radiometry": {
            "policy": "analytical_master",
            "odm": {"texturing-skip-global-seam-leveling": True, "build-overviews": False},
        },
        "outputs": {"orthophoto": True, "dsm": True, "point_cloud": False},
    }
    base.update(overrides)
    return ProcessingProfile.model_validate(base)


def node(recorder: Recorder) -> NodeODMClient:
    return NodeODMClient("http://node:3000", transport=recorder.transport(), backoff_seconds=0.0)


def task_routes() -> dict[str, Any]:
    return {
        "/options": httpx.Response(200, json=ENGINE_OPTIONS),
        "/task/new/init": httpx.Response(200, json={"uuid": "task-1"}),
        "/task/new/upload": httpx.Response(200, json={"success": True}),
        "/task/new/commit": httpx.Response(200, json={"uuid": "task-1"}),
    }


def test_radiometry_options_win_over_processing_options() -> None:
    """A tuning value must never quietly override a radiometric decision."""
    merged = odm_options(
        profile(
            processing={"engine": "ODM", "odm": {"build-overviews": True, "dsm": True}},
            radiometry={"policy": "analytical_master", "odm": {"build-overviews": False}},
        )
    )

    assert merged["build-overviews"] is False
    assert merged["dsm"] is True


def test_requested_outputs_follow_the_profile() -> None:
    wanted = wanted_outputs(profile())

    assert ORTHO_ASSET in wanted
    assert DSM_ASSET in wanted
    assert POINT_CLOUD_ASSET not in wanted


def test_the_profile_can_choose_the_uncropped_orthophoto() -> None:
    wanted = wanted_outputs(
        profile(packaging={"source_asset": "odm_orthophoto/odm_orthophoto.original.tif"})
    )
    assert "odm_orthophoto/odm_orthophoto.original.tif" in wanted


def test_a_block_that_failed_validation_is_never_submitted(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    recorder = Recorder(task_routes())

    with pytest.raises(OdmProcessingError, match="did not pass validation"):
        submit(validate_block(scan_block(empty)), profile(), node(recorder))

    assert recorder.calls == []


def test_an_option_the_engine_does_not_have_is_refused_before_upload(tmp_path: Path) -> None:
    """ODM ignores unknown options silently, so this must fail loudly instead."""
    block = validate_block(scan_block(flat_flight(tmp_path / "flight")))
    recorder = Recorder(task_routes())

    with pytest.raises(OdmProcessingError, match="invented-option"):
        submit(
            block,
            profile(radiometry={"policy": "p", "odm": {"invented-option": 1}}),
            node(recorder),
        )

    assert not any("upload" in path for path in recorder.paths())


def test_ground_control_is_uploaded_with_the_imagery_not_passed_as_an_option(
    tmp_path: Path,
) -> None:
    """gcp and geo are not NodeODM processing options; the engine detects them by filename."""
    block = validate_block(scan_block(flat_flight(tmp_path / "flight", control=True)))
    recorder = Recorder(task_routes())

    submit(block, profile(), node(recorder))

    uploaded = b"".join(call.content for call in recorder.calls if "upload" in call.url.path)
    assert b"gcp_list.txt" in uploaded
    init_body = recorder.calls[1].content.decode()
    assert "gcp" not in init_body


def test_the_task_carries_the_block_id_and_the_requested_outputs(tmp_path: Path) -> None:
    block = validate_block(scan_block(flat_flight(tmp_path / "DJI_flight_B084")))
    recorder = Recorder(task_routes())

    uuid = submit(block, profile(), node(recorder))

    assert uuid == "task-1"
    init_body = recorder.calls[1].content.decode()
    assert "DJI_flight_B084" in init_body
    assert "outputs" in init_body


def make_archive(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as bundle:
        for name, payload in members.items():
            bundle.writestr(name, payload)
    return path


def test_requested_assets_are_extracted_from_the_archive(tmp_path: Path) -> None:
    archive = make_archive(
        tmp_path / "all.zip",
        {ORTHO_ASSET: b"ortho", DSM_ASSET: b"dsm", "odm_texturing/model.obj": b"huge"},
    )

    found = extract_assets(archive, tmp_path / "out", [ORTHO_ASSET, DSM_ASSET])

    assert set(found) == {ORTHO_ASSET, DSM_ASSET}
    assert found[ORTHO_ASSET].read_bytes() == b"ortho"
    assert not (tmp_path / "out" / "model.obj").exists()


def test_assets_are_found_even_when_the_archive_is_rooted_at_a_task_directory(
    tmp_path: Path,
) -> None:
    archive = make_archive(tmp_path / "all.zip", {f"task-1/{ORTHO_ASSET}": b"ortho"})

    found = extract_assets(archive, tmp_path / "out", [ORTHO_ASSET])

    assert found[ORTHO_ASSET].read_bytes() == b"ortho"


def test_a_missing_asset_is_reported_by_omission_not_by_an_invented_path(
    tmp_path: Path,
) -> None:
    archive = make_archive(tmp_path / "all.zip", {ORTHO_ASSET: b"ortho"})

    found = extract_assets(archive, tmp_path / "out", [ORTHO_ASSET, DSM_ASSET])

    assert set(found) == {ORTHO_ASSET}
