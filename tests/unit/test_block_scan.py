"""Block scanning and validation.

The flat-layout cases matter most: that is what a DJI flight folder actually looks like, and
requiring an operator to restructure one before it can be read would invite the hand-editing
that loses files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drone_photogrammetry_pipeline.ingest.scan import (
    LAYOUT_FLAT,
    LAYOUT_STRUCTURED,
    BlockScanError,
    scan_block,
)
from drone_photogrammetry_pipeline.ingest.validate import validate_block
from drone_photogrammetry_pipeline.models.enums import ValidationSeverity


def flat_flight(root: Path, *, images: int = 5, control: bool = False) -> Path:
    """A DJI flight folder as it comes off the aircraft."""
    root.mkdir(parents=True, exist_ok=True)
    for index in range(images):
        (root / f"DJI_2026080313{index:04d}_0001.JPG").write_bytes(b"x")
    (root / "DJI_202608031301_013_B084_Timestamp.MRK").write_text("mrk")
    (root / "DJI_202608031301_013_B084_PPKRAW.bin").write_bytes(b"ppk")
    (root / "DJI_202608031301_013_B084_PPKOBS.obs").write_text("obs")
    (root / "metadata.csv").write_text("a,b")
    if control:
        (root / "gcp_list.txt").write_text("WGS84 UTM 47N")
    return root


def structured_block(root: Path) -> Path:
    for name in ("imagery", "navigation", "control", "checkpoints", "reference"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for index in range(4):
        (root / "imagery" / f"DJI_{index:04d}.JPG").write_bytes(b"x")
    (root / "navigation" / "flight.MRK").write_text("mrk")
    (root / "control" / "gcp_list.txt").write_text("gcp")
    (root / "checkpoints" / "cp.csv").write_text("cp")
    (root / "reference" / "notes.txt").write_text("ref")
    (root / "block.yaml").write_text(
        "project_id: Buduunkhad\nblock_id: B064\ncrs: EPSG:32647\n"
        "vertical:\n  height_type: NORMAL\n  epsg: 5705\n",
        encoding="utf-8",
    )
    return root


def test_a_dji_flight_folder_is_read_without_restructuring(tmp_path: Path) -> None:
    block = scan_block(flat_flight(tmp_path / "DJI_202608031301_013_B084"))

    assert block.layout == LAYOUT_FLAT
    assert block.image_count == 5
    assert {p.name for p in block.navigation} >= {
        "DJI_202608031301_013_B084_Timestamp.MRK",
        "DJI_202608031301_013_B084_PPKRAW.bin",
        "DJI_202608031301_013_B084_PPKOBS.obs",
    }


def test_the_structured_layout_is_read_from_its_subdirectories(tmp_path: Path) -> None:
    block = scan_block(structured_block(tmp_path / "B064"))

    assert block.layout == LAYOUT_STRUCTURED
    assert block.image_count == 4
    assert [p.name for p in block.control] == ["gcp_list.txt"]
    assert [p.name for p in block.checkpoints] == ["cp.csv"]
    assert block.config is not None and block.config.crs == "EPSG:32647"


def test_the_block_id_comes_from_the_config_before_the_directory_name(tmp_path: Path) -> None:
    block = scan_block(structured_block(tmp_path / "some-other-name"))
    assert block.block_id == "B064"


def test_the_directory_name_is_used_when_nothing_declares_a_block_id(tmp_path: Path) -> None:
    block = scan_block(flat_flight(tmp_path / "DJI_202608031301_013_B084"))
    assert block.block_id == "DJI_202608031301_013_B084"


def test_navigation_files_are_never_mistaken_for_imagery(tmp_path: Path) -> None:
    root = flat_flight(tmp_path / "flight")
    block = scan_block(root)

    assert all(p.suffix.lower() in {".jpg"} for p in block.images)
    assert not any(p.suffix.lower() == ".jpg" for p in block.navigation)


def test_scanning_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BlockScanError, match="not a directory"):
        scan_block(tmp_path / "nope")


def test_a_block_with_imagery_is_processable(tmp_path: Path) -> None:
    validated = validate_block(scan_block(flat_flight(tmp_path / "flight")))

    assert validated.is_processable
    assert validated.fatal == []


def test_a_block_with_no_imagery_is_fatal(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "DJI_flight.MRK").write_text("mrk")

    validated = validate_block(scan_block(empty))

    assert not validated.is_processable
    assert [f.name for f in validated.fatal] == ["imagery"]


def test_a_single_image_cannot_be_reconstructed(tmp_path: Path) -> None:
    validated = validate_block(scan_block(flat_flight(tmp_path / "flight", images=1)))

    assert not validated.is_processable
    assert "overlapping" in validated.fatal[0].detail


def test_missing_ground_control_is_acceptable_not_fatal(tmp_path: Path) -> None:
    """An RTK or PPK block legitimately has none."""
    validated = validate_block(scan_block(flat_flight(tmp_path / "flight", control=False)))

    control = next(f for f in validated.findings if f.name == "control")
    assert control.severity is ValidationSeverity.MISSING_ACCEPTABLE
    assert validated.is_processable


def test_missing_check_points_narrows_what_may_be_claimed(tmp_path: Path) -> None:
    validated = validate_block(scan_block(flat_flight(tmp_path / "flight")))

    checkpoints = next(f for f in validated.findings if f.name == "checkpoints")
    assert checkpoints.severity is ValidationSeverity.MISSING_ACCEPTABLE
    assert "no accuracy may be claimed" in checkpoints.detail


def test_an_undeclared_vertical_reference_blocks_absolute_z_but_not_the_run(
    tmp_path: Path,
) -> None:
    validated = validate_block(scan_block(flat_flight(tmp_path / "flight")))

    assert validated.is_processable
    assert validated.suitable_for_absolute_z is False
    vertical = next(f for f in validated.findings if f.name == "vertical_reference")
    assert vertical.severity is ValidationSeverity.MISSING_ACCEPTABLE


def test_a_declared_vertical_reference_permits_absolute_z(tmp_path: Path) -> None:
    validated = validate_block(scan_block(structured_block(tmp_path / "B064")))

    assert validated.suitable_for_absolute_z is True
    vertical = next(f for f in validated.findings if f.name == "vertical_reference")
    assert vertical.severity is ValidationSeverity.REQUIRED_PRESENT
    assert "NORMAL" in vertical.detail


def test_ground_control_is_found_in_a_flat_folder(tmp_path: Path) -> None:
    block = scan_block(flat_flight(tmp_path / "flight", control=True))
    assert [p.name for p in block.control] == ["gcp_list.txt"]


def test_scanning_never_modifies_the_block(tmp_path: Path) -> None:
    root = flat_flight(tmp_path / "flight", control=True)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in root.iterdir())

    scan_block(root)
    validate_block(scan_block(root))

    assert sorted((p.name, p.stat().st_mtime_ns) for p in root.iterdir()) == before
