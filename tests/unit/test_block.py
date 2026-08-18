"""Block configuration tests, including the shipped examples.

The examples encode a real delivery's reference system. If one of them stops parsing or
starts reporting a different vertical reference, that is a defect worth failing on, because
the vertical reference cannot be recovered from the rasters themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drone_photogrammetry_pipeline.models.block import BlockConfig, load_block_config
from drone_photogrammetry_pipeline.models.enums import HeightType, SensorId

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize("name", ["p1_block.yaml", "l3_rgb_block.yaml"])
def test_shipped_examples_parse(name: str) -> None:
    load_block_config(EXAMPLES_DIR / name)


def test_buduunkhad_example_records_normal_heights_not_orthometric() -> None:
    """Baltic 1977 is a normal-height system over a quasigeoid, not an orthometric one.

    Recording it as ORTHOMETRIC would be a metre-level error, and `geoid_applied` must stay
    true: the geoid was applied in the field, and applying it again costs about 48 m.
    """
    block = load_block_config(EXAMPLES_DIR / "l3_rgb_block.yaml")

    assert block.vertical.height_type is HeightType.NORMAL
    assert block.vertical.epsg == 5705
    assert block.vertical.geoid_applied is True


def test_buduunkhad_example_matches_the_delivered_rasters() -> None:
    block = load_block_config(EXAMPLES_DIR / "l3_rgb_block.yaml")

    assert block.project_id == "Buduunkhad"
    assert block.sensor is SensorId.L3_RGB
    assert block.crs == "EPSG:32647"


def test_master_crs_is_compound_when_a_vertical_code_is_declared() -> None:
    block = load_block_config(EXAMPLES_DIR / "l3_rgb_block.yaml")
    assert block.master_crs == "EPSG:32647+5705"


def test_master_crs_stays_horizontal_when_no_vertical_code_is_declared() -> None:
    block = BlockConfig(project_id="P", block_id="B1", crs="EPSG:32647")
    assert block.master_crs == "EPSG:32647"


def test_master_crs_is_absent_when_no_crs_is_declared() -> None:
    assert BlockConfig(project_id="P", block_id="B1").master_crs is None


def test_an_undeclared_vertical_reference_stays_unknown() -> None:
    """Absence must not quietly become ellipsoidal."""
    block = BlockConfig(project_id="P", block_id="B1", crs="EPSG:32647")

    assert block.vertical.height_type is HeightType.UNKNOWN
    assert block.vertical.epsg is None
    assert block.vertical.geoid_applied is None
