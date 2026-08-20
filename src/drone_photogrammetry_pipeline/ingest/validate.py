"""Deciding whether a block can be processed, and what may be claimed about the result.

The four-way severity is the whole point. The distinction that matters is between an
expectation that is absent for a good reason and one that is absent without one:

* `REQUIRED_PRESENT` — required and found.
* `OPTIONAL_MISSING`  — optional and absent; recorded, nothing changes.
* `MISSING_ACCEPTABLE` — expected but justifiably absent. The run proceeds, and what QA may
  later claim is narrowed. A block with no check points is not a failed block; it is a block
  nobody independently verified, and the manifest should say so.
* `MISSING_FATAL` — absent, and the run cannot be trusted. Processing stops.

A missing vertical reference is deliberately not fatal. It restricts the run to relative-Z
work rather than blocking it, which is the honest outcome — and it is recorded so that no
downstream step can quietly assume otherwise.
"""

from __future__ import annotations

from ..models.block import Block, ValidatedBlock, ValidationFinding
from ..models.enums import HeightType, ValidationSeverity

MINIMUM_IMAGES = 2


def _finding(name: str, severity: ValidationSeverity, detail: str = "") -> ValidationFinding:
    return ValidationFinding(name=name, severity=severity, detail=detail)


def validate_block(block: Block) -> ValidatedBlock:
    findings: list[ValidationFinding] = []

    if not block.images:
        findings.append(
            _finding(
                "imagery",
                ValidationSeverity.MISSING_FATAL,
                f"no readable images under {block.root}; nothing can be reconstructed",
            )
        )
    elif len(block.images) < MINIMUM_IMAGES:
        findings.append(
            _finding(
                "imagery",
                ValidationSeverity.MISSING_FATAL,
                f"only {len(block.images)} image; photogrammetry needs overlapping images",
            )
        )
    else:
        findings.append(
            _finding("imagery", ValidationSeverity.REQUIRED_PRESENT, f"{len(block.images)} images")
        )

    findings.append(
        _finding(
            "navigation", ValidationSeverity.REQUIRED_PRESENT, f"{len(block.navigation)} files"
        )
        if block.navigation
        else _finding(
            "navigation",
            ValidationSeverity.MISSING_ACCEPTABLE,
            "no navigation files found; georeferencing will rely on image EXIF alone",
        )
    )

    findings.append(
        _finding("control", ValidationSeverity.REQUIRED_PRESENT, f"{len(block.control)} files")
        if block.control
        else _finding(
            "control",
            ValidationSeverity.MISSING_ACCEPTABLE,
            "no ground control; acceptable for an RTK or PPK block, but the adjustment is "
            "then unconstrained by ground measurement",
        )
    )

    findings.append(
        _finding(
            "checkpoints", ValidationSeverity.REQUIRED_PRESENT, f"{len(block.checkpoints)} files"
        )
        if block.checkpoints
        else _finding(
            "checkpoints",
            ValidationSeverity.MISSING_ACCEPTABLE,
            "no independent check points; the product can be made but not independently "
            "verified, and no accuracy may be claimed for it",
        )
    )

    config = block.config
    if config is None:
        findings.append(
            _finding(
                "block.yaml",
                ValidationSeverity.MISSING_ACCEPTABLE,
                "no block.yaml; CRS and vertical reference are undeclared and will not be "
                "guessed from the imagery",
            )
        )
    else:
        findings.append(
            _finding("block.yaml", ValidationSeverity.REQUIRED_PRESENT, str(config.crs))
        )

    height_type = config.vertical.height_type if config else HeightType.UNKNOWN
    suitable_for_absolute_z = height_type is not HeightType.UNKNOWN
    findings.append(
        _finding(
            "vertical_reference",
            ValidationSeverity.REQUIRED_PRESENT,
            f"{height_type.value}"
            + (f", EPSG:{config.vertical.epsg}" if config and config.vertical.epsg else ""),
        )
        if suitable_for_absolute_z
        else _finding(
            "vertical_reference",
            ValidationSeverity.MISSING_ACCEPTABLE,
            "vertical reference undeclared; the run proceeds but is not suitable for "
            "absolute-Z QA, and nothing downstream may claim otherwise",
        )
    )

    findings.append(
        _finding("reference", ValidationSeverity.REQUIRED_PRESENT, f"{len(block.reference)} files")
        if block.reference
        else _finding("reference", ValidationSeverity.OPTIONAL_MISSING, "no reference material")
    )

    return ValidatedBlock(
        block=block, findings=findings, suitable_for_absolute_z=suitable_for_absolute_z
    )
