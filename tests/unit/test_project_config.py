"""A project configuration is the one place two dangerous values are written down.

`declare_crs` and `height_type` relocate every elevation if they disagree, and neither can be
checked against the imagery. These tests pin the refusals rather than the happy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drone_photogrammetry_pipeline.models.enums import HeightType, SensorId, SourceType
from drone_photogrammetry_pipeline.models.project import (
    ProjectConfig,
    ProjectConfigError,
    load_project,
    resolve_source_root,
)

BUDUUNKHAD = """
project_id: Buduunkhad
source_root: ../mining_pipeline_inputs/buduunkhad
source_type: DJI_TERRA
asset: dom.tif
declare_crs: EPSG:32647+5705
height_type: NORMAL
sensor: L2_RGB
overview_gsd: 1.0
notes: geoid already applied in the field
"""


def write_config(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_configuration_round_trips_its_declared_values(tmp_path: Path) -> None:
    config = load_project(write_config(tmp_path / "buduunkhad.yaml", BUDUUNKHAD))

    assert config.project_id == "Buduunkhad"
    assert config.source_type is SourceType.DJI_TERRA
    assert config.declare_crs == "EPSG:32647+5705"
    assert config.height_type is HeightType.NORMAL
    assert config.sensor is SensorId.L2_RGB
    assert config.overview_gsd == 1.0


def test_a_relative_source_root_resolves_against_the_configuration_file(tmp_path: Path) -> None:
    """So a config and its delivery can be moved together and the path keeps its meaning."""
    path = write_config(tmp_path / "projects" / "buduunkhad.yaml", BUDUUNKHAD)

    config = load_project(path)

    assert config.source_root is not None
    assert config.source_root.is_absolute()
    assert config.source_root == (tmp_path / "mining_pipeline_inputs" / "buduunkhad").resolve()


def test_an_absolute_source_root_is_left_alone(tmp_path: Path) -> None:
    delivery = tmp_path / "delivery"
    path = write_config(
        tmp_path / "p.yaml",
        f"project_id: Sant\nsource_root: {delivery.as_posix()}\nsource_type: DJI_TERRA\n",
    )

    assert load_project(path).source_root == delivery.resolve()


def test_a_vertical_component_without_a_height_type_is_refused(tmp_path: Path) -> None:
    """The declaration exists to remove an ambiguity; leaving the surface unstated restores it.

    Normal and orthometric heights are different surfaces, and the Baltic 1977 system these
    deliveries use is normal. A number whose surface is unstated is not a height.
    """
    path = write_config(
        tmp_path / "p.yaml",
        "project_id: Sant\nsource_root: .\nsource_type: DJI_TERRA\ndeclare_crs: EPSG:32649+5705\n",
    )

    with pytest.raises(ProjectConfigError, match="height_type is UNKNOWN"):
        load_project(path)


def test_a_horizontal_only_declaration_needs_no_height_type(tmp_path: Path) -> None:
    """Sant has no document stating a vertical datum, so it must declare none."""
    path = write_config(
        tmp_path / "p.yaml",
        "project_id: Sant\nsource_root: .\nsource_type: DJI_TERRA\ndeclare_crs: EPSG:32649\n",
    )

    assert load_project(path).height_type is HeightType.UNKNOWN


def test_the_error_says_which_file_it_came_from(tmp_path: Path) -> None:
    """A validation message with no filename is useless when several projects are configured."""
    path = write_config(tmp_path / "sant.yaml", "project_id: '  '\nsource_root: .\n")

    with pytest.raises(ProjectConfigError, match="sant.yaml"):
        load_project(path)


def test_a_missing_file_is_reported_as_such(tmp_path: Path) -> None:
    with pytest.raises(ProjectConfigError, match="no project configuration"):
        load_project(tmp_path / "absent.yaml")


def test_a_document_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    path = write_config(tmp_path / "p.yaml", "- Buduunkhad\n- Sant\n")

    with pytest.raises(ProjectConfigError, match="must contain a mapping"):
        load_project(path)


def test_invalid_yaml_is_refused_before_validation(tmp_path: Path) -> None:
    path = write_config(tmp_path / "p.yaml", "project_id: [unclosed\n")

    with pytest.raises(ProjectConfigError, match="not valid YAML"):
        load_project(path)


def test_an_unknown_key_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    """A silently ignored `destripe_preview` reads as configured and does nothing."""
    path = write_config(
        tmp_path / "p.yaml",
        "project_id: Sant\nsource_root: .\nsource_type: DJI_TERRA\ndestripe_preview: false\n",
    )

    with pytest.raises(ProjectConfigError):
        load_project(path)


def test_a_zero_ground_sample_distance_is_refused() -> None:
    with pytest.raises(ValueError, match="overview_gsd"):
        ProjectConfig(project_id="Sant", source_root=Path(), overview_gsd=0.0)


def test_a_project_without_a_source_type_is_a_raw_imagery_project() -> None:
    """Left unset rather than defaulted: this pipeline is then the producer, not the consumer."""
    assert ProjectConfig(project_id="Buduunkhad P1", source_root=Path()).source_type is None


@pytest.mark.parametrize("name", ["buduunkhad.yaml", "sant.yaml"])
def test_the_configurations_shipped_with_the_repository_load(name: str) -> None:
    """These are the two commands a new user runs first; a typo in one is a bad first minute."""
    shipped = Path(__file__).resolve().parents[2] / "projects" / name

    config = load_project(shipped)

    assert config.project_id
    assert config.source_type is SourceType.DJI_TERRA
    # No committed path: it would be one machine's drive letter in everybody's checkout.
    assert config.source_root is None


def test_sources_are_found_by_convention_when_nothing_says_otherwise(tmp_path: Path) -> None:
    """The case a fresh clone is in: one setting in .env and no per-project edits."""
    inputs = tmp_path / "drone_inputs"
    (inputs / "buduunkhad").mkdir(parents=True)

    found = resolve_source_root(
        ProjectConfig(project_id="Buduunkhad"), inputs_root=inputs, slug="buduunkhad"
    )

    assert found == (inputs / "buduunkhad").resolve()


def test_the_command_line_wins_over_the_configuration_and_the_convention(tmp_path: Path) -> None:
    elsewhere = tmp_path / "delivery_on_a_usb_disk"
    elsewhere.mkdir()
    configured = tmp_path / "configured"
    configured.mkdir()
    (tmp_path / "inputs" / "sant").mkdir(parents=True)

    found = resolve_source_root(
        ProjectConfig(project_id="Sant", source_root=configured),
        inputs_root=tmp_path / "inputs",
        slug="sant",
        override=elsewhere,
    )

    assert found == elsewhere.resolve()


def test_the_configuration_wins_over_the_convention(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    configured.mkdir()
    (tmp_path / "inputs" / "sant").mkdir(parents=True)

    found = resolve_source_root(
        ProjectConfig(project_id="Sant", source_root=configured),
        inputs_root=tmp_path / "inputs",
        slug="sant",
    )

    assert found == configured.resolve()


def test_a_candidate_that_is_not_a_directory_falls_through_to_the_next(tmp_path: Path) -> None:
    """A stale path in a configuration must not mask a delivery that is where it should be."""
    (tmp_path / "inputs" / "sant").mkdir(parents=True)

    found = resolve_source_root(
        ProjectConfig(project_id="Sant", source_root=tmp_path / "moved_away"),
        inputs_root=tmp_path / "inputs",
        slug="sant",
    )

    assert found == (tmp_path / "inputs" / "sant").resolve()


def test_when_nothing_is_found_the_error_lists_every_path_tried(tmp_path: Path) -> None:
    """ "No such directory" without saying which ones were considered helps nobody."""
    with pytest.raises(ProjectConfigError) as raised:
        resolve_source_root(
            ProjectConfig(project_id="Sant", source_root=tmp_path / "configured"),
            inputs_root=tmp_path / "inputs",
            slug="sant",
            override=tmp_path / "passed_in",
        )

    message = str(raised.value)
    assert "passed_in" in message
    assert "configured" in message
    assert str(Path("inputs") / "sant") in message
