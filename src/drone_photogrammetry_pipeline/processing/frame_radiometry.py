"""Per-frame radiometric normalisation, applied before reconstruction.

This exists because the disagreement between blocks is not a per-block property. Exposure
measured across sampled L3 frames varies 3.2x (1.68 stops) *within a single flight*, so a
mosaic is a patchwork of frames taken at different exposures. One gain per block cannot
represent that, which is why per-block harmonisation leaves the residual it does. The fix has
to happen while frames are still separate.

Two things make that tractable here, and both were measured rather than assumed:

* **Exposure is exactly recoverable.** Across every sampled frame the aperture is f/2.8 and
  the sensitivity ISO 100; only shutter time varies. Relative exposure between two frames is
  therefore a known ratio, not an estimate. This is the whole reason Tier A is worth doing.
* **Nothing is clipped.** Highlight occupancy in the delivered mosaics is 0.00-0.01%, so the
  encoding is invertible over the range that actually carries data.

What this module deliberately does NOT do is white balance. The cameras ran auto white
balance and DJI does not expose the per-channel gains, so any per-frame correction would have
to be estimated from the frame's own content -- and a grey-world or white-patch estimate
applied per frame would flatten exactly the colour differences lithology is read from. A
frame filled with iron-stained rock is not a frame with a red cast. Residual colour
differences are left for the block-level solve, which constrains them against overlapping
ground rather than against an assumption about the scene.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from ..colour import U8_MAX as _U8_MAX
from ..colour import linear_to_srgb, srgb_to_linear

__all__ = [
    "FrameExposure",
    "FrameRadiometryError",
    "channel_ratio",
    "linear_to_srgb",
    "normalise_frame",
    "reference_exposure_factor",
    "srgb_to_linear",
]

_U16_MAX = 65535.0


class FrameRadiometryError(RuntimeError):
    pass


@dataclass(frozen=True)
class FrameExposure:
    """The exposure a frame was taken at, as read from its EXIF."""

    path: Path
    shutter_seconds: float
    f_number: float
    iso: float

    @property
    def exposure_factor(self) -> float:
        """Relative light collected per unit scene radiance.

        `t * ISO / N^2` is proportional to the exposure the sensor received for a given scene
        radiance, so dividing by it removes the camera's settings and leaves a quantity
        proportional to radiance. Written in full rather than as `t` alone: aperture and ISO
        happen to be constant in this survey, but a profile that silently assumed so would be
        wrong the first time someone flies with auto-ISO.
        """
        if self.shutter_seconds <= 0 or self.f_number <= 0 or self.iso <= 0:
            raise FrameRadiometryError(
                f"{self.path.name}: cannot form an exposure factor from "
                f"shutter={self.shutter_seconds}, f={self.f_number}, iso={self.iso}"
            )
        return self.shutter_seconds * self.iso / (self.f_number**2)


def reference_exposure_factor(exposures: list[FrameExposure]) -> float:
    """The exposure every frame in a set is normalised onto.

    The minimum is chosen so that every scale factor is <= 1. Normalising onto a brighter
    reference would push the shortest-exposure frames above full scale and clip them, which
    would destroy highlight detail that the delivered mosaics still have. Scaling down cannot
    clip, and since the output carries more bits than the input it costs no precision.
    """
    if not exposures:
        raise FrameRadiometryError("no frames given; cannot choose a reference exposure")
    return min(e.exposure_factor for e in exposures)


def normalise_frame(
    rgb_u8: NDArray[np.uint8], exposure: FrameExposure, reference_factor: float
) -> NDArray[np.uint16]:
    """Linearise one frame and put it on the reference exposure.

    Returns 16-bit because the input is 8-bit and is about to be divided by up to the full
    exposure spread; re-quantising that to 8 bits would throw away roughly the 1.7 stops the
    correction is trying to recover.
    """
    if rgb_u8.ndim != 3 or rgb_u8.shape[0] != 3:
        raise FrameRadiometryError(f"expected a 3-band array, got shape {rgb_u8.shape}")

    linear = srgb_to_linear(rgb_u8.astype(np.float32) / _U8_MAX)
    scale = reference_factor / exposure.exposure_factor
    if scale > 1.0:
        raise FrameRadiometryError(
            f"{exposure.path.name}: scale {scale:.3f} exceeds 1 and would clip; the reference "
            "must be the minimum exposure factor of the set"
        )
    scaled: NDArray[np.uint16] = np.clip(linear * scale * _U16_MAX, 0.0, _U16_MAX).astype(np.uint16)
    return scaled


def channel_ratio(rgb: NDArray[np.number], sample_stride: int = 16) -> tuple[float, float]:
    """Median R/G and B/G over a subsample, for detecting white-balance drift between frames.

    Ratios are taken on linear values, where a per-channel white-balance gain is a clean
    multiplier. Comparing this across frames of the same ground says whether auto white
    balance actually moved: if it held steady, the residual colour difference between blocks
    is scene, not camera, and must not be "corrected" away.
    """
    a = np.asarray(rgb, dtype=np.float32)[:, ::sample_stride, ::sample_stride]
    green = a[1]
    usable = green > 0
    if not usable.any():
        return (float("nan"), float("nan"))
    return (
        float(np.median(a[0][usable] / green[usable])),
        float(np.median(a[2][usable] / green[usable])),
    )
