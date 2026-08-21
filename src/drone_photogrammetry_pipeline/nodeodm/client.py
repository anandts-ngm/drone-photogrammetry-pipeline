"""The only module that speaks HTTP to NodeODM.

Design points that matter, and why:

* **Chunked upload, always.** A P1 block is hundreds of 25 MB images; a single multipart POST
  of several gigabytes fails badly and restarts from zero. The init / upload / commit flow
  sends images in batches, so a failed batch costs one batch.
* **Retries only where retrying is safe.** Connection errors and 5xx responses are transient
  and are retried with backoff. A 4xx is the server saying the request was wrong, and
  repeating it just asks the same wrong question again.
* **The engine reports its own version.** What a compose file pins and what is actually
  running can differ. `info()` is what the manifest records.
* **Only `all.zip` can be downloaded.** NodeODM's `getAssetsArchivePath` rejects every other
  asset name (verified in libs/Task.js), so selective retrieval is done by restricting what
  goes *into* the archive with the `outputs` parameter at task creation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ..log import get_logger
from .schemas import NodeInfo, OdmOption, TaskInfo

logger = get_logger("nodeodm.client")

DEFAULT_TIMEOUT = 60.0
UPLOAD_TIMEOUT = 900.0
DEFAULT_BATCH_SIZE = 20
ARCHIVE_ASSET = "all.zip"

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class NodeODMError(RuntimeError):
    """NodeODM could not do what was asked."""


class TaskFailedError(NodeODMError):
    pass


@dataclass(frozen=True)
class TaskRequest:
    """What to process and how.

    `options` are ODM option names and values passed straight through. This repository does
    not maintain its own copy of ODM's option schema — a stale copy would silently disagree
    with the engine actually running — so they are validated against `/options` instead.
    """

    name: str
    images: Sequence[Path]
    options: dict[str, Any] = field(default_factory=dict)

    # Paths, relative to the project directory, to include in all.zip. Restricting this is
    # the only way to avoid downloading every asset ODM produced.
    outputs: Sequence[str] | None = None
    webhook: str | None = None


class NodeODMClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = 4,
        backoff_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._client = httpx.Client(
            base_url=self.base_url, timeout=timeout, transport=transport, follow_redirects=True
        )

    def __enter__(self) -> NodeODMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---- plumbing -------------------------------------------------------------------

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(extra or {})
        if self.token:
            params["token"] = self.token
        return params

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.TransportError as error:
                last = error
                logger.warning(
                    "nodeodm transport error",
                    extra={"path": path, "attempt": attempt, "error": str(error)},
                )
            else:
                if response.status_code not in _RETRYABLE_STATUS:
                    if response.status_code >= 400:
                        raise NodeODMError(
                            f"{method} {path} returned {response.status_code}: "
                            f"{response.text[:300]}"
                        )
                    return response
                last = NodeODMError(f"{method} {path} returned {response.status_code}")
                logger.warning(
                    "nodeodm retryable status",
                    extra={"path": path, "attempt": attempt, "status": response.status_code},
                )

            if attempt < self.max_attempts:
                time.sleep(self.backoff_seconds * (2 ** (attempt - 1)))

        raise NodeODMError(
            f"{method} {path} failed after {self.max_attempts} attempts: {last}"
        ) from last

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Decode a response, honouring NodeODM's in-body error convention.

        NodeODM answers some failures with HTTP 200 and `{"error": "..."}` rather than a 4xx —
        asking about an unknown task uuid does exactly that. Without this check the error
        text reaches the model validator and surfaces as a confusing schema failure instead
        of the message NodeODM actually sent.
        """
        payload = self._request(method, path, **kwargs).json()
        if isinstance(payload, dict) and payload.get("error"):
            raise NodeODMError(f"{method} {path}: {payload['error']}")
        return payload

    # ---- server ---------------------------------------------------------------------

    def info(self) -> NodeInfo:
        return NodeInfo.model_validate(self._json("GET", "/info", params=self._params()))

    def options(self) -> list[OdmOption]:
        payload = self._json("GET", "/options", params=self._params())
        return [OdmOption.model_validate(item) for item in payload]

    def health(self) -> bool:
        """Whether the node answers at all. Never raises, so a caller can branch on it."""
        try:
            self.info()
        except NodeODMError:
            return False
        return True

    def unknown_options(self, wanted: dict[str, Any]) -> list[str]:
        """Option names this engine does not recognise.

        Checked against the running engine rather than a table in this repository, because
        only the engine knows what it accepts.
        """
        known = {option.name for option in self.options()}
        return sorted(name for name in wanted if name.lstrip("-") not in known)

    # ---- task creation --------------------------------------------------------------

    @staticmethod
    def _encode_options(options: dict[str, Any]) -> str:
        """NodeODM expects [{"name": ..., "value": ...}, ...] as a JSON string."""
        return json.dumps(
            [{"name": name.lstrip("-"), "value": value} for name, value in options.items()]
        )

    def create_task(self, request: TaskRequest, *, batch_size: int = DEFAULT_BATCH_SIZE) -> str:
        missing = [image for image in request.images if not image.is_file()]
        if missing:
            raise NodeODMError(f"{len(missing)} image(s) do not exist, e.g. {missing[0]}")
        if not request.images:
            raise NodeODMError("a task needs at least one image")

        fields: dict[str, str] = {
            "name": request.name,
            "options": self._encode_options(request.options),
        }
        if request.outputs is not None:
            fields["outputs"] = json.dumps(list(request.outputs))
        if request.webhook:
            fields["webhook"] = request.webhook

        # Multipart, not form-encoded. `/task/new/init` is routed through `multer().none()`,
        # which parses multipart/form-data only -- the engine's own urlencoded parser is wired
        # to /task/cancel, /task/remove and /task/restart but not to this route. A form-encoded
        # body is therefore discarded in full, silently and with a 200: every task created that
        # way ran with the engine's defaults, no name, and none of the profile's options. Sending
        # each field as a nameless multipart part is what httpx offers for text-only multipart.
        uuid = str(
            self._json(
                "POST",
                "/task/new/init",
                params=self._params(),
                files=[(key, (None, value)) for key, value in fields.items()],
            ).get("uuid", "")
        )
        if not uuid:
            raise NodeODMError("NodeODM did not return a task uuid from /task/new/init")
        # `task_name`, not `name`: `name` is a reserved LogRecord attribute and passing it in
        # `extra` makes logging raise. See the note in log.py.
        logger.info(
            "nodeodm task initialised",
            extra={"uuid": uuid, "images": len(request.images), "task_name": request.name},
        )

        uploaded = 0
        for batch in _batched(request.images, batch_size):
            files = [("images", (path.name, path.read_bytes())) for path in batch]
            self._request(
                "POST",
                f"/task/new/upload/{uuid}",
                params=self._params(),
                files=files,
                timeout=UPLOAD_TIMEOUT,
            )
            uploaded += len(batch)
            logger.info(
                "nodeodm upload progress",
                extra={"uuid": uuid, "uploaded": uploaded, "total": len(request.images)},
            )

        self._request("POST", f"/task/new/commit/{uuid}", params=self._params())
        logger.info("nodeodm task committed", extra={"uuid": uuid})
        self._confirm_options(uuid, request.options)
        return uuid

    def _confirm_options(self, uuid: str, wanted: dict[str, Any]) -> None:
        """Read back what the engine recorded, and refuse a task that lost its options.

        Validating option *names* against `/options` was not enough: it proves the engine knows
        the option, not that the option arrived. A form-encoded body was accepted with a 200 and
        discarded, so tasks ran under the engine's defaults while the manifest recorded a profile
        that had never been applied. Nothing downstream could have detected that from the
        product.

        The engine adds defaults of its own, so this asserts that what was sent is present, not
        that the two sets are equal.
        """
        if not wanted:
            return
        recorded = {
            str(option.get("name")): option.get("value")
            for option in (self.task_info(uuid).options or [])
        }
        missing = [
            f"{name}={value!r} (engine has {recorded.get(name, 'nothing')!r})"
            for name, value in ((key.lstrip("-"), value) for key, value in wanted.items())
            if name not in recorded or not _same_option(recorded[name], value)
        ]
        if missing:
            raise NodeODMError(
                f"the engine did not record {len(missing)} of the {len(wanted)} options sent: "
                f"{'; '.join(missing)}. The task was created but would run with different "
                "settings than the profile asks for, so it is not usable evidence"
            )

    # ---- task lifecycle -------------------------------------------------------------

    def task_info(self, uuid: str, *, with_output_from: int | None = None) -> TaskInfo:
        extra: dict[str, Any] = {}
        if with_output_from is not None:
            extra["with_output"] = with_output_from
        payload = self._json("GET", f"/task/{uuid}/info", params=self._params(extra))
        return TaskInfo.model_validate(payload)

    def task_output(self, uuid: str, *, line: int = 0) -> list[str]:
        payload = self._json("GET", f"/task/{uuid}/output", params=self._params({"line": line}))
        return [str(entry) for entry in payload] if isinstance(payload, list) else []

    def wait(
        self,
        uuid: str,
        *,
        poll_seconds: float = 10.0,
        on_progress: Any = None,
        timeout_seconds: float | None = None,
    ) -> TaskInfo:
        """Block until the task reaches a terminal state.

        Returns the final info for any terminal state, including failure. Deciding what a
        failure means belongs to the caller, not here.
        """
        started = time.monotonic()
        while True:
            info = self.task_info(uuid)
            if on_progress is not None:
                on_progress(info)
            if info.is_terminal:
                return info
            if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
                raise NodeODMError(
                    f"task {uuid} did not finish within {timeout_seconds:.0f}s "
                    f"(last progress {info.progress:.0f}%)"
                )
            time.sleep(poll_seconds)

    def download_archive(self, uuid: str, destination: Path) -> Path:
        """Download all.zip, the only asset NodeODM will serve.

        Streamed to disk: these archives are gigabytes and must not be held in memory.
        Written to a temporary name first so an interrupted download cannot be mistaken for
        a complete one.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".partial")
        with self._client.stream(
            "GET",
            f"/task/{uuid}/download/{ARCHIVE_ASSET}",
            params=self._params(),
            timeout=UPLOAD_TIMEOUT,
        ) as response:
            if response.status_code >= 400:
                raise NodeODMError(
                    f"downloading {ARCHIVE_ASSET} for {uuid} returned {response.status_code}"
                )
            with partial.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    handle.write(chunk)
        partial.replace(destination)
        logger.info(
            "nodeodm archive downloaded",
            extra={"uuid": uuid, "bytes": destination.stat().st_size},
        )
        return destination

    def cancel(self, uuid: str) -> None:
        self._request("POST", "/task/cancel", params=self._params(), data={"uuid": uuid})

    def remove(self, uuid: str) -> None:
        self._request("POST", "/task/remove", params=self._params(), data={"uuid": uuid})

    def restart(self, uuid: str, options: dict[str, Any] | None = None) -> None:
        data: dict[str, Any] = {"uuid": uuid}
        if options is not None:
            data["options"] = self._encode_options(options)
        self._request("POST", "/task/restart", params=self._params(), data=data)


def _same_option(recorded: Any, sent: Any) -> bool:
    """Whether the engine's value for an option matches what was sent.

    Compared loosely on purpose: NodeODM round-trips values through JSON and a form, so a
    boolean can come back as the string "true" and a number as "8". Being strict here would
    reject working tasks, which is the opposite of the point.
    """
    if isinstance(sent, bool):
        truthy = {"true", "1"} if sent else {"false", "0"}
        return str(recorded).strip().lower() in truthy
    # `bool` is an `int`, so the check above has already claimed it.
    if isinstance(sent, int | float):
        try:
            return float(recorded) == float(sent)
        except (TypeError, ValueError):
            return False
    return str(recorded) == str(sent)


def _batched(items: Sequence[Path], size: int) -> Iterator[Sequence[Path]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
