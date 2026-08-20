"""Applying a solved radiometric correction to pixels.

A harmonisation solution is one multiplicative gain per block per band. Applying it is
arithmetic, but applying it in the wrong encoding is not a small error: measured on B42, the
survey's largest correction, a gain of 1.42 in light corresponds to 1.16 in display values,
because in sRGB's power-law region `g_encoded = g_linear ** (1/2.4)`. Using the linear figure
directly on DN over-brightens by roughly 23%, a mean of 22 DN across the frame -- larger than
the disagreement the correction exists to remove.

Nothing in the numbers reveals such a swap: the two solutions score the same residual when
each is applied in its own space (6.13% against 6.17% median). So the space is carried on the
solution and honoured here, and a correction can only be built from a solution that declares
one.

Correction and pixel verification are mutually exclusive by construction. `--verify-pixels`
asserts the RGB bands are bit-identical before and after packaging; a correction changes them
deliberately. Rather than quietly weakening the check, asking for both is refused.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from ..colour import dn_to_linear, linear_to_dn
from ..models.harmonisation import BlockGains, HarmonisationSolution, RadiometricSpace

BAND_ORDER = ("red", "green", "blue")


class CorrectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class BlockCorrection:
    """The correction for one block, with the encoding it must be applied in."""

    block_id: str
    gains: tuple[float, float, float]
    space: RadiometricSpace
    offsets: tuple[float, float, float] | None = None
    source_solution: str = ""

    @property
    def is_identity(self) -> bool:
        return all(g == 1.0 for g in self.gains) and self.offsets is None


def correction_for(solution: HarmonisationSolution, block_id: str) -> BlockCorrection | None:
    """Pull one block's correction out of a solution, or None if it was not solved.

    A block absent from the solution is not an error: it may share no overlap with anything.
    Returning None lets the caller package it uncorrected and say so, rather than inventing a
    gain of 1.0 that would be indistinguishable from a measured one.
    """
    entry: BlockGains | None = next((b for b in solution.blocks if b.block_id == block_id), None)
    if entry is None:
        return None

    missing = [name for name in BAND_ORDER if name not in entry.gains]
    if missing:
        raise CorrectionError(
            f"{block_id}: solution is missing gains for {missing}; a partial correction would "
            "shift colour balance rather than brightness"
        )

    offsets = None
    if entry.offsets is not None:
        absent = [name for name in BAND_ORDER if name not in entry.offsets]
        if absent:
            raise CorrectionError(
                f"{block_id}: solution has offsets but not for {absent}; applying some and not "
                "others would be worse than applying none"
            )
        offsets = tuple(float(entry.offsets[name]) for name in BAND_ORDER)

    return BlockCorrection(
        block_id=block_id,
        gains=tuple(float(entry.gains[name]) for name in BAND_ORDER),  # type: ignore[arg-type]
        space=solution.space,
        offsets=offsets,  # type: ignore[arg-type]
        source_solution=f"{solution.project_id} {solution.generated_at:%Y%m%dT%H%M%SZ}",
    )


def _band_values(
    correction: BlockCorrection, band: int, values: NDArray[np.float32], max_value: int
) -> NDArray[np.float32]:
    """One band's corrected values, in the space the solution was solved in."""
    gain = np.float32(correction.gains[band])
    offset = np.float32(correction.offsets[band]) if correction.offsets is not None else None

    if correction.space is RadiometricSpace.LINEAR:
        corrected = dn_to_linear(values) * gain
        if offset is not None:
            corrected = corrected + offset
        return linear_to_dn(np.clip(corrected, 0.0, float(max_value)))

    corrected = values * gain
    if offset is not None:
        corrected = corrected + offset
    return corrected


def correction_table(
    correction: BlockCorrection, band: int, *, max_value: int = 255
) -> NDArray[np.uint8]:
    """Every possible output for one band, precomputed.

    The correction is a per-pixel function of a single 8-bit value, so it has only 256
    distinct results. Building them once per band and indexing turns a transfer-function round
    trip over a gigapixel into a table lookup, with bit-identical output because it is the
    same arithmetic evaluated on the same inputs.

    Rounded rather than truncated: truncation would bias every corrected pixel downwards by
    half a DN, a systematic darkening across the whole survey that the solve never asked for.
    """
    domain = np.arange(max_value + 1, dtype=np.float32)
    corrected = _band_values(correction, band, domain, max_value)
    return np.clip(np.rint(corrected), 0, max_value).astype(np.uint8)


def apply_correction(
    rgb: NDArray[np.number], correction: BlockCorrection, *, max_value: int = 255
) -> NDArray[np.uint8]:
    """Apply a block's gains to a 3-band RGB array, in the solution's own space."""
    if rgb.ndim != 3 or rgb.shape[0] != 3:
        raise CorrectionError(f"expected a 3-band array, got shape {rgb.shape}")

    if rgb.dtype == np.uint8 and max_value == 255:
        return np.stack(
            [correction_table(correction, band)[rgb[band]] for band in range(3)]
        ).astype(np.uint8)

    values = np.asarray(rgb, dtype=np.float32)
    corrected = np.stack(
        [_band_values(correction, band, values[band], max_value) for band in range(3)]
    )
    return np.clip(np.rint(corrected), 0, max_value).astype(np.uint8)
