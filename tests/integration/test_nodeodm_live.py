"""Live NodeODM checks.

Excluded from ordinary CI by the `integration` marker; run them with:

    uv run pytest -m integration

These exist because the mocked unit tests prove the client is self-consistent, not that its
picture of NodeODM is correct. Only a real node can tell us that, and it is the only place
the engine version can be established at all.

    docker compose up -d
    uv run pytest -m integration
"""

from __future__ import annotations

import os

import pytest

from drone_photogrammetry_pipeline.nodeodm.client import NodeODMClient

pytestmark = pytest.mark.integration

NODE_URL = os.environ.get("DPP_NODEODM_URL", "http://localhost:3000")


@pytest.fixture(scope="module")
def node() -> NodeODMClient:
    client = NodeODMClient(NODE_URL, timeout=15.0, max_attempts=2, backoff_seconds=0.5)
    if not client.health():
        pytest.skip(f"no NodeODM at {NODE_URL}; run 'docker compose up -d' first")
    return client


def test_the_node_answers_and_names_its_engine(node: NodeODMClient) -> None:
    """Establishes what is actually running behind the pinned digest."""
    info = node.info()

    assert info.version, "NodeODM reported no version"
    assert info.engine == "odm"
    assert info.engineVersion, "NodeODM reported no engine version"
    print(
        f"\nNodeODM {info.version}, engine {info.engine} {info.engineVersion}, "
        f"{info.cpuCores} cores, {info.maxImages or 'unlimited'} max images"
    )


def test_the_engine_exposes_the_options_the_profiles_rely_on(node: NodeODMClient) -> None:
    """Every ODM option named in a shipped profile must exist on the running engine.

    This is the check that keeps profiles from drifting away from the engine. It fails
    loudly rather than letting a stale option name be silently ignored at processing time.
    """
    options = node.options()
    assert options, "GET /options returned nothing"

    # Note what is NOT here: `gcp` and `geo`. Those are ODM command-line arguments but they
    # are not NodeODM processing options — ground control and geolocation reach ODM as files
    # uploaded alongside the images, not as name/value options. An earlier version of this
    # test expected them and failed against a healthy engine.
    relied_on = {
        "orthophoto-compression",
        "orthophoto-cutline",
        "orthophoto-resolution",
        "build-overviews",
        "texturing-skip-global-seam-leveling",
        "radiometric-calibration",
        "dsm",
        "dtm",
        "crop",
    }
    missing = node.unknown_options(dict.fromkeys(relied_on, None))
    assert not missing, f"options this engine does not recognise: {missing}"


def test_ground_control_is_not_a_processing_option(node: NodeODMClient) -> None:
    """Records the mechanism, so the adapter does not try to pass them as options."""
    assert node.unknown_options({"gcp": None, "geo": None}) == ["gcp", "geo"]


def test_an_invented_option_is_reported_as_unknown(node: NodeODMClient) -> None:
    assert node.unknown_options({"definitely-not-an-odm-option": 1}) == [
        "definitely-not-an-odm-option"
    ]


def test_asking_about_a_nonexistent_task_fails_cleanly(node: NodeODMClient) -> None:
    from drone_photogrammetry_pipeline.nodeodm.client import NodeODMError

    with pytest.raises(NodeODMError):
        node.task_info("00000000-0000-0000-0000-000000000000")
