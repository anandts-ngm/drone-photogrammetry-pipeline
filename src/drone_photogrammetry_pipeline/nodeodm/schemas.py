"""Typed shapes of the NodeODM API.

Field names follow NodeODM's own camelCase rather than being renamed to look Python-ish. A
reader comparing this against the NodeODM documentation should not have to translate, and a
silent rename is exactly how a field gets mismatched during an upstream change.

Verified against NodeODM's docs/index.adoc and index.js on 2026-08-18.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TaskStatusCode(IntEnum):
    """NodeODM's numeric task states."""

    QUEUED = 10
    RUNNING = 20
    FAILED = 30
    COMPLETED = 40
    CANCELED = 50

    @property
    def is_terminal(self) -> bool:
        return self in (TaskStatusCode.FAILED, TaskStatusCode.COMPLETED, TaskStatusCode.CANCELED)

    @property
    def is_success(self) -> bool:
        return self is TaskStatusCode.COMPLETED


class NodeInfo(BaseModel):
    """Response of GET /info. The engine version here is ground truth.

    What the compose file pins and what is actually running can diverge, so the manifest
    records what this endpoint reports rather than what configuration claims.
    """

    model_config = ConfigDict(extra="allow")

    version: str = ""
    engine: str = ""
    engineVersion: str = ""
    totalMemory: int | None = None
    availableMemory: int | None = None
    cpuCores: int | None = None
    maxImages: int | None = None
    maxParallelTasks: int | None = None
    taskQueueCount: int | None = None


class OdmOption(BaseModel):
    """One entry of GET /options — the engine's own account of what it accepts.

    This is the only non-stale source for ODM option names. A copy maintained in this
    repository would eventually disagree with the engine actually running.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    type: str = ""
    value: Any = None
    domain: Any = None
    help: str = ""


class TaskStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: int = 0
    errorMessage: str = ""

    @property
    def status(self) -> TaskStatusCode | None:
        try:
            return TaskStatusCode(self.code)
        except ValueError:
            return None


class TaskInfo(BaseModel):
    """Response of GET /task/{uuid}/info."""

    model_config = ConfigDict(extra="allow")

    uuid: str
    name: str = ""
    dateCreated: int | None = None
    processingTime: int | None = None
    imagesCount: int = 0
    progress: float = 0.0
    status: TaskStatus = Field(default_factory=TaskStatus)
    output: list[str] | None = None

    @property
    def code(self) -> TaskStatusCode | None:
        return self.status.status

    @property
    def is_terminal(self) -> bool:
        code = self.code
        return code is not None and code.is_terminal

    @property
    def is_success(self) -> bool:
        code = self.code
        return code is not None and code.is_success
