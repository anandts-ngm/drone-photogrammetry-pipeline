"""The sRGB transfer function.

The property that matters to the pipeline is the one asserted last: a multiplicative change
in light is *not* a multiplicative change in encoded value near black. That is the whole
reason gains must be solved on linearised numbers.
"""

from __future__ import annotations

import numpy as np
import pytest

from drone_photogrammetry_pipeline.colour import (
    dn_to_linear,
    linear_to_dn,
    linear_to_srgb,
    srgb_to_linear,
)


def test_round_trip_is_exact_across_the_range() -> None:
    values = np.linspace(0.0, 1.0, 4096, dtype=np.float32)
    assert np.allclose(linear_to_srgb(srgb_to_linear(values)), values, atol=1e-5)


def test_dn_round_trip_holds_on_the_0_255_scale() -> None:
    dn = np.arange(0, 256, dtype=np.float32)
    assert np.allclose(linear_to_dn(dn_to_linear(dn)), dn, atol=1e-3)


def test_endpoints_and_mid_grey() -> None:
    assert srgb_to_linear(np.float32(0.0)) == pytest.approx(0.0)
    assert srgb_to_linear(np.float32(1.0)) == pytest.approx(1.0, rel=1e-6)
    # Half display brightness is roughly a fifth of the light; the pipeline's thresholds are
    # read in DN, so this ratio is worth pinning.
    assert float(dn_to_linear(np.float32(128.0))) == pytest.approx(55.0, abs=1.5)


def test_the_toe_is_linear_not_a_power_law() -> None:
    dark = np.float32(0.02)
    assert srgb_to_linear(dark) == pytest.approx(dark / 12.92, rel=1e-6)
    assert srgb_to_linear(dark) != pytest.approx(dark**2.4, rel=1e-3)


def test_a_gain_in_light_is_not_a_gain_in_encoded_value_near_black() -> None:
    """Why `harmonise` must run on linearised numbers.

    Doubling the light doubles the linearised value everywhere, by definition. In encoded
    space the same doubling produces a ratio that drifts with brightness, so a single gain
    fitted on encoded medians cannot be right at both ends of the range at once.
    """
    bright, dark = np.float32(180.0), np.float32(12.0)

    # Linear space: doubling is exactly doubling, at both ends.
    for dn in (bright, dark):
        assert float((dn_to_linear(dn) * 2.0) / dn_to_linear(dn)) == pytest.approx(2.0)

    # Encoded space: the same doubling of light gives two very different DN ratios.
    encoded_bright = float(linear_to_dn(dn_to_linear(bright) * 2.0) / bright)
    encoded_dark = float(linear_to_dn(dn_to_linear(dark) * 2.0) / dark)

    assert encoded_bright == pytest.approx(1.361, abs=0.005)
    assert encoded_dark == pytest.approx(1.726, abs=0.005)

    # A single per-block gain fitted on encoded medians has to pick one of these. Choosing
    # the bright end under-corrects shadow by roughly a quarter, which is the size of the
    # residual that per-block harmonisation cannot remove.
    assert encoded_dark / encoded_bright > 1.25


def test_linearisation_preserves_order() -> None:
    dn = np.array([0, 1, 17, 60, 128, 200, 255], dtype=np.float32)
    linear = dn_to_linear(dn)
    assert np.all(np.diff(linear) > 0)
