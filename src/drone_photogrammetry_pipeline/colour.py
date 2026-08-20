"""The sRGB transfer function, and why anything radiometric has to go through it.

Delivered orthophotos are display-referred: the camera applied a non-linear encoding so the
image looks right on a screen. Pixel values are therefore not proportional to the light that
reached the sensor, and the operations this pipeline performs on them -- solving a
multiplicative gain, taking a band ratio, dividing out an exposure -- all mean something
different on encoded numbers than on radiance.

A gain survives the encoding only if the encoding is a pure power law: for `y = x**(1/g)`,
scaling `x` by `k` scales `y` by `k**(1/g)`, still a scale. sRGB is *not* a pure power law.
It has a linear segment below 0.04045, and real camera pipelines add an S-shaped tone curve
on top. In the toe a scale factor in radiance is not a scale factor in the encoded value, so
a gain fitted on encoded numbers is systematically wrong in shadow -- which on this survey is
the shaded side of every rock face, exactly where lithology is hardest to read.

This module is deliberately dependency-free and sits at package root so that both the QA
measurement path and the processing path can use it without either importing the other.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

# IEC 61966-2-1.
_THRESHOLD = 0.04045
_SLOPE = 12.92
_OFFSET = 0.055
_GAMMA = 2.4

U8_MAX = 255.0


def srgb_to_linear(encoded: NDArray[np.floating] | float) -> NDArray[np.float32]:
    """Map display-referred values in [0, 1] to linear values in [0, 1]."""
    x = np.asarray(encoded, dtype=np.float32)
    return np.where(
        x <= _THRESHOLD, x / _SLOPE, ((x + _OFFSET) / (1.0 + _OFFSET)) ** _GAMMA
    ).astype(np.float32)


def linear_to_srgb(linear: NDArray[np.floating] | float) -> NDArray[np.float32]:
    """Forward sRGB transfer function; the exact inverse of `srgb_to_linear`."""
    x = np.clip(np.asarray(linear, dtype=np.float32), 0.0, 1.0)
    return np.where(
        x <= _THRESHOLD / _SLOPE,
        x * _SLOPE,
        (1.0 + _OFFSET) * np.power(x, 1.0 / _GAMMA) - _OFFSET,
    ).astype(np.float32)


def dn_to_linear(dn: NDArray[np.number]) -> NDArray[np.float32]:
    """Linearise 8-bit DN, returning values on the same 0-255 scale.

    The scale is kept so that linearised numbers stay comparable in magnitude to the DN they
    came from and thresholds expressed in DN remain meaningful. Note the mapping is not
    order-preserving in magnitude terms: mid-grey DN 128 linearises to about 55 on this
    scale, because half of display brightness is roughly a fifth of the light.
    """
    return srgb_to_linear(np.asarray(dn, dtype=np.float32) / U8_MAX) * U8_MAX


def linear_to_dn(linear: NDArray[np.floating]) -> NDArray[np.float32]:
    """Inverse of `dn_to_linear`, still on the 0-255 scale and not yet rounded."""
    return linear_to_srgb(np.asarray(linear, dtype=np.float32) / U8_MAX) * U8_MAX
