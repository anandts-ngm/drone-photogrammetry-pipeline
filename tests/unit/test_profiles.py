"""Tests over the profiles actually shipped in the repository.

These guard decisions, not code. The radiometric settings in particular were chosen
deliberately and provisionally (docs/radiometry.md); a silent edit to one of them would
change what every future product means, so it should break a test rather than pass quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from drone_photogrammetry_pipeline.models.enums import ProcessingEngine
from drone_photogrammetry_pipeline.models.profile import (
    ProfileNotFoundError,
    load_profile,
    load_profile_file,
)

PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
ODM_PROFILE_IDS = ["p1_35_master", "p1_50_master", "l2_rgb_master", "l3_rgb_master"]


def test_every_shipped_profile_parses() -> None:
    paths = sorted(PROFILES_DIR.glob("*.yaml"))
    assert paths, "no profiles are shipped"
    for path in paths:
        load_profile_file(path)


def test_profiles_are_found_by_declared_id_not_filename() -> None:
    loaded = load_profile(PROFILES_DIR, "p1_35_master")

    assert loaded.profile.profile_id == "p1_35_master"
    assert loaded.path.stem == "p1_35"


def test_unknown_profile_id_names_what_is_available() -> None:
    with pytest.raises(ProfileNotFoundError, match="p1_35"):
        load_profile(PROFILES_DIR, "does_not_exist")


def test_profile_hash_is_stable_across_reformatting(tmp_path: Path) -> None:
    original = load_profile(PROFILES_DIR, "p1_35_master")
    document = yaml.safe_load(original.path.read_text(encoding="utf-8"))

    reformatted = tmp_path / "reformatted.yaml"
    reformatted.write_text(
        "# a comment that carries no meaning\n"
        + yaml.safe_dump(document, sort_keys=True, default_flow_style=True),
        encoding="utf-8",
    )

    assert load_profile_file(reformatted).profile_hash == original.profile_hash


@pytest.mark.parametrize("profile_id", ODM_PROFILE_IDS)
def test_odm_profiles_skip_global_seam_leveling(profile_id: str) -> None:
    """ODM normalises colours across all images unless told not to; see docs/radiometry.md."""
    profile = load_profile(PROFILES_DIR, profile_id).profile

    assert profile.radiometry.odm["texturing-skip-global-seam-leveling"] is True
    assert profile.radiometry.provisional is True, (
        "the seam-leveling choice is provisional until the B64/B66, B44/B51 and N3/N7 "
        "benchmarks have been run both ways"
    )


@pytest.mark.parametrize("profile_id", ODM_PROFILE_IDS)
def test_odm_profiles_never_request_overviews_or_lossy_compression(profile_id: str) -> None:
    """ODM builds overviews with JPEG compression, which would put lossy data in a master."""
    profile = load_profile(PROFILES_DIR, profile_id).profile

    assert profile.radiometry.odm["build-overviews"] is False
    assert profile.radiometry.odm["orthophoto-compression"] == "DEFLATE"
    assert profile.radiometry.odm["orthophoto-cutline"] is False


@pytest.mark.parametrize("profile_id", ODM_PROFILE_IDS)
def test_odm_profiles_do_not_enable_alpha_from_nodata(profile_id: str) -> None:
    profile = load_profile(PROFILES_DIR, profile_id).profile
    assert profile.packaging.allow_alpha_from_nodata is False


def test_external_terra_profile_declares_no_odm_run() -> None:
    profile = load_profile(PROFILES_DIR, "external_terra").profile

    assert profile.processing.engine is ProcessingEngine.DJI_TERRA
    assert profile.processing.odm == {}
    assert profile.radiometry.policy == "external_uncontrolled"
    assert profile.packaging.allow_alpha_from_nodata is False


def test_l3_profile_is_marked_as_a_placeholder() -> None:
    """L3 sensor specifications are not established; the file must say so."""
    text = (PROFILES_DIR / "l3_rgb.yaml").read_text(encoding="utf-8")
    assert "PLACEHOLDER" in text
