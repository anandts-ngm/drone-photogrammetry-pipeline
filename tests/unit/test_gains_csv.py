"""The coefficients CSV must carry everything a correct application needs.

A gain taken from a joint gain-and-offset fit is not a gain-only correction. Writing those
gains without their offsets produces a file that looks usable and is measurably worse than
applying nothing, so these tests exist to keep that from happening again.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from drone_photogrammetry_pipeline.models.harmonisation import (
    BandResidual,
    BlockGains,
    HarmonisationSolution,
)
from drone_photogrammetry_pipeline.reporting.manifest import write_harmonisation_gains_csv

BANDS = ("red", "green", "blue")


def solution(*, with_offsets: bool) -> HarmonisationSolution:
    return HarmonisationSolution(
        project_id="Test",
        generated_at=datetime.now(UTC),
        source_report="synthetic",
        model="gain_offset" if with_offsets else "gain",
        anchor="project_mean",
        weighting="none",
        constraint_count=2,
        block_count=2,
        component_count=1,
        blocks=[
            BlockGains(
                block_id=block,
                gains=dict.fromkeys(BANDS, 1.1),
                offsets=dict.fromkeys(BANDS, -7.5) if with_offsets else None,
                overlap_count=3,
                component=0,
                residual_pct=2.5,
            )
            for block in ("B1", "B2")
        ],
        residuals=[
            BandResidual(
                band=band,
                median_before_pct=8.0,
                median_after_pct=4.0,
                p90_before_pct=20.0,
                p90_after_pct=10.0,
                gain_min=1.0,
                gain_max=1.2,
            )
            for band in BANDS
        ],
    )


def read(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.open(encoding="utf-8")))


def test_a_gain_only_solution_writes_no_offset_columns(tmp_path: Path) -> None:
    rows = read(write_harmonisation_gains_csv(tmp_path / "g.csv", solution(with_offsets=False)))

    assert rows[0]["model"] == "gain"
    assert not any(key.startswith("offset_") for key in rows[0])


def test_a_gain_offset_solution_writes_its_offsets(tmp_path: Path) -> None:
    """Without this, the file silently describes a different correction than the one solved."""
    rows = read(write_harmonisation_gains_csv(tmp_path / "go.csv", solution(with_offsets=True)))

    assert rows[0]["model"] == "gain_offset"
    for band in BANDS:
        assert f"offset_{band}" in rows[0]
        assert float(rows[0][f"offset_{band}"]) == -7.5


def test_every_row_carries_its_own_trust_information(tmp_path: Path) -> None:
    rows = read(write_harmonisation_gains_csv(tmp_path / "g.csv", solution(with_offsets=True)))

    for row in rows:
        assert row["overlap_count"] == "3"
        assert row["residual_pct"] == "2.50"
        assert row["anchor"] == "project_mean"
        assert row["component"] == "0"


def test_gains_are_written_for_every_band(tmp_path: Path) -> None:
    rows = read(write_harmonisation_gains_csv(tmp_path / "g.csv", solution(with_offsets=False)))

    for band in BANDS:
        assert float(rows[0][f"gain_{band}"]) == 1.1
