"""The two interactive commands, and the resolution chosen for a new area.

These are the only prompts in the tool. They run before anything is processed, which is the
whole argument for them being here and nowhere else: there is nothing in flight to interrupt,
and the answers land in files that can be reviewed rather than in a terminal that scrolls away.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import Result
from rasterio.transform import from_origin
from typer.testing import CliRunner

from drone_photogrammetry_pipeline.cli import EXIT_FAIL, EXIT_PASS, app
from drone_photogrammetry_pipeline.config import get_settings
from drone_photogrammetry_pipeline.derive.overview import choose_gsd
from drone_photogrammetry_pipeline.derive.preview import PreviewError
from drone_photogrammetry_pipeline.models.enums import HeightType
from drone_photogrammetry_pipeline.models.project import load_project
from tests.fixtures import make_rasters

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A clone-shaped working directory: a .git, a projects/, and the env template."""
    root = tmp_path / "checkout"
    (root / "projects").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".env.example").write_text(
        "DPP_INPUTS_ROOT=./inputs\nDPP_WORKSPACE_ROOT=./workspace\nDPP_LOG_LEVEL=INFO\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.delenv("DPP_INPUTS_ROOT", raising=False)
    monkeypatch.delenv("DPP_WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("DPP_PROFILES_DIR", str(REPO_ROOT / "profiles"))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def write_delivery(root: Path, blocks: tuple[str, ...], *, pixel: float = 0.02) -> Path:
    for index, block_id in enumerate(blocks):
        (root / block_id).mkdir(parents=True, exist_ok=True)
        make_rasters.terra_dom_source(
            root / block_id / "dom.tif",
            crs="EPSG:32647",
            transform=from_origin(500_000.0 + index * 0.6, 5_000_000.0, pixel, pixel),
        )
    return root


def run(*args: str, answers: str = "") -> Result:
    return runner.invoke(app, list(args), input=answers)


# --- the resolution chosen for a survey ------------------------------------------------------


def test_the_real_surveys_get_a_browsable_size(tmp_path: Path) -> None:
    """Measured extents, not guessed ones: Buduunkhad is 9,766 m across and Sant 2,667 m.

    Sant comes out at the 0.5 m that was chosen by hand. Buduunkhad comes out at 1 m rather
    than the 0.5 m it was given, which is the point: 0.5 m there is 19531 x 12869 px and
    404 MB, four times more browse image than a browse image needs.
    """
    assert choose_gsd(9766.0, finest_native=0.0254) == 1.0
    assert choose_gsd(2667.0, finest_native=0.0159) == 0.5


def test_a_small_area_gets_a_finer_resolution_rather_than_a_thumbnail() -> None:
    """The other half of the reason: one fixed value cannot serve 6,285 ha and 350 ha."""
    assert choose_gsd(1870.0, finest_native=0.00181) == 0.2
    assert choose_gsd(400.0, finest_native=0.00181) == 0.05


def test_it_never_goes_finer_than_the_masters_it_reads() -> None:
    """Past the native size it is upsampling: more bytes, no more detail."""
    assert choose_gsd(100.0, finest_native=0.05) == 0.05


def test_the_long_edge_stays_under_the_cap() -> None:
    """A cap, not a target: a browse image too big to browse is the failure that matters."""
    for extent in (400.0, 1870.0, 2667.0, 9766.0, 60_000.0):
        chosen = choose_gsd(extent, finest_native=0.001)
        assert extent / chosen <= 10_000, (extent, chosen)


def test_the_ladder_keeps_the_number_speakable() -> None:
    """These values end up in filenames and conversations, so 0.977 m is not an answer."""
    for extent in (3000.0, 7000.0, 12_000.0, 40_000.0):
        chosen = choose_gsd(extent, finest_native=0.01)
        assert chosen in {0.5, 1.0, 2.0, 5.0}, chosen


def test_a_survey_with_no_extent_is_an_error() -> None:
    with pytest.raises(PreviewError):
        choose_gsd(0.0, finest_native=0.02)


# --- init -----------------------------------------------------------------------------------


def test_init_writes_both_paths_and_creates_what_is_missing(checkout: Path) -> None:
    inputs = checkout.parent / "drone_inputs"
    outputs = checkout.parent / "outputs"

    result = run("init", answers=f"{inputs.as_posix()}\n{outputs.as_posix()}\ny\ny\n")

    assert result.exit_code == EXIT_PASS, result.output
    env = (checkout / ".env").read_text(encoding="utf-8")
    assert f"DPP_INPUTS_ROOT={inputs.as_posix()}" in env
    assert f"DPP_WORKSPACE_ROOT={outputs.as_posix()}" in env
    assert inputs.is_dir() and outputs.is_dir()


def test_init_keeps_settings_it_was_not_asked_about(checkout: Path) -> None:
    """Someone set a node URL for a reason; running this again must not drop it."""
    (checkout / ".env").write_text(
        "DPP_NODEODM_URL=http://gpu-box:3000\nDPP_INPUTS_ROOT=./old\n", encoding="utf-8"
    )
    inputs = checkout.parent / "in"
    inputs.mkdir()
    outputs = checkout.parent / "out"
    outputs.mkdir()

    result = run("init", answers=f"{inputs.as_posix()}\n{outputs.as_posix()}\n")

    assert result.exit_code == EXIT_PASS, result.output
    env = (checkout / ".env").read_text(encoding="utf-8")
    assert "DPP_NODEODM_URL=http://gpu-box:3000" in env
    assert "./old" not in env


def test_init_refuses_a_workspace_inside_the_checkout(checkout: Path) -> None:
    """The mistake the default invites, caught before 75 GB lands in a git working tree."""
    inputs = checkout.parent / "in"
    inputs.mkdir()

    result = run("init", answers=f"{inputs.as_posix()}\n{(checkout / 'workspace').as_posix()}\n")

    assert result.exit_code == EXIT_FAIL
    assert "inside the git repository" in result.output
    assert not (checkout / ".env").exists(), "nothing may be written when a path is refused"


def test_init_reports_which_projects_have_blocks_and_which_do_not(checkout: Path) -> None:
    inputs = checkout.parent / "drone_inputs"
    write_delivery(inputs / "artsat", ("A1", "A2"))
    (checkout / "projects" / "artsat.yaml").write_text(
        "project_id: Artsat\nsource_type: DJI_TERRA\n", encoding="utf-8"
    )
    (checkout / "projects" / "sant.yaml").write_text(
        "project_id: Sant\nsource_type: DJI_TERRA\n", encoding="utf-8"
    )
    outputs = checkout.parent / "out"
    outputs.mkdir()

    result = run("init", answers=f"{inputs.as_posix()}\n{outputs.as_posix()}\n")

    assert result.exit_code == EXIT_PASS, result.output
    assert "Artsat: 2 blocks" in result.output
    assert "Sant: no blocks yet" in result.output


def test_init_asks_nothing_when_told_not_to(checkout: Path) -> None:
    """So a setup script can run it, and so it cannot block on a machine with no terminal."""
    inputs = checkout.parent / "in"
    outputs = checkout.parent / "out"

    result = run("init", "--inputs", str(inputs), "--workspace", str(outputs), "--yes")

    assert result.exit_code == EXIT_PASS, result.output
    assert inputs.is_dir() and outputs.is_dir()


# --- new-project ----------------------------------------------------------------------------


def test_new_project_writes_a_file_that_loads(checkout: Path) -> None:
    delivery = write_delivery(checkout.parent / "artsat", ("A1", "A2", "A3"))

    result = run("new-project", "Artsat", "--source-root", str(delivery), answers="n\n")

    assert result.exit_code == EXIT_PASS, result.output
    config = load_project(checkout / "projects" / "artsat.yaml")
    assert config.project_id == "Artsat"
    assert config.overview_gsd is None, "chosen from the extent, not written down"


def test_declining_the_vertical_question_declares_nothing(checkout: Path) -> None:
    """A guessed datum is indistinguishable from a measured one downstream."""
    delivery = write_delivery(checkout.parent / "artsat", ("A1",))

    result = run("new-project", "Artsat", "--source-root", str(delivery), answers="n\n")

    assert result.exit_code == EXIT_PASS, result.output
    config = load_project(checkout / "projects" / "artsat.yaml")
    assert config.declare_crs is None
    assert config.height_type is HeightType.UNKNOWN
    assert "not be suitable for absolute-Z" in result.output


def test_a_stated_datum_is_composed_onto_the_horizontal_crs_of_the_sources(
    checkout: Path,
) -> None:
    """The horizontal half comes from the files; only the vertical half is a human answer."""
    delivery = write_delivery(checkout.parent / "artsat", ("A1",))

    result = run(
        "new-project",
        "Artsat",
        "--source-root",
        str(delivery),
        answers="y\n5705\nNORMAL\nMETADATA_Artsat.txt\n",
    )

    assert result.exit_code == EXIT_PASS, result.output
    config = load_project(checkout / "projects" / "artsat.yaml")
    assert config.declare_crs == "EPSG:32647+5705"
    assert config.height_type is HeightType.NORMAL
    assert "METADATA_Artsat.txt" in config.notes, "the document is the evidence; keep its name"


def test_a_vertical_component_without_a_height_type_is_refused(checkout: Path) -> None:
    delivery = write_delivery(checkout.parent / "artsat", ("A1",))

    result = run(
        "new-project",
        "Artsat",
        "--source-root",
        str(delivery),
        "--declare-crs",
        "EPSG:32647+5705",
        "--yes",
    )

    assert result.exit_code == EXIT_FAIL
    assert "adds a vertical component" in result.output


def test_an_existing_project_file_is_not_overwritten_silently(checkout: Path) -> None:
    delivery = write_delivery(checkout.parent / "artsat", ("A1",))
    (checkout / "projects" / "artsat.yaml").write_text("project_id: Artsat\n", encoding="utf-8")

    result = run("new-project", "Artsat", "--source-root", str(delivery), "--yes")

    assert result.exit_code == EXIT_FAIL
    assert "already exists" in result.output


def test_a_directory_with_no_blocks_says_what_the_layout_should_be(checkout: Path) -> None:
    empty = checkout.parent / "artsat"
    empty.mkdir()

    result = run("new-project", "Artsat", "--source-root", str(empty), "--yes")

    assert result.exit_code == EXIT_FAIL
    assert "own directory" in result.output


def test_new_project_refuses_two_cameras_in_one_folder(checkout: Path) -> None:
    """Same check as the run itself, so the refusal comes before a file promises otherwise."""
    delivery = write_delivery(checkout.parent / "mixed", ("L1", "L2"), pixel=0.0254)
    write_delivery(delivery, ("P1_B84",), pixel=0.00181)

    result = run("new-project", "Mixed", "--source-root", str(delivery), "--yes")

    assert result.exit_code == EXIT_FAIL
    assert "own project id" in result.output
    assert not (checkout / "projects" / "mixed.yaml").exists()
