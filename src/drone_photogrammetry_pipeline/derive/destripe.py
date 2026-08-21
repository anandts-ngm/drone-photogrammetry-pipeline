"""Removing flight-strip banding, for viewing products only.

The delivered mosaics carry banding along the flight lines: measured across the 79 Buduunkhad
masters it is 2.7% of scene brightness at the median and present in 99% of blocks. It comes
from Terra's strip blending, not from this pipeline, and at contact-sheet scale it is the most
visually objectionable thing left in the imagery.

**This is deliberately not applied to masters.** Stripes run along flight lines; bedding
traces, faults and dykes are also linear, and no directional filter can tell them apart. In an
orthophoto collected to map linear structure, attenuating real lineaments to remove a 2.7%
artefact is a bad trade. A preview is the opposite case: it is lossy and resampled already,
nothing is measured from it, and the master keeps every lineament exactly.

Method, and the safety property it buys:

A stripe is coherent along its whole length and abrupt across it. Averaging the image along
one axis therefore keeps stripes and averages terrain away; subtracting the *broad trend* from
that profile leaves only narrow cross-track ripple, which is the stripe signal. Applying that
as a correction removes exactly the component that is coherent over the full length of the
image.

What survives, by construction:

* a broad brightness gradient -- it stays in the trend and is never subtracted
* a lineament that does not span the image
* a lineament at an angle to either axis
* anything that is not coherent along a full row or column

Corrections are multiplicative and computed in linear light, because a strip differs from its
neighbour by exposure, and exposure is a gain rather than an offset.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from ..colour import dn_to_linear, linear_to_dn

# Wider than the spacing between flight lines, so the trend cannot follow the stripes it is
# meant to expose. At preview scale a block is ~2048 px across ~1400 m, and lines sit 50-100 m
# apart, so the ripple to remove has a period of roughly 70-140 px.
DEFAULT_TREND_WINDOW = 401

# Below this fraction of valid pixels a row or column has too little ground to estimate from,
# and its correction is left at 1.0 rather than invented from a handful of pixels.
_MIN_VALID_FRACTION = 0.25

# A single strip should never be moved by more than this. A larger correction means the
# profile is being driven by scene content rather than by banding, and clamping keeps a
# mis-estimate from becoming a visible artefact of its own.
_MAX_GAIN_STEP = 0.25


@dataclass(frozen=True)
class DestripeResult:
    """How much banding was removed, so the effect can be reported rather than assumed."""

    ripple_before_pct: float
    ripple_after_pct: float

    @property
    def reduction_pct(self) -> float:
        if self.ripple_before_pct <= 0:
            return 0.0
        return 100.0 * (1.0 - self.ripple_after_pct / self.ripple_before_pct)


def _smooth(profile: NDArray[np.float64], window: int) -> NDArray[np.float64]:
    """Moving average with edge padding, wide enough to ignore the stripes."""
    window = max(3, min(window, profile.size))
    if window % 2 == 0:
        window += 1
    padded = np.pad(profile, window // 2, mode="edge")
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(padded, kernel, mode="valid")[: profile.size]


def _axis_gain(
    linear: NDArray[np.float64], valid: NDArray[np.bool_], axis: int, window: int
) -> NDArray[np.float64]:
    """Per-row or per-column multiplicative correction, as a 1-D array of gains."""
    other = 1 - axis
    counts = valid.sum(axis=other)
    totals = np.where(valid, linear, 0.0).sum(axis=other)

    enough = counts >= max(1, int(_MIN_VALID_FRACTION * valid.shape[other]))
    profile = np.ones(counts.size, dtype=np.float64)
    np.divide(totals, np.maximum(counts, 1), out=profile, where=enough)

    # Interpolate across positions with too little ground, so a thin edge does not create a
    # step in the correction itself.
    if not enough.all():
        index = np.arange(profile.size)
        if enough.any():
            profile = np.interp(index, index[enough], profile[enough])
        else:
            return np.ones(profile.size, dtype=np.float64)

    trend = _smooth(profile, window)
    with np.errstate(divide="ignore", invalid="ignore"):
        gain = np.where(profile > 0, trend / profile, 1.0)
    return np.clip(np.nan_to_num(gain, nan=1.0), 1.0 - _MAX_GAIN_STEP, 1.0 + _MAX_GAIN_STEP)


def _ripple_pct(linear: NDArray[np.float64], valid: NDArray[np.bool_], window: int) -> float:
    """Cross-track ripple amplitude, as a percentage of mean brightness, worse axis."""
    worst = 0.0
    for axis in (0, 1):
        other = 1 - axis
        counts = valid.sum(axis=other)
        keep = counts >= max(1, int(_MIN_VALID_FRACTION * valid.shape[other]))
        if keep.sum() < 8:
            continue
        profile = np.where(valid, linear, 0.0).sum(axis=other)[keep] / counts[keep]
        mean = float(profile.mean())
        if mean <= 0:
            continue
        worst = max(worst, 100.0 * float((profile - _smooth(profile, window)).std()) / mean)
    return worst


def destripe(
    rgb: NDArray[np.number],
    alpha: NDArray[np.number],
    *,
    window: int = DEFAULT_TREND_WINDOW,
) -> tuple[NDArray[np.uint8], DestripeResult]:
    """Remove full-length coherent banding from a rendered block.

    Both axes are corrected in turn: flight direction varies between blocks -- 42 of the 79
    Buduunkhad blocks band vertically and 37 horizontally -- and an axis with no banding gets a
    correction of essentially 1.0, so applying both is safe and avoids having to detect which.
    """
    values = np.asarray(rgb, dtype=np.float64)
    if values.ndim != 3 or values.shape[0] != 3:
        raise ValueError(f"expected a 3-band array, got shape {values.shape}")
    valid = np.asarray(alpha) > 250

    linear = dn_to_linear(values.astype(np.float32)).astype(np.float64)
    luminance = linear.mean(axis=0)
    before = _ripple_pct(luminance, valid, window)

    # One gain per row and per column, from luminance, applied to every band. Estimating per
    # band would let the correction shift colour balance, which is scene information rather
    # than banding.
    for axis in (0, 1):
        gain = _axis_gain(linear.mean(axis=0), valid, axis, window)
        shape = (gain.size, 1) if axis == 0 else (1, gain.size)
        linear *= gain.reshape(shape)[None, :, :]

    after = _ripple_pct(linear.mean(axis=0), valid, window)
    corrected = linear_to_dn(np.clip(linear, 0.0, 255.0).astype(np.float32))
    return (
        np.clip(np.rint(corrected), 0, 255).astype(np.uint8),
        DestripeResult(ripple_before_pct=before, ripple_after_pct=after),
    )
