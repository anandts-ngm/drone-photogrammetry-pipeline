"""Reading what is on disk for a block.

Two layouts are supported, because both exist in practice:

* **structured** — the documented layout, with `imagery/`, `navigation/`, `control/`,
  `checkpoints/`, `reference/` and `block.yaml`.
* **flat** — a DJI flight folder exactly as it comes off the aircraft, with images and
  navigation files side by side in one directory.

Supporting the flat layout is not a convenience: it is what the real deliveries look like,
and requiring operators to restructure a folder before it can be read would invite exactly
the kind of hand-editing that loses files.

This module reads. It never writes, moves or renames anything under the block root.
"""

from __future__ import annotations

from pathlib import Path

from ..models.block import Block, BlockConfig, load_block_config

# What ODM will actually ingest. RAW formats are deliberately absent: ODM lists .dng among
# its extensions but does not process it, so a raw file must be converted first and being
# permissive here would let it reach the engine and fail late.
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".tif", ".tiff", ".png"})

# DJI writes navigation beside the imagery: MRK event marks, PPK observables and the
# solution files. Matched by suffix and by name because DJI uses both conventions.
NAVIGATION_SUFFIXES = frozenset({".mrk", ".obs", ".nav", ".bin", ".rtk", ".ppk", ".rtb"})
NAVIGATION_MARKERS = ("ppk", "rtk", "timestamp", "rinex")

# ODM detects ground control and geolocation by filename, so the same names are what this
# scanner looks for.
GCP_NAMES = ("gcp_list.txt", "gcp.txt")
GEO_NAMES = ("geo.txt",)

STRUCTURED_DIRS = ("imagery", "navigation", "control", "checkpoints", "reference")

LAYOUT_STRUCTURED = "structured"
LAYOUT_FLAT = "flat"


class BlockScanError(RuntimeError):
    pass


def _files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file())


def _images_in(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.suffix.lower() in IMAGE_SUFFIXES]


def _navigation_in(paths: list[Path]) -> list[Path]:
    found = []
    for path in paths:
        name = path.name.lower()
        looks_like_navigation = path.suffix.lower() in NAVIGATION_SUFFIXES or any(
            marker in name for marker in NAVIGATION_MARKERS
        )
        if looks_like_navigation and path.suffix.lower() not in IMAGE_SUFFIXES:
            found.append(path)
    return found


def _control_in(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.name.lower() in GCP_NAMES + GEO_NAMES]


def detect_layout(root: Path) -> str:
    return (
        LAYOUT_STRUCTURED
        if any((root / name).is_dir() for name in STRUCTURED_DIRS)
        else LAYOUT_FLAT
    )


def scan_block(root: Path, *, block_id: str | None = None) -> Block:
    """Inventory a block directory without modifying it."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise BlockScanError(f"{root} is not a directory")

    config: BlockConfig | None = None
    config_path = root / "block.yaml"
    if config_path.is_file():
        config = load_block_config(config_path)

    layout = detect_layout(root)
    if layout == LAYOUT_STRUCTURED:
        images = _images_in(_files(root / "imagery"))
        navigation = _files(root / "navigation")
        control = _files(root / "control")
        checkpoints = _files(root / "checkpoints")
        reference = _files(root / "reference")
    else:
        top = _files(root)
        images = _images_in(top)
        navigation = _navigation_in(top)
        control = _control_in(top)
        checkpoints = []
        reference = [
            path
            for path in top
            if path not in images and path not in navigation and path not in control
        ]

    return Block(
        block_id=block_id or (config.block_id if config else root.name),
        root=root,
        layout=layout,
        config=config,
        images=images,
        navigation=navigation,
        control=control,
        checkpoints=checkpoints,
        reference=reference,
    )
