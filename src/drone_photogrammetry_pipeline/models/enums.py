"""Vocabulary shared by every model.

Kept in a leaf module so that block, profile, manifest and qa models can all refer to the
same values without importing one another.
"""

from __future__ import annotations

from enum import StrEnum


class SensorId(StrEnum):
    P1_35 = "P1_35"
    P1_50 = "P1_50"
    L2_RGB = "L2_RGB"
    L3_RGB = "L3_RGB"


class SourceType(StrEnum):
    """Where the orthophoto that was packaged came from."""

    ODM = "ODM"
    DJI_TERRA = "DJI_TERRA"


class ProcessingEngine(StrEnum):
    ODM = "ODM"
    DJI_TERRA = "DJI_TERRA"


class WorkflowStatus(StrEnum):
    """Progress of the engine and the pipeline. Says nothing about product approval."""

    VALIDATION_PENDING = "VALIDATION_PENDING"
    VALIDATED = "VALIDATED"
    PROCESSING_PENDING = "PROCESSING_PENDING"
    PROCESSING = "PROCESSING"
    PROCESSING_COMPLETE = "PROCESSING_COMPLETE"
    PACKAGING = "PACKAGING"
    PACKAGED = "PACKAGED"
    QA_PENDING = "QA_PENDING"
    QA_RUNNING = "QA_RUNNING"
    QA_COMPLETE = "QA_COMPLETE"
    FAILED = "FAILED"


class GateStatus(StrEnum):
    """Approval state of the product. Only ever set by QA or by explicit promotion."""

    NOT_EVALUATED = "NOT_EVALUATED"
    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"
    MASTER = "MASTER"


class HeightType(StrEnum):
    """How a Z value is referenced.

    NORMAL and ORTHOMETRIC are kept apart on purpose. Normal heights are measured above the
    quasigeoid and orthometric heights above the geoid; they are different surfaces, and the
    Baltic 1977 system used by the Mongolian deliveries is explicitly normal, not
    orthometric. Folding one into the other would be exactly the silent mixing of vertical
    references this project forbids.
    """

    ELLIPSOIDAL = "ELLIPSOIDAL"
    ORTHOMETRIC = "ORTHOMETRIC"
    NORMAL = "NORMAL"
    LOCAL_MINE = "LOCAL_MINE"
    UNKNOWN = "UNKNOWN"


class AlphaProvenance(StrEnum):
    """How the master's validity mask was obtained. Recorded for every packaged product."""

    PASSTHROUGH = "passthrough"
    RETAGGED = "retagged"
    FROM_MASK = "from_mask"
    FROM_NODATA = "from_nodata"


class CheckOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"
    SKIPPED = "SKIPPED"


class ValidationSeverity(StrEnum):
    """The four-way outcome required of block validation.

    The distinction that matters is the last two: an expectation that is absent for a good
    reason restricts what QA can later claim, whereas one that is absent without a reason
    stops the run.
    """

    REQUIRED_PRESENT = "REQUIRED_PRESENT"
    OPTIONAL_MISSING = "OPTIONAL_MISSING"
    MISSING_ACCEPTABLE = "MISSING_ACCEPTABLE"
    MISSING_FATAL = "MISSING_FATAL"
