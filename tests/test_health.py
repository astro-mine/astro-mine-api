"""One health endpoint spelling, one health shape (api.md §4; conventions.md §10; api#4).

The port kept four surfaces' worth of accumulated spelling — ``/hub/health`` against three
``/healthz`` — and three different bodies. ``api.md`` §4 called the convergence *"a visible,
low-cost example of what one home for the decision is for"*. These are what makes it stay
converged: a new surface that invents a fourth spelling or a fifth shape fails here, not in a
deployment's probe configuration six months later.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from astro_mine.hub.index import InMemoryCatalog
from fastapi.testclient import TestClient

from astro_mine_api import __version__
from astro_mine_api._app import SURFACES, SURFACES_ENV, build_app
from astro_mine_api._health import Health
from astro_mine_api._openapi import current_document
from astro_mine_api.bench._app import create_app as bench_app
from astro_mine_api.cloud.app import create_app as cloud_app
from astro_mine_api.hub._app import DEPRECATED_HEALTH_PATH
from astro_mine_api.hub._app import PREFIX as HUB_PREFIX
from astro_mine_api.hub._app import create_app as hub_app
from astro_mine_api.studio.app import create_app as studio_app
from astro_mine_api.studio.serve import CACHE_DIR_ENV, REGISTRY_ENV

#: The alias, spelled out: ``/hub/health``.
DEPRECATED_HEALTH = f"{HUB_PREFIX}{DEPRECATED_HEALTH_PATH}"


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (SURFACES_ENV, REGISTRY_ENV, CACHE_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def document() -> dict[str, Any]:
    return current_document()


# --- one spelling ---------------------------------------------------------------------------


def test_every_surface_answers_healthz() -> None:
    """The convergence, asserted against the composed deployment a probe would actually hit."""
    client = TestClient(build_app())
    for surface in SURFACES:
        response = client.get(f"/{surface}/healthz")
        assert response.status_code == 200, surface
        assert Health.model_validate(response.json()).component == surface


def test_the_deployment_and_every_surface_use_the_same_path_segment(
    document: dict[str, Any],
) -> None:
    """No surface may reintroduce a spelling of its own — including one added later."""
    health_paths = {
        path
        for path in document["paths"]
        if path.rsplit("/", 1)[-1] in {"health", "healthz", "healthcheck", "livez", "ping"}
    }
    assert health_paths == {
        "/healthz",
        "/hub/healthz",
        "/hub/health",  # the deprecated alias, and the only exception
        "/studio/healthz",
        "/cloud/healthz",
        "/bench/healthz",
    }


@pytest.mark.parametrize(
    ("factory", "component", "path"),
    [
        pytest.param(lambda: hub_app(InMemoryCatalog()), "hub", "/hub/healthz", id="hub"),
        pytest.param(studio_app, "studio", "/studio/healthz", id="studio"),
        pytest.param(cloud_app, "cloud", "/cloud/healthz", id="cloud"),
        pytest.param(bench_app, "bench", "/bench/healthz", id="bench"),
    ],
)
def test_a_single_surface_deployment_answers_the_same_path(
    factory: Any, component: str, path: str
) -> None:
    """A surface served alone must not answer somewhere else than when it is composed."""
    assert TestClient(factory()).get(path).json()["component"] == component


# --- one shape ------------------------------------------------------------------------------


def test_every_surface_answers_the_same_shape() -> None:
    """The property a probe, a status page and a generated client all depend on."""
    client = TestClient(build_app())
    bodies = [client.get(f"/{surface}/healthz").json() for surface in SURFACES]
    assert {frozenset(body) for body in bodies} == {frozenset({"status", "component", "version"})}
    assert {body["status"] for body in bodies} == {"ok"}
    assert {body["version"] for body in bodies} == {__version__}
    assert [body["component"] for body in bodies] == list(SURFACES)


def test_the_deployment_health_is_the_surface_shape_plus_one_field() -> None:
    """Not a shape of its own: the extra field is the only thing a reader has to know about."""
    body = TestClient(build_app(["hub", "bench"])).get("/healthz").json()
    assert body == {
        "status": "ok",
        "component": "api",
        "version": __version__,
        "surfaces": ["hub", "bench"],
    }
    # And it still validates as the surface shape, which is what "plus one field" has to mean.
    assert Health.model_validate(body).component == "api"


def test_the_document_gives_every_health_route_the_same_response_model(
    document: dict[str, Any],
) -> None:
    """A generated client gets one type for a surface's health, not four near-identical ones."""
    for path in ("/hub/healthz", "/hub/health", "/studio/healthz", "/cloud/healthz"):
        schema = document["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
        assert schema["schema"] == {"$ref": "#/components/schemas/Health"}, path
    deployment = document["paths"]["/healthz"]["get"]["responses"]["200"]["content"]
    assert deployment["application/json"]["schema"] == {
        "$ref": "#/components/schemas/DeploymentHealth"
    }


# --- the deprecated alias -------------------------------------------------------------------


def test_the_old_hub_spelling_still_answers() -> None:
    """Kept for one cycle: nothing probing it breaks on the deploy that converges it."""
    client = TestClient(build_app(["hub"]))
    assert client.get(DEPRECATED_HEALTH).json() == client.get("/hub/healthz").json()


def test_the_old_hub_spelling_says_it_is_deprecated() -> None:
    """In the headers, for a client that reads responses."""
    response = TestClient(build_app(["hub"])).get(DEPRECATED_HEALTH)
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Link"] == '</hub/healthz>; rel="successor-version"'


def test_the_old_hub_spelling_is_deprecated_in_the_document(document: dict[str, Any]) -> None:
    """And in the schema, for a client that is generated from it — which is the one that matters.

    A generated client marks a deprecated operation as deprecated, so a front end calling it gets
    told by its own tooling rather than by a changelog nobody read.
    """
    alias = document["paths"][DEPRECATED_HEALTH]["get"]
    assert alias["deprecated"] is True
    assert set(alias["responses"]["200"]["headers"]) == {"Deprecation", "Link"}
    assert document["paths"]["/hub/healthz"]["get"].get("deprecated") is not True


def test_the_alias_and_the_successor_are_different_operations(document: dict[str, Any]) -> None:
    """Two operation ids: a client can drop the deprecated call site without renaming the other."""
    assert document["paths"][DEPRECATED_HEALTH]["get"]["operationId"] == "hub_health"
    assert document["paths"]["/hub/healthz"]["get"]["operationId"] == "hub_healthz"
