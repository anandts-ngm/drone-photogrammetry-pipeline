"""The one command a teammate runs.

`process-project` is the whole pipeline in one call, so these tests are about the things a
first-time user hits: where the sources are looked for, what happens when the folder holds two
cameras' work, and whether the products all actually appear.
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
from tests.fixtures import make_rasters

REPO_ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()

CONFIG = """
project_id: Buduunkhad
source_type: DJI_TERRA
asset: dom.tif
overview_gsd: 0.05
preview_longest_side: 64
"""


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DPP_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("DPP_INPUTS_ROOT", str(tmp_path / "inputs"))
    monkeypatch.setenv("DPP_PROFILES_DIR", str(REPO_ROOT / "profiles"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def write_blocks(root: Path, *, pixel_sizes: dict[str, float]) -> Path:
    """One directory per block, each holding a `dom.tif`, laid out west to east."""
    for index, (block_id, pixel) in enumerate(pixel_sizes.items()):
        (root / block_id).mkdir(parents=True, exist_ok=True)
        make_rasters.terra_dom_source(
            root / block_id / "dom.tif",
            transform=from_origin(500_000.0 + index * 0.5, 5_000_000.0, pixel, pixel),
        )
    return root


def config_at(path: Path, text: str = CONFIG) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def run(config: Path, *extra: str) -> Result:
    return runner.invoke(app, ["process-project", str(config), *extra])


def test_the_sources_are_found_under_the_inputs_root_without_any_path_given(
    tmp_path: Path,
) -> None:
    """What a fresh clone does: set DPP_INPUTS_ROOT once, then name only the project file."""
    write_blocks(tmp_path / "inputs" / "buduunkhad", pixel_sizes={"B1": 0.02, "B2": 0.02})

    result = run(config_at(tmp_path / "p.yaml"), "--dry-run")

    assert result.exit_code == EXIT_PASS, result.output
    assert "blocks:    2" in result.output
    assert "nothing was written" in result.output


def test_a_source_root_on_the_command_line_overrides_the_convention(tmp_path: Path) -> None:
    write_blocks(tmp_path / "inputs" / "buduunkhad", pixel_sizes={"B1": 0.02})
    elsewhere = write_blocks(
        tmp_path / "usb_disk", pixel_sizes={"Z1": 0.02, "Z2": 0.02, "Z3": 0.02}
    )

    result = run(config_at(tmp_path / "p.yaml"), "--source-root", str(elsewhere), "--dry-run")

    assert result.exit_code == EXIT_PASS, result.output
    assert "blocks:    3" in result.output


def test_a_missing_delivery_names_every_path_it_tried(tmp_path: Path) -> None:
    result = run(config_at(tmp_path / "p.yaml"), "--dry-run")

    assert result.exit_code == EXIT_FAIL
    assert "DPP_INPUTS_ROOT/buduunkhad" in result.output


def test_a_folder_of_orthos_without_block_directories_says_what_the_layout_should_be(
    tmp_path: Path,
) -> None:
    """The likely first mistake: every dom.tif dropped loose into one folder."""
    inputs = tmp_path / "inputs" / "buduunkhad"
    inputs.mkdir(parents=True)
    make_rasters.terra_dom_source(inputs / "dom.tif")

    result = run(config_at(tmp_path / "p.yaml"), "--dry-run")

    assert result.exit_code == EXIT_FAIL
    assert "own directory" in result.output


def test_two_cameras_in_one_folder_are_refused_before_any_packaging(tmp_path: Path) -> None:
    """The P1-plus-L-camera case, and the reason this is checked on the sources.

    A single mosaic grid has to be the finest pixel size present, so mixing a 2 cm delivery
    with a 2 mm one would build a grid a hundred times larger than the coarse blocks can fill.
    Refusing at the mosaic stage would mean refusing after every master had been written.
    """
    write_blocks(
        tmp_path / "inputs" / "buduunkhad",
        pixel_sizes={"B1": 0.0254, "B2": 0.0254, "P1_B84": 0.00181},
    )

    result = run(config_at(tmp_path / "p.yaml"))

    assert result.exit_code == EXIT_FAIL
    assert "14.0x" in result.output
    assert "own project id" in result.output
    assert not list((tmp_path / "workspace").rglob("*_ORTHO_MASTER.tif")), (
        "nothing may be packaged once the delivery has been refused"
    )


def test_the_spread_within_one_survey_is_accepted(tmp_path: Path) -> None:
    """Buduunkhad runs 2.54 cm to 5.11 cm across 47 distinct values and must still process."""
    write_blocks(
        tmp_path / "inputs" / "buduunkhad", pixel_sizes={"B1": 0.0254, "B2": 0.0511, "B3": 0.04}
    )

    result = run(config_at(tmp_path / "p.yaml"), "--dry-run")

    assert result.exit_code == EXIT_PASS, result.output


def test_every_product_is_written_in_one_pass(tmp_path: Path) -> None:
    """The end-to-end shape: masters, then the four derived products, in one command.

    Correction is off because these fixtures do not overlap, and a gain solved from no
    constraints is not a gain; the correction path has its own tests.
    """
    write_blocks(tmp_path / "inputs" / "buduunkhad", pixel_sizes={"B1": 0.02, "B2": 0.02})

    result = run(config_at(tmp_path / "p.yaml"), "--no-correct")

    assert result.exit_code == EXIT_PASS, result.output
    workspace = tmp_path / "workspace" / "buduunkhad"
    assert len(list(workspace.rglob("*_ORTHO_MASTER.tif"))) == 2
    assert len(list(workspace.rglob("manifest.json"))) == 2

    derived = workspace / "derived"
    assert sorted(p.name for p in derived.glob("*")) == [
        "buduunkhad_contact_sheet.jpg",
        "buduunkhad_mosaic.vrt",
        "buduunkhad_overview.jpg",
        "buduunkhad_overview.tif",
        "previews",
    ]
    assert len(list((derived / "previews").glob("*.jpg"))) == 2


def test_the_mosaic_pyramid_is_off_unless_asked_for(tmp_path: Path) -> None:
    """It reads every master once, which is hours on a real survey and never the default."""
    write_blocks(tmp_path / "inputs" / "buduunkhad", pixel_sizes={"B1": 0.02})

    result = run(config_at(tmp_path / "p.yaml"), "--no-correct")

    assert result.exit_code == EXIT_PASS, result.output
    assert not list((tmp_path / "workspace").rglob("*.vrt.ovr"))
    # And it says so, with what to do instead: a mosaic that silently will not open at full
    # extent is worse than one that explains why.
    assert "no overviews" in result.output


def test_a_second_run_reuses_the_masters_it_already_made(tmp_path: Path) -> None:
    """Resume matters at 79 blocks, and the project id is capitalised while the directory is not.

    That mismatch once made the reuse lookup read a directory nothing writes to, which is
    invisible on Windows and repackages everything anywhere else.
    """
    write_blocks(tmp_path / "inputs" / "buduunkhad", pixel_sizes={"B1": 0.02, "B2": 0.02})
    config = config_at(tmp_path / "p.yaml")

    assert run(config, "--no-correct", "--no-derived").exit_code == EXIT_PASS
    again = run(config, "--no-correct", "--no-derived")

    assert again.exit_code == EXIT_PASS, again.output
    assert again.output.count("(reused)") == 2
    assert len(list((tmp_path / "workspace").rglob("*_ORTHO_MASTER.tif"))) == 2


def test_a_raw_imagery_project_is_not_processed_here(tmp_path: Path) -> None:
    config = config_at(tmp_path / "p.yaml", "project_id: Buduunkhad P1\n")

    result = run(config, "--dry-run")

    assert result.exit_code == EXIT_FAIL
    assert "raw-imagery project" in result.output
