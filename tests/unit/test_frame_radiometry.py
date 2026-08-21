"""Per-frame radiometric normalisation.

The properties pinned here are the ones the whole Tier A argument rests on: that the sRGB
inversion is a true inverse including its linear toe, that removing exposure makes two frames
of the same scene agree, and that normalisation can never clip.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from drone_photogrammetry_pipeline.processing.frame_radiometry import (
    FrameExposure,
    FrameRadiometryError,
    channel_ratio,
    linear_to_srgb,
    normalise_frame,
    reference_exposure_factor,
    srgb_to_linear,
)


def frame(shutter: float, *, iso: float = 100.0, f_number: float = 2.8) -> FrameExposure:
    return FrameExposure(
        path=Path(f"DJI_{shutter}.JPG"), shutter_seconds=shutter, f_number=f_number, iso=iso
    )


def test_srgb_inversion_round_trips_across_the_full_range() -> None:
    values = np.linspace(0.0, 1.0, 4096, dtype=np.float32)
    assert np.allclose(linear_to_srgb(srgb_to_linear(values)), values, atol=1e-5)


def test_the_linear_toe_is_not_a_power_law() -> None:
    """Below the sRGB threshold the curve is linear; treating it as x**2.2 is wrong there.

    Shadowed rock faces sit in this region, so the error would land exactly where lithology
    is hardest to read.
    """
    dark = np.float32(0.02)
    assert srgb_to_linear(dark) == pytest.approx(dark / 12.92, rel=1e-6)
    assert srgb_to_linear(dark) != pytest.approx(dark**2.4, rel=1e-3)


def test_exposure_factor_follows_shutter_iso_and_aperture() -> None:
    assert frame(1 / 1000).exposure_factor == pytest.approx(100 / (1000 * 2.8**2))
    # Doubling the time and halving the sensitivity collects the same light.
    assert frame(1 / 500, iso=50).exposure_factor == pytest.approx(frame(1 / 1000).exposure_factor)
    # One stop slower is twice the light.
    assert frame(1 / 500).exposure_factor == pytest.approx(2 * frame(1 / 1000).exposure_factor)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_an_impossible_exposure_is_refused_rather_than_producing_a_silent_infinity(
    bad: float,
) -> None:
    with pytest.raises(FrameRadiometryError):
        _ = frame(bad).exposure_factor


def test_two_frames_of_the_same_scene_agree_after_normalisation() -> None:
    """The claim Tier A rests on: remove exposure and the same ground reads the same.

    A scene is imaged at 1/500 and again at 1/1600. Before normalisation the two differ by
    the exposure ratio; afterwards they must agree to within 8-bit quantisation.
    """
    scene = np.linspace(0.02, 0.55, 3 * 64 * 64, dtype=np.float32).reshape(3, 64, 64)

    slow, fast = frame(1 / 500), frame(1 / 1600)
    ratio = fast.exposure_factor / slow.exposure_factor

    def encode(lin: np.ndarray) -> NDArray[np.uint8]:
        encoded: NDArray[np.uint8] = np.clip(linear_to_srgb(lin) * 255.0, 0, 255).astype(np.uint8)
        return encoded

    reference = reference_exposure_factor([slow, fast])
    out_slow = normalise_frame(encode(scene), slow, reference)
    out_fast = normalise_frame(encode(scene * ratio), fast, reference)

    # Both land on the darkest exposure, so compare against the scene scaled likewise.
    assert np.allclose(out_slow.astype(np.float32), out_fast.astype(np.float32), atol=400)
    disagreement_before = abs(1.0 - ratio)
    disagreement_after = float(
        np.median(np.abs(out_slow.astype(np.float32) - out_fast.astype(np.float32)))
        / max(np.median(out_slow.astype(np.float32)), 1.0)
    )
    assert disagreement_after < disagreement_before / 10


def test_the_reference_is_the_minimum_so_normalisation_can_never_clip() -> None:
    exposures = [frame(1 / 500), frame(1 / 1000), frame(1 / 1600)]
    reference = reference_exposure_factor(exposures)

    assert reference == pytest.approx(frame(1 / 1600).exposure_factor)
    full_scale = np.full((3, 8, 8), 255, dtype=np.uint8)
    for e in exposures:
        assert normalise_frame(full_scale, e, reference).max() <= 65535


def test_a_reference_brighter_than_the_darkest_frame_is_refused() -> None:
    """Silently clipping highlights would be worse than failing, so it fails."""
    with pytest.raises(FrameRadiometryError, match="would clip"):
        normalise_frame(
            np.full((3, 4, 4), 200, dtype=np.uint8),
            frame(1 / 1600),
            frame(1 / 500).exposure_factor,
        )


def test_reference_of_an_empty_set_is_an_error_not_a_default() -> None:
    with pytest.raises(FrameRadiometryError):
        reference_exposure_factor([])


def test_channel_ratio_detects_a_white_balance_shift_but_ignores_exposure() -> None:
    """The measurement that decides whether auto white balance actually moved.

    A pure exposure change must not register, or every frame would look like a colour shift
    and the correction would chase the wrong thing.
    """
    rgb = np.stack(
        [
            np.full((64, 64), 0.40, dtype=np.float32),
            np.full((64, 64), 0.50, dtype=np.float32),
            np.full((64, 64), 0.25, dtype=np.float32),
        ]
    )

    assert channel_ratio(rgb) == pytest.approx((0.8, 0.5), rel=1e-5)
    assert channel_ratio(rgb * 0.4) == pytest.approx((0.8, 0.5), rel=1e-5)

    warmer = rgb.copy()
    warmer[0] *= 1.15
    assert channel_ratio(warmer)[0] == pytest.approx(0.92, rel=1e-5)


def test_a_frame_that_is_not_three_band_is_refused() -> None:
    with pytest.raises(FrameRadiometryError, match="3-band"):
        normalise_frame(np.zeros((4, 8, 8), dtype=np.uint8), frame(1 / 1000), 1e-9)
