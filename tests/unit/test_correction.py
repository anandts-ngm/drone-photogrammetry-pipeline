"""Applying a solved correction to pixels.

The test that matters most is the last one: the same solution applied in the wrong space
produces a materially different image, and no property of the output reveals which was
intended. That is the whole reason the space travels with the gains.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from drone_photogrammetry_pipeline.models.harmonisation import (
    BlockGains,
    HarmonisationSolution,
    RadiometricSpace,
)
from drone_photogrammetry_pipeline.packaging.correction import (
    BlockCorrection,
    CorrectionError,
    _band_values,
    apply_correction,
    correction_for,
    correction_table,
)


def solution(
    *,
    space: RadiometricSpace = RadiometricSpace.LINEAR,
    gains: dict[str, float] | None = None,
    offsets: dict[str, float] | None = None,
    block_id: str = "B42",
) -> HarmonisationSolution:
    return HarmonisationSolution(
        project_id="Buduunkhad",
        generated_at=datetime.now(UTC),
        source_report="test",
        space=space,
        anchor="project_mean",
        weighting="none",
        constraint_count=1,
        block_count=1,
        component_count=1,
        blocks=[
            BlockGains(
                block_id=block_id,
                gains=gains or {"red": 1.42, "green": 1.38, "blue": 1.30},
                offsets=offsets,
                overlap_count=5,
            )
        ],
    )


def for_b42(
    *,
    space: RadiometricSpace = RadiometricSpace.LINEAR,
    gains: dict[str, float] | None = None,
) -> BlockCorrection:
    """The correction under test.

    `correction_for` returns None for a block its solution does not cover, which is its own
    test below. Everywhere else the block is present, and carrying the Optional through every
    call site says nothing about the behaviour being checked.
    """
    found = correction_for(solution(space=space, gains=gains), "B42")
    assert found is not None
    return found


def test_a_block_absent_from_the_solution_returns_none_rather_than_an_identity() -> None:
    """An unsolved block must stay distinguishable from one measured to need no change."""
    assert correction_for(solution(), "B99") is None


def test_the_correction_carries_the_space_from_its_solution() -> None:
    assert for_b42(space=RadiometricSpace.LINEAR).space is RadiometricSpace.LINEAR
    assert for_b42(space=RadiometricSpace.ENCODED).space is RadiometricSpace.ENCODED


def test_a_solution_missing_a_band_is_refused() -> None:
    with pytest.raises(CorrectionError, match="missing gains"):
        correction_for(solution(gains={"red": 1.1, "green": 1.1}), "B42")


def test_offsets_present_for_only_some_bands_are_refused() -> None:
    with pytest.raises(CorrectionError, match="offsets but not for"):
        correction_for(
            solution(
                gains={"red": 1.1, "green": 1.1, "blue": 1.1},
                offsets={"red": 2.0, "green": 1.0},
            ),
            "B42",
        )


def test_an_identity_gain_leaves_pixels_untouched() -> None:
    correction = for_b42(gains={"red": 1.0, "green": 1.0, "blue": 1.0})
    pixels = np.arange(3 * 8 * 8, dtype=np.uint8).reshape(3, 8, 8)

    assert np.array_equal(apply_correction(pixels, correction), pixels)


def test_encoded_gain_scales_dn_directly() -> None:
    correction = for_b42(
        space=RadiometricSpace.ENCODED, gains={"red": 1.5, "green": 1.0, "blue": 0.5}
    )
    pixels = np.full((3, 4, 4), 100, dtype=np.uint8)

    out = apply_correction(pixels, correction)

    assert out[0].item(0) == 150
    assert out[1].item(0) == 100
    assert out[2].item(0) == 50


def test_correction_cannot_push_pixels_out_of_range() -> None:
    correction = for_b42(
        space=RadiometricSpace.ENCODED, gains={"red": 4.0, "green": 4.0, "blue": 4.0}
    )
    out = apply_correction(np.full((3, 4, 4), 250, dtype=np.uint8), correction)

    assert out.max() == 255
    assert out.dtype == np.uint8


def test_rounding_is_not_truncation() -> None:
    """Truncating would darken every corrected block by half a DN systematically."""
    correction = for_b42(
        space=RadiometricSpace.ENCODED, gains={"red": 1.009, "green": 1.0, "blue": 1.0}
    )
    # 100 * 1.009 = 100.9 -> rounds to 101, truncates to 100.
    assert apply_correction(np.full((3, 2, 2), 100, dtype=np.uint8), correction)[0].item(0) == 101


@pytest.mark.parametrize("space", [RadiometricSpace.LINEAR, RadiometricSpace.ENCODED])
def test_the_lookup_table_is_the_same_arithmetic_not_an_approximation(
    space: RadiometricSpace,
) -> None:
    """The fast path must be bit-identical, or it is a different correction.

    8-bit input has only 256 possible values per band, so the table is the function evaluated
    exhaustively rather than a sampled approximation of it. This asserts that over the entire
    domain, which is small enough to check completely.
    """
    correction = for_b42(space=space, gains={"red": 0.569, "green": 1.42, "blue": 1.0})

    every_value = np.tile(np.arange(256, dtype=np.uint8), (3, 1, 1))
    from_table = apply_correction(every_value, correction)
    from_float = np.clip(
        np.rint(
            np.stack(
                [
                    _band_values(correction, band, every_value[band].astype(np.float32), 255)
                    for band in range(3)
                ]
            )
        ),
        0,
        255,
    ).astype(np.uint8)

    assert np.array_equal(from_table, from_float)


def test_a_gain_of_one_produces_an_exact_identity_table() -> None:
    """Guards against an off-by-one in the table's domain silently shifting every pixel."""
    correction = for_b42(
        space=RadiometricSpace.LINEAR, gains={"red": 1.0, "green": 1.0, "blue": 1.0}
    )
    assert np.array_equal(correction_table(correction, 0), np.arange(256, dtype=np.uint8))


def test_the_same_gains_in_the_two_spaces_give_materially_different_images() -> None:
    """Why the space is recorded: nothing in the output reveals which was intended.

    Uses B42's real gain of 1.42 on its measured median DN of 104.
    """
    pixels = np.full((3, 16, 16), 104, dtype=np.uint8)
    gains = {"red": 1.42, "green": 1.42, "blue": 1.42}

    linear = apply_correction(pixels, for_b42(space=RadiometricSpace.LINEAR, gains=gains))
    encoded = apply_correction(pixels, for_b42(space=RadiometricSpace.ENCODED, gains=gains))

    assert encoded[0].item(0) == 148
    # The power-law shorthand `g ** (1/2.4)` predicts 120; the exact sRGB round trip gives
    # 123, which is why the code performs the transform rather than approximating it.
    assert linear[0].item(0) == 123
    assert int(encoded[0].item(0)) - int(linear[0].item(0)) == 25
