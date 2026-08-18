from __future__ import annotations

from pathlib import Path

from drone_photogrammetry_pipeline.integrity import (
    canonical_json_sha256,
    hash_outputs,
    sha256_bytes,
    sha256_file,
)


def test_file_and_byte_hashes_agree(tmp_path: Path) -> None:
    payload = b"orthophoto bytes"
    path = tmp_path / "file.bin"
    path.write_bytes(payload)

    assert sha256_file(path) == sha256_bytes(payload)


def test_canonical_hash_ignores_key_order_and_formatting() -> None:
    """A reformatted profile must hash the same, or profile_hash would mean nothing."""
    one = {"profile_id": "p1_35_master", "profile_version": 1, "radiometry": {"policy": "a"}}
    other = {"radiometry": {"policy": "a"}, "profile_version": 1, "profile_id": "p1_35_master"}

    assert canonical_json_sha256(one) == canonical_json_sha256(other)


def test_canonical_hash_changes_when_meaning_changes() -> None:
    baseline = {"radiometry": {"texturing-skip-global-seam-leveling": True}}
    flipped = {"radiometry": {"texturing-skip-global-seam-leveling": False}}

    assert canonical_json_sha256(baseline) != canonical_json_sha256(flipped)


def test_hash_outputs_maps_names_to_digests(tmp_path: Path) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    digests = hash_outputs({"first": first, "second": second})

    assert set(digests) == {"first", "second"}
    assert digests["first"] == sha256_bytes(b"a")
