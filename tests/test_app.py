"""The composition root — mounting the surfaces a deployment enables (api.md §3, §6).

The property that makes one image serve four surfaces is the component prefix: because every
route begins with its owning component's name, composing them is inclusion rather than routing.
These assert that directly — the composed app answers on exactly the paths the single-surface
apps do, the surfaces do not collide, and a deployment that enables a subset gets that subset and
nothing else.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from astro_mine.core.registry import CapabilityTag
from astro_mine.hub.registry import Registry
from astro_mine.hub.supply_chain import generate_keypair
from fastapi.testclient import TestClient

from astro_mine_api import __version__
from astro_mine_api._app import SURFACES, SURFACES_ENV, build_app, enabled_surfaces, make_app
from astro_mine_api.bench import PREFIX as BENCH_PREFIX
from astro_mine_api.cloud import PREFIX as CLOUD_PREFIX
from astro_mine_api.hub import PREFIX as HUB_PREFIX
from astro_mine_api.studio import PREFIX as STUDIO_PREFIX
from astro_mine_api.studio.serve import CACHE_DIR_ENV, REGISTRY_ENV

from .studio._assets import publish_asset


@pytest.fixture(autouse=True)
def _no_ambient_surface_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A developer's own ``ASTRO_MINE_API_SURFACES``/registry must not steer these tests."""
    for name in (SURFACES_ENV, REGISTRY_ENV, CACHE_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    yield


def _paths(app_paths: dict[str, object]) -> set[str]:
    return set(app_paths)


# --- which surfaces get mounted ------------------------------------------------------------


def test_every_surface_is_mounted_by_default() -> None:
    paths = _paths(build_app().openapi()["paths"])
    for prefix in (HUB_PREFIX, STUDIO_PREFIX, CLOUD_PREFIX, BENCH_PREFIX):
        assert any(path.startswith(f"{prefix}/") for path in paths), prefix


def test_a_subset_deployment_mounts_only_what_it_asked_for() -> None:
    paths = _paths(build_app(["hub", "cloud"]).openapi()["paths"])
    assert any(path.startswith(f"{HUB_PREFIX}/") for path in paths)
    assert any(path.startswith(f"{CLOUD_PREFIX}/") for path in paths)
    # Not merely "fewer paths" — the surfaces nobody enabled are absent entirely.
    assert not any(path.startswith(f"{STUDIO_PREFIX}/") for path in paths)
    assert not any(path.startswith(f"{BENCH_PREFIX}/") for path in paths)


def test_the_environment_selects_the_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SURFACES_ENV, " Bench , cloud ")
    assert enabled_surfaces() == ("cloud", "bench")


def test_an_empty_environment_value_means_every_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env var set to "" is a deployment that configured nothing, not one that wants nothing."""
    monkeypatch.setenv(SURFACES_ENV, "  ,  ")
    assert enabled_surfaces() == SURFACES


def test_the_mount_order_is_stable_regardless_of_request_order() -> None:
    """A deployment that reorders its env var must not reshuffle its OpenAPI document."""
    expected = ("hub", "bench")
    assert enabled_surfaces(["bench", "hub"]) == expected
    assert enabled_surfaces(["hub", "bench"]) == expected


def test_a_typo_in_the_surface_list_fails_at_composition_time() -> None:
    """The failure mode a typo in a deployment manifest must have: loud, not a missing surface."""
    with pytest.raises(ValueError, match="unknown API surface"):
        enabled_surfaces(["hub", "studioo"])


def test_make_app_is_the_asgi_factory() -> None:
    app = make_app()
    assert app.title == "Astro-Mine-API"


# --- the composed app answers like the single-surface ones ---------------------------------


def test_each_surface_answers_its_own_health_endpoint() -> None:
    """One spelling and one shape across all four (api#4); `test_health.py` owns the details."""
    client = TestClient(build_app())
    for prefix in (HUB_PREFIX, STUDIO_PREFIX, CLOUD_PREFIX, BENCH_PREFIX):
        assert client.get(f"{prefix}/healthz").json()["status"] == "ok", prefix


def test_the_deployment_health_endpoint_names_what_it_serves() -> None:
    body = TestClient(build_app(["hub", "bench"])).get("/healthz").json()
    assert body == {
        "status": "ok",
        "component": "api",
        "version": __version__,
        "surfaces": ["hub", "bench"],
    }


def test_surfaces_do_not_shadow_each_other() -> None:
    """Cloud and Bench both own a `/jobs` space; the prefix is what keeps them apart."""
    client = TestClient(build_app(["cloud", "bench"]))
    assert client.get(f"{CLOUD_PREFIX}/backends").status_code == 200
    # Bench's is `/jobs/{job_id}` — an unknown id is its 404, not Cloud's route answering.
    unknown = client.get(f"{BENCH_PREFIX}/jobs/nope")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "content_not_found"


def test_the_openapi_document_covers_every_mounted_surface() -> None:
    """One document for the deployment — the API's documentation is the generated schema
    (api.md §4), so a surface missing from it is a surface a client cannot discover."""
    paths = _paths(build_app().openapi()["paths"])
    assert {
        f"{HUB_PREFIX}/publish",
        f"{STUDIO_PREFIX}/intent",
        f"{CLOUD_PREFIX}/jobs",
        f"{BENCH_PREFIX}/submissions",
        "/healthz",
    } <= paths


# --- the Studio surface's environment wiring -----------------------------------------------


def test_studio_mounts_unwired_when_no_registry_is_configured() -> None:
    """No registry → the Hub-backed routes 503 honestly; intent capture still works (CX-LOCAL)."""
    client = TestClient(build_app(["studio"]))
    assert client.get(f"{STUDIO_PREFIX}/healthz").status_code == 200
    assert client.get(f"{STUDIO_PREFIX}/catalog/assets").status_code == 503


def test_studio_wires_its_seams_from_a_configured_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_pem, public_pem = generate_keypair()
    registry_path = tmp_path / "registry"
    registry = Registry(registry_path)
    publish_asset(
        registry,
        private_pem,
        asset_id="hopper",
        kind="hopper",
        name="Hopper Mk1",
        tags=[CapabilityTag("mobility.wheeled")],
    )
    keys = registry_path / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    (keys / "cosign.pub").write_bytes(public_pem)
    (keys / "cosign.key").write_bytes(private_pem)
    monkeypatch.setenv(REGISTRY_ENV, str(registry_path))
    monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path / "cache"))

    client = TestClient(build_app(["studio"]))
    catalog = client.get(f"{STUDIO_PREFIX}/catalog/assets")
    assert catalog.status_code == 200
    assert [row["kind"] for row in catalog.json()] == ["hopper"]
    # A wired materializer 404s an unknown reference; an unwired one would 503.
    assert client.get(f"{STUDIO_PREFIX}/worlds/does-not-exist").status_code == 404
    # The static mounts moved under the prefix with the routes.
    assert client.get(f"{STUDIO_PREFIX}/assets/files/").status_code in (403, 404)
