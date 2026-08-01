"""Cross-origin access for the browser tier (api#2; ``_cors.py``).

The front end is a static export, so every call it makes is cross-origin. These assert the headers
a *browser* acts on rather than inspecting the configuration — a CORS policy that is merely
configured is a policy nobody has seen work.

Two properties get the most attention here because both are the kind that erode quietly: that a
request carrying no ``Origin`` is completely untouched (the local tier must not acquire a
requirement), and that credentials are never permitted.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from astro_mine.hub.index import InMemoryCatalog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from astro_mine_api._app import SURFACES_ENV, build_app
from astro_mine_api._cors import (
    CORS_ORIGINS_ENV,
    DEFAULT_ORIGINS,
    add_cors,
    resolve_origins,
)
from astro_mine_api.bench._app import create_app as bench_app
from astro_mine_api.cloud.app import create_app as cloud_app
from astro_mine_api.hub._app import create_app as hub_app
from astro_mine_api.studio.app import create_app as studio_app

ALLOWED = "https://console.example.org"
DENIED = "https://not-the-console.example.org"
DEV = DEFAULT_ORIGINS[0]


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A developer's own environment must not steer these assertions."""
    for name in (CORS_ORIGINS_ENV, SURFACES_ENV):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def allowlisted(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(CORS_ORIGINS_ENV, ALLOWED)
    yield


# --- the allowlist ---------------------------------------------------------------------------


def test_an_unset_environment_allows_the_development_origins() -> None:
    assert resolve_origins() == list(DEFAULT_ORIGINS)


def test_localhost_and_127_0_0_1_are_both_listed() -> None:
    """A browser treats them as different origins; allowing only one is a confusing half-fix."""
    assert "http://localhost:3000" in DEFAULT_ORIGINS
    assert "http://127.0.0.1:3000" in DEFAULT_ORIGINS


def test_the_environment_supplies_a_comma_separated_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CORS_ORIGINS_ENV, "https://a.example, https://b.example")
    assert resolve_origins() == ["https://a.example", "https://b.example"]


def test_blank_entries_and_duplicates_are_dropped_and_order_is_kept() -> None:
    assert resolve_origins("https://b.example, ,https://a.example,https://b.example,") == [
        "https://b.example",
        "https://a.example",
    ]


def test_a_set_but_empty_value_means_no_cross_origin_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment turning this off must be able to say so — and not get the dev default back."""
    monkeypatch.setenv(CORS_ORIGINS_ENV, "")
    assert resolve_origins() == []


def test_a_wildcard_is_honoured_as_an_explicit_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safe only because credentials are never allowed — see the credentials test below."""
    monkeypatch.setenv(CORS_ORIGINS_ENV, "*")
    assert resolve_origins() == ["*"]


# --- what a browser actually sees ------------------------------------------------------------


@pytest.mark.usefixtures("allowlisted")
def test_a_page_on_another_origin_can_read_the_scenarios() -> None:
    """The acceptance criterion's simple-request half: ``GET /bench/scenarios``."""
    response = TestClient(build_app()).get("/bench/scenarios", headers={"Origin": ALLOWED})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED


@pytest.mark.usefixtures("allowlisted")
def test_the_preflight_for_creating_a_study_is_answered() -> None:
    """``POST /studio/studies`` sends JSON, so a browser preflights it before sending anything."""
    response = TestClient(build_app()).options(
        "/studio/studies",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


@pytest.mark.usefixtures("allowlisted")
def test_the_preflight_for_retracting_a_submission_is_answered() -> None:
    """``DELETE /bench/submissions/{id}`` is a steward action, and preflights like any other.

    The preflight itself carries no token — a browser never sends credentials on one — so it is
    answerable without auth, and a preflight that demanded auth would be unanswerable.

    That is only half of what the route needs, and reading it as the whole is what api#8 fixed: the
    preflight must also **declare** that ``authorization`` may be sent, or the browser answers the
    question "may I send this?" with no, and the real request never leaves. See
    :func:`test_every_route_that_takes_a_bearer_token_permits_it_at_the_preflight`.
    """
    response = TestClient(build_app()).options(
        "/bench/submissions/any-id",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "DELETE" in response.headers["access-control-allow-methods"]


# --- the bearer token can arrive (api#8) --------------------------------------------------------


def _routes_taking_a_bearer_token() -> list[tuple[str, str]]:
    """Every ``(method, path)`` whose operation declares an ``authorization`` header parameter.

    Read off the OpenAPI document rather than listed by hand, for the reason api.md §7 gives for
    the error and health contracts: a test that walks the routes keeps covering them, and a test
    that names five keeps covering five. An authenticated route added later is included here the
    day it is added, with nobody remembering to do it.
    """
    document = build_app().openapi()
    found: list[tuple[str, str]] = []
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            parameters = operation.get("parameters", [])
            if any(p.get("in") == "header" and p["name"] == "authorization" for p in parameters):
                found.append((method.upper(), path))
    return sorted(found)


def test_some_route_takes_a_bearer_token() -> None:
    """Guards the walk above against becoming a test of nothing.

    If the document stopped declaring the header — a refactor that dropped the parameter, say —
    the parametrised test below would collapse to zero cases and pass in silence, which is the
    failure mode of every test that iterates a discovered set.
    """
    assert _routes_taking_a_bearer_token()


@pytest.mark.usefixtures("allowlisted")
@pytest.mark.parametrize(("method", "path"), _routes_taking_a_bearer_token())
def test_every_route_that_takes_a_bearer_token_permits_it_at_the_preflight(
    method: str, path: str
) -> None:
    """The preflight must name ``authorization``, or the request it guards is never sent.

    Starlette checks each requested header against the allowlist and fails the whole preflight on
    the first miss — a 400 ``Disallowed CORS headers``, before any route is reached. With
    ``content-type`` alone allowed, Bench's entire write path and the steward's audit view were
    unreachable from a browser while every test here stayed green, because every one of them asked
    for ``content-type`` and nothing else (api#8).
    """
    response = TestClient(build_app()).options(
        path.replace("{submission_id}", "any-id"),
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert method in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


@pytest.mark.usefixtures("allowlisted")
def test_permitting_the_token_did_not_permit_credentials() -> None:
    """The two are different mechanisms, and this is the assertion that keeps them apart.

    An ``Authorization`` header a page sets explicitly is an ordinary request header. CORS
    *credentials* are cookies and auth state the browser attaches by itself, and allowing those is
    the plausible-looking wrong fix for api#8 — it would not have been needed, and it would have
    turned a wildcard allowlist into a genuine hazard.
    """
    response = TestClient(build_app()).options(
        "/bench/submissions",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type",
        },
    )
    assert "authorization" in response.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers


@pytest.mark.usefixtures("allowlisted")
def test_an_origin_off_the_allowlist_is_refused_legibly() -> None:
    """Not a silent empty response: the preflight is refused with a reason a human can read."""
    response = TestClient(build_app()).options(
        "/studio/studies",
        headers={"Origin": DENIED, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert "disallowed cors origin" in response.text.lower()
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.usefixtures("allowlisted")
def test_a_disallowed_origin_gets_no_allow_header_on_a_simple_request() -> None:
    """The browser is what blocks this one; our job is to not vouch for the origin."""
    response = TestClient(build_app()).get("/bench/scenarios", headers={"Origin": DENIED})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.usefixtures("allowlisted")
def test_credentials_are_never_allowed() -> None:
    """``*`` plus credentials is the combination that turns any page into an authenticated client.

    Reads are account-free and nothing in the browser tier carries a cookie, so the header must be
    absent whatever the allowlist says.
    """
    client = TestClient(build_app())
    simple = client.get("/bench/scenarios", headers={"Origin": ALLOWED})
    preflight = client.options(
        "/studio/studies",
        headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-credentials" not in simple.headers
    assert "access-control-allow-credentials" not in preflight.headers


# --- the local tier is untouched ---------------------------------------------------------------


def test_a_request_without_an_origin_is_untouched() -> None:
    """``curl``, the CLI and server-to-server callers see what they saw before (CX-LOCAL)."""
    response = TestClient(build_app()).get("/bench/scenarios")
    assert response.status_code == 200
    assert not [name for name in response.headers if name.lower().startswith("access-control-")]


def test_no_configuration_is_required_for_the_local_development_origin() -> None:
    """With nothing set, `pnpm dev` against a local `uvicorn` works — the zero-config path."""
    response = TestClient(build_app()).get("/bench/scenarios", headers={"Origin": DEV})
    assert response.headers["access-control-allow-origin"] == DEV


# --- every app this distribution builds ---------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(lambda: build_app(), id="composed"),
        pytest.param(bench_app, id="bench"),
        pytest.param(cloud_app, id="cloud"),
        pytest.param(lambda: hub_app(InMemoryCatalog()), id="hub"),
        pytest.param(studio_app, id="studio"),
    ],
)
def test_every_app_factory_installs_the_policy(factory: object) -> None:
    """A route test must drive an app that behaves like the deployed one in this dimension."""
    app = factory()  # type: ignore[operator]
    assert "CORSMiddleware" in [m.cls.__name__ for m in app.user_middleware]


def test_add_cors_returns_the_app_it_was_given() -> None:
    app = FastAPI()
    assert add_cors(app) is app
