"""NodeODM client tests.

Mocked at the httpx transport layer, so both halves are under test: the requests the client
builds, and how it parses what comes back. No container, no reconstruction, no network.

The recorded shapes come from NodeODM's own docs/index.adoc and index.js, verified
2026-08-18. If NodeODM changes, these fixtures are what should be updated first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from drone_photogrammetry_pipeline.nodeodm.client import (
    NodeODMClient,
    NodeODMError,
    TaskRequest,
)
from drone_photogrammetry_pipeline.nodeodm.schemas import TaskStatusCode


class Recorder:
    """Serves canned responses and remembers what was asked."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for path, response in self.routes.items():
            if request.url.path == path or request.url.path.startswith(path):
                built = response(request) if callable(response) else response
                assert isinstance(built, httpx.Response)
                return built
        return httpx.Response(404, json={"error": f"no route for {request.url.path}"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [call.url.path for call in self.calls]


INFO = {
    "version": "2.2.3",
    "engine": "odm",
    "engineVersion": "3.5.6",
    "cpuCores": 32,
    "maxImages": None,
    "taskQueueCount": 0,
}


def client(recorder: Recorder, **kwargs: Any) -> NodeODMClient:
    return NodeODMClient(
        "http://node:3000", transport=recorder.transport(), backoff_seconds=0.0, **kwargs
    )


def images(tmp_path: Path, count: int = 3) -> list[Path]:
    made = []
    for index in range(count):
        path = tmp_path / f"DJI_{index:04d}.JPG"
        path.write_bytes(b"not-a-real-jpeg")
        made.append(path)
    return made


def test_info_reports_the_engine_version_the_node_is_actually_running() -> None:
    """The manifest records this, not what a compose file claims."""
    recorder = Recorder({"/info": httpx.Response(200, json=INFO)})

    info = client(recorder).info()

    assert info.version == "2.2.3"
    assert info.engineVersion == "3.5.6"
    assert info.cpuCores == 32


def test_health_is_false_rather_than_raising_when_the_node_is_down() -> None:
    recorder = Recorder({"/info": httpx.Response(500, text="boom")})
    assert client(recorder).health() is False


def test_the_auth_token_is_sent_as_a_query_parameter() -> None:
    recorder = Recorder({"/info": httpx.Response(200, json=INFO)})

    client(recorder, token="secret").info()

    assert recorder.calls[0].url.params["token"] == "secret"


def test_no_token_parameter_is_sent_when_none_is_configured() -> None:
    recorder = Recorder({"/info": httpx.Response(200, json=INFO)})
    client(recorder).info()
    assert "token" not in recorder.calls[0].url.params


def test_option_names_are_checked_against_the_running_engine() -> None:
    """The engine is the only non-stale source for what ODM accepts."""
    recorder = Recorder(
        {
            "/options": httpx.Response(
                200, json=[{"name": "dsm", "type": "bool"}, {"name": "orthophoto-resolution"}]
            )
        }
    )

    unknown = client(recorder).unknown_options(
        {"dsm": True, "orthophoto-resolution": 2, "invented-option": 1}
    )

    assert unknown == ["invented-option"]


def new_task_routes(uuid: str = "abc-123") -> dict[str, Any]:
    return {
        "/task/new/init": httpx.Response(200, json={"uuid": uuid}),
        "/task/new/upload": httpx.Response(200, json={"success": True}),
        "/task/new/commit": httpx.Response(200, json={"uuid": uuid}),
    }


def test_a_task_is_created_through_the_chunked_upload_flow(tmp_path: Path) -> None:
    """Single-POST upload of a multi-gigabyte block restarts from zero on any failure."""
    recorder = Recorder(new_task_routes())

    uuid = client(recorder).create_task(
        TaskRequest(name="B064", images=images(tmp_path, 5)), batch_size=2
    )

    assert uuid == "abc-123"
    paths = recorder.paths()
    assert paths[0] == "/task/new/init"
    assert paths[-1].startswith("/task/new/commit")
    assert sum(1 for p in paths if p.startswith("/task/new/upload")) == 3


def test_options_are_encoded_in_the_shape_nodeodm_expects(tmp_path: Path) -> None:
    recorder = Recorder(new_task_routes())

    client(recorder).create_task(
        TaskRequest(name="B064", images=images(tmp_path, 1), options={"dsm": True, "--crop": 0})
    )

    form = parse_qs(recorder.calls[0].content.decode())
    encoded = json.loads(form["options"][0])
    assert {"name": "dsm", "value": True} in encoded
    # A leading double dash is stripped: profiles may write either form.
    assert {"name": "crop", "value": 0} in encoded


def test_outputs_restricts_what_the_archive_will_contain(tmp_path: Path) -> None:
    """The only lever against downloading every asset ODM produced."""
    recorder = Recorder(new_task_routes())

    client(recorder).create_task(
        TaskRequest(
            name="B064",
            images=images(tmp_path, 1),
            outputs=["odm_orthophoto/odm_orthophoto.tif", "odm_dem/dsm.tif"],
        )
    )

    assert "outputs" in recorder.calls[0].content.decode()


def test_a_missing_image_is_refused_before_anything_is_uploaded(tmp_path: Path) -> None:
    recorder = Recorder(new_task_routes())
    files = [*images(tmp_path, 2), tmp_path / "gone.JPG"]

    with pytest.raises(NodeODMError, match="do not exist"):
        client(recorder).create_task(TaskRequest(name="B064", images=files))

    assert recorder.calls == []


def test_an_empty_task_is_refused(tmp_path: Path) -> None:
    recorder = Recorder(new_task_routes())
    with pytest.raises(NodeODMError, match="at least one image"):
        client(recorder).create_task(TaskRequest(name="B064", images=[]))


def test_task_status_codes_are_interpreted(tmp_path: Path) -> None:
    recorder = Recorder(
        {
            "/task/": httpx.Response(
                200, json={"uuid": "u", "progress": 100.0, "status": {"code": 40}}
            )
        }
    )

    info = client(recorder).task_info("u")

    assert info.code is TaskStatusCode.COMPLETED
    assert info.is_terminal and info.is_success


def test_a_failed_task_is_terminal_but_not_successful() -> None:
    recorder = Recorder(
        {
            "/task/": httpx.Response(
                200, json={"uuid": "u", "status": {"code": 30, "errorMessage": "no images"}}
            )
        }
    )

    info = client(recorder).task_info("u")

    assert info.is_terminal
    assert not info.is_success
    assert info.status.errorMessage == "no images"


def test_wait_polls_until_a_terminal_state_and_reports_progress() -> None:
    states = iter(
        [
            {"uuid": "u", "progress": 0.0, "status": {"code": 10}},
            {"uuid": "u", "progress": 45.0, "status": {"code": 20}},
            {"uuid": "u", "progress": 100.0, "status": {"code": 40}},
        ]
    )
    recorder = Recorder({"/task/": lambda request: httpx.Response(200, json=next(states))})
    seen: list[float] = []

    info = client(recorder).wait(
        "u", poll_seconds=0.0, on_progress=lambda i: seen.append(i.progress)
    )

    assert info.is_success
    assert seen == [0.0, 45.0, 100.0]


def test_wait_returns_a_failure_rather_than_raising() -> None:
    """Deciding what a failure means belongs to the caller."""
    recorder = Recorder({"/task/": httpx.Response(200, json={"uuid": "u", "status": {"code": 30}})})

    info = client(recorder).wait("u", poll_seconds=0.0)

    assert info.is_terminal and not info.is_success


def test_wait_gives_up_if_the_task_never_finishes() -> None:
    recorder = Recorder({"/task/": httpx.Response(200, json={"uuid": "u", "status": {"code": 20}})})

    with pytest.raises(NodeODMError, match="did not finish"):
        client(recorder).wait("u", poll_seconds=0.0, timeout_seconds=-1.0)


def test_the_archive_is_streamed_to_disk_and_only_named_on_success(tmp_path: Path) -> None:
    payload = b"zip-bytes" * 1000
    recorder = Recorder({"/task/": httpx.Response(200, content=payload)})
    destination = tmp_path / "all.zip"

    client(recorder).download_archive("u", destination)

    assert destination.read_bytes() == payload
    assert not list(tmp_path.glob("*.partial"))


def test_a_failed_download_leaves_no_file_that_looks_complete(tmp_path: Path) -> None:
    recorder = Recorder({"/task/": httpx.Response(404, text="gone")})
    destination = tmp_path / "all.zip"

    with pytest.raises(NodeODMError):
        client(recorder).download_archive("u", destination)

    assert not destination.exists()


def test_transient_failures_are_retried() -> None:
    responses = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200, json=INFO)])
    recorder = Recorder({"/info": lambda request: next(responses)})

    info = client(recorder).info()

    assert info.engineVersion == "3.5.6"
    assert len(recorder.calls) == 3


def test_a_client_error_is_not_retried() -> None:
    """A 400 means the request was wrong; repeating it asks the same wrong question."""
    recorder = Recorder({"/info": httpx.Response(400, text="bad request")})

    with pytest.raises(NodeODMError, match="400"):
        client(recorder).info()

    assert len(recorder.calls) == 1


def test_retries_are_bounded() -> None:
    recorder = Recorder({"/info": httpx.Response(503)})

    with pytest.raises(NodeODMError, match="after 3 attempts"):
        client(recorder, max_attempts=3).info()

    assert len(recorder.calls) == 3


def test_an_error_reported_with_http_200_is_still_an_error() -> None:
    """NodeODM answers an unknown task uuid with 200 and an `error` key, not a 4xx.

    Without this the error text reaches the model validator and surfaces as a confusing
    schema failure instead of the message NodeODM actually sent. Found by the live tests.
    """
    recorder = Recorder({"/task/": httpx.Response(200, json={"error": "00000000-0000 not found"})})

    with pytest.raises(NodeODMError, match="not found"):
        client(recorder).task_info("00000000-0000")


def test_an_in_body_error_is_caught_on_task_creation(tmp_path: Path) -> None:
    recorder = Recorder({"/task/new/init": httpx.Response(200, json={"error": "too many images"})})

    with pytest.raises(NodeODMError, match="too many images"):
        client(recorder).create_task(TaskRequest(name="B064", images=images(tmp_path, 1)))


def test_cancel_and_remove_post_the_uuid() -> None:
    recorder = Recorder(
        {
            "/task/cancel": httpx.Response(200, json={"success": True}),
            "/task/remove": httpx.Response(200, json={"success": True}),
        }
    )
    node = client(recorder)

    node.cancel("u")
    node.remove("u")

    assert recorder.paths() == ["/task/cancel", "/task/remove"]
    assert "uuid=u" in recorder.calls[0].content.decode()
