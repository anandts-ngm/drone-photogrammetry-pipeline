"""The space a gain was solved in must travel with the gain.

Measured on B42, the survey's largest correction: the same physical adjustment is a gain of
1.42 in light and 1.16 in display values. Applying one where the other belongs is a mean
error of 22 DN across the frame -- larger than anything the correction removes. The two
solutions score the same residual when each is used correctly, so nothing in the numbers
reveals a swap. Only the recorded space does.
"""

from __future__ import annotations

import numpy as np
import pytest

from drone_photogrammetry_pipeline.colour import dn_to_linear, linear_to_dn
from drone_photogrammetry_pipeline.harmonisation import solve_gains
from drone_photogrammetry_pipeline.models.harmonisation import RadiometricSpace
from drone_photogrammetry_pipeline.models.qa import (
    BandDifference,
    RadiometricOverlapReport,
    RadiometricPairResult,
)


def report(*, linearised: bool) -> RadiometricOverlapReport:
    pair = RadiometricPairResult(
        block_a="A",
        block_b="B",
        overlap_area_ha=10.0,
        sample_count=8,
        sample_pixels=4096,
        patch_metres=4.0,
        bands=[
            BandDifference(
                band=name,
                median_a=100.0,
                median_b=120.0,
                median_difference=20.0,
                relative_difference_pct=18.2,
                robust_normalized_difference_pct=18.2,
            )
            for name in ("red", "green", "blue")
        ],
    )
    return RadiometricOverlapReport(
        project_id="P",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        pair_count=1,
        measured_count=1,
        linearised=linearised,
        pairs=[pair],
    )


def test_the_solution_inherits_the_space_of_its_measurement() -> None:
    assert solve_gains(report(linearised=True)).space is RadiometricSpace.LINEAR
    assert solve_gains(report(linearised=False)).space is RadiometricSpace.ENCODED


def test_an_old_report_without_the_field_is_read_as_encoded() -> None:
    """Reports predating the field were measured on DN; reinterpreting them would corrupt.

    Built by removing the key from a serialised report, which is exactly what the reports
    already on disk from 2026-08-18 look like.
    """
    document = report(linearised=True).model_dump(mode="json")
    del document["linearised"]

    old = RadiometricOverlapReport.model_validate(document)

    assert old.linearised is False
    assert solve_gains(old).space is RadiometricSpace.ENCODED


def test_applying_a_linear_gain_to_encoded_values_is_a_large_error() -> None:
    """Quantifies why the space is recorded rather than inferred.

    A linear gain of 1.42 corresponds to an encoded gain of 1.42**(1/2.4). Using the linear
    figure directly on DN over-brightens by roughly a quarter.
    """
    dn = np.float32(104.0)
    linear_gain = 1.42

    correct = float(linear_to_dn(dn_to_linear(dn) * linear_gain))
    naive = float(dn) * linear_gain

    assert correct == pytest.approx(float(dn) * linear_gain ** (1 / 2.4), rel=0.02)
    assert naive - correct > 20.0
    assert naive / correct == pytest.approx(1.23, abs=0.03)
