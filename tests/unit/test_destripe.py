"""Destriping, and the things it must not remove.

The first test shows it works. The rest are the safety property: this filter is allowed to
remove banding that is coherent along a whole row or column, and nothing else. A geological
lineament that is diagonal, or that does not span the image, has to survive -- which is the
reason destriping is applied to previews and never to a master.
"""

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from drone_photogrammetry_pipeline.colour import dn_to_linear, linear_to_dn
from drone_photogrammetry_pipeline.derive.destripe import destripe


def scene(size: int = 512, value: int = 140) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(3)
    base = rng.normal(value, 8.0, (size, size))
    rgb = np.clip(np.stack([base, base * 0.95, base * 0.85]), 0, 255).astype(np.uint8)
    return rgb, np.full((size, size), 255, dtype=np.uint8)


def add_stripes(
    rgb: np.ndarray, *, axis: int, period: int = 64, amplitude: float = 0.06
) -> NDArray[np.uint8]:
    """Multiply alternating full-length bands, the way an exposure difference would."""
    size = rgb.shape[1 + axis]
    factor = 1.0 + amplitude * np.sign(np.sin(2 * np.pi * np.arange(size) / period))
    linear = dn_to_linear(rgb.astype(np.float32)).astype(np.float64)
    shape = (size, 1) if axis == 0 else (1, size)
    linear *= factor.reshape(shape)[None, :, :]
    return np.clip(
        np.rint(linear_to_dn(np.clip(linear, 0, 255).astype(np.float32))), 0, 255
    ).astype(np.uint8)


@pytest.mark.parametrize("axis", [0, 1])
def test_banding_is_removed_on_either_axis(axis: int) -> None:
    """Flight direction varies between blocks, so both axes must be handled."""
    rgb, alpha = scene()
    striped = add_stripes(rgb, axis=axis)

    _, result = destripe(striped, alpha)

    assert result.ripple_before_pct > 2.0, "fixture must actually contain banding"
    assert result.reduction_pct > 60.0


def test_an_unstriped_block_is_left_essentially_alone() -> None:
    """A correction of about 1.0 where there is nothing to correct."""
    rgb, alpha = scene()

    corrected, result = destripe(rgb, alpha)

    assert result.ripple_before_pct < 1.0
    assert float(np.abs(corrected.astype(np.int16) - rgb.astype(np.int16)).mean()) < 1.5


def test_a_broad_gradient_survives() -> None:
    """Real illumination and terrain gradients are not banding and must be preserved."""
    rgb, alpha = scene()
    ramp = np.linspace(0.75, 1.25, rgb.shape[2])[None, None, :]
    graded = np.clip(rgb * ramp, 0, 255).astype(np.uint8)

    corrected, _ = destripe(graded, alpha)

    left = float(corrected[:, :, :64].mean())
    right = float(corrected[:, :, -64:].mean())
    assert right / left > 1.4, "the gradient must still be there"


def test_a_diagonal_lineament_survives() -> None:
    """A fault or dyke at an angle is not coherent along any row or column."""
    rgb, alpha = scene()
    marked = rgb.copy()
    size = marked.shape[1]
    rows = np.arange(size)
    for offset in (-1, 0, 1):
        columns = np.clip(rows + offset, 0, size - 1)
        marked[:, rows, columns] = 40

    corrected, _ = destripe(marked, alpha)

    on_line = float(corrected[0, rows, np.clip(rows, 0, size - 1)].mean())
    background = float(np.median(corrected[0]))
    assert background - on_line > 50, "the diagonal feature must remain dark against its ground"


def test_a_partial_lineament_survives() -> None:
    """A feature crossing only part of the image is not full-length coherent banding."""
    rgb, alpha = scene()
    marked = rgb.copy()
    marked[:, 200:210, :150] = 40

    corrected, _ = destripe(marked, alpha)

    feature = float(corrected[0, 200:210, :150].mean())
    background = float(np.median(corrected[0]))
    assert background - feature > 50


def test_colour_balance_is_not_shifted() -> None:
    """Gains come from luminance only; per-band gains would rewrite scene colour."""
    rgb, alpha = scene()
    striped = add_stripes(rgb, axis=1)

    corrected, _ = destripe(striped, alpha)

    before = striped[0].astype(np.float64).mean() / striped[2].astype(np.float64).mean()
    after = corrected[0].astype(np.float64).mean() / corrected[2].astype(np.float64).mean()
    assert after == pytest.approx(before, rel=0.02)


def test_invalid_ground_does_not_drive_the_correction() -> None:
    """A block's transparent margin must not be read as a dark stripe."""
    rgb, alpha = scene()
    alpha = alpha.copy()
    alpha[:, :40] = 0
    rgb = rgb.copy()
    rgb[:, :, :40] = 0

    corrected, result = destripe(rgb, alpha)

    assert result.ripple_before_pct < 2.0
    interior = corrected[:, :, 80:]
    assert 120 < float(interior.mean()) < 160


def test_a_three_band_array_is_required() -> None:
    with pytest.raises(ValueError, match="3-band"):
        destripe(np.zeros((4, 16, 16), dtype=np.uint8), np.full((16, 16), 255, dtype=np.uint8))
