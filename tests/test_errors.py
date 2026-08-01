"""The error contract, asserted the two ways it can be wrong (api.md §4; api#4).

An error contract fails in two places and they need different tests. It fails **in the document**,
where a generated client learns what an error looks like — so one set of tests walks the OpenAPI
document and asserts every operation declares the problem shape and nothing declares the array shape
it replaces. And it fails **in the response**, where a handler that forgot the contract answers
something else entirely — so the rest drive real requests and validate what comes back against
:class:`Problem`.

The route walk is the one the issue asks for: every route in the composed app, not a sample.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from astro_mine.cloud.artifacts.store import FilesystemArtifactStore
from astro_mine.cloud.packaging import ImageRef
from astro_mine.cloud.submission.jobspec import JobSpec
from astro_mine.hub.index import InMemoryCatalog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from astro_mine_api._app import SURFACES_ENV, build_app
from astro_mine_api._errors import (
    PROBLEM_MEDIA_TYPE,
    ApiError,
    ErrorCode,
    Problem,
    add_error_handlers,
    only_problem_media_type,
    status_code_for,
)
from astro_mine_api._openapi import current_document
from astro_mine_api.bench._app import create_app as bench_app
from astro_mine_api.cloud.app import create_app as cloud_app
from astro_mine_api.hub._app import create_app as hub_app
from astro_mine_api.studio.app import create_app as studio_app
from astro_mine_api.studio.serve import CACHE_DIR_ENV, REGISTRY_ENV

#: Every HTTP method a route in this API might not serve. Sending one a route does not have is the
#: only failure that can be provoked on *every* route without knowing anything about it — which is
#: what makes it a walk rather than a sample.
_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A developer's own environment must not steer which surfaces mount or how they wire."""
    for name in (SURFACES_ENV, REGISTRY_ENV, CACHE_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def document() -> dict[str, Any]:
    return current_document()


@pytest.fixture
def app() -> FastAPI:
    return build_app()


@pytest.fixture
def cloud(tmp_path: Path) -> TestClient:
    """The Cloud surface over a throwaway artifact store.

    Its default is a :class:`FilesystemArtifactStore` rooted at the working directory, and
    ``submit`` stages a job's inputs there *before* it resolves the backend — so a test driving the
    unknown-backend arm with the default store writes a blob into the repository.
    """
    return TestClient(cloud_app(store=FilesystemArtifactStore(tmp_path)))


def _operations(document: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, operations in document["paths"].items():
        for verb, operation in operations.items():
            yield path, verb, operation


def _problem(response: Any) -> Problem:
    """The response as a validated problem document — the assertion, not a helper around it."""
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE), response.headers
    problem = Problem.model_validate(response.json())
    assert problem.status == response.status_code
    return problem


# --- the contract in the document ---------------------------------------------------------------


def test_every_operation_declares_the_problem_shape(document: dict[str, Any]) -> None:
    """A client generated from this document gets one error type for the whole API."""
    for path, verb, operation in _operations(document):
        responses = operation["responses"]
        assert "default" in responses, f"{verb.upper()} {path} declares no error response"
        schema = responses["default"]["content"][PROBLEM_MEDIA_TYPE]["schema"]
        assert schema == {"$ref": "#/components/schemas/Problem"}, (verb, path)


def test_no_operation_still_answers_the_validation_array(document: dict[str, Any]) -> None:
    """FastAPI's ``HTTPValidationError`` is the array shape this issue exists to replace.

    It is generated only for a route that declares no 422 of its own, so its total absence is the
    proof that every router carries ``ERROR_RESPONSES`` — including any router added later.
    """
    for path, verb, operation in _operations(document):
        schema = operation["responses"].get("422", {}).get("content", {})
        assert PROBLEM_MEDIA_TYPE in schema, f"{verb.upper()} {path} has no problem-shaped 422"
    assert "HTTPValidationError" not in document["components"]["schemas"]
    assert "ValidationError" not in document["components"]["schemas"]


def test_the_document_never_advertises_a_problem_as_plain_json(document: dict[str, Any]) -> None:
    """The handlers send ``application/problem+json``; the document must not claim otherwise."""
    for path, verb, operation in _operations(document):
        for status, response in operation["responses"].items():
            content = response.get("content", {})
            if PROBLEM_MEDIA_TYPE in content:
                assert "application/json" not in content, (verb, path, status)


def test_the_error_codes_are_documented_as_an_enumeration(document: dict[str, Any]) -> None:
    """A client branches on ``code``, so the document has to say what the values are."""
    declared = set(document["components"]["schemas"]["ErrorCode"]["enum"])
    assert declared == {str(code) for code in ErrorCode}


# --- the contract in the responses: the route walk ------------------------------------------------


def _url(template: str) -> str:
    """A concrete URL for a templated path: every ``{parameter}`` filled with a literal.

    The value never resolves to anything, which is the point — the walk provokes a failure, and the
    parameter only has to make the path match.
    """
    return re.sub(r"\{[^}]+\}", "walk", template)


def _served(document: dict[str, Any]) -> dict[str, set[str]]:
    """Every path the app serves and the methods it serves, read from the OpenAPI document.

    Not from ``app.routes``: ``include_router`` does not flatten in FastAPI 0.141 — each included
    surface stays one route object wrapping its own — so iterating ``app.routes`` would walk five
    entries, report success, and have tested one of them. The document is both public and the same
    set a generated client sees, which is the set that matters here.
    """
    return {
        path: {verb.upper() for verb in operations}
        for path, operations in document["paths"].items()
    }


def test_the_route_walk_covers_the_whole_composed_app(document: dict[str, Any]) -> None:
    """A guard on the two walks below: they must actually be reaching every surface."""
    paths = _served(document)
    assert len(paths) > 25
    for prefix in ("/hub/", "/studio/", "/cloud/", "/bench/"):
        assert any(path.startswith(prefix) for path in paths), prefix


def test_every_route_answers_a_problem_document(app: FastAPI, document: dict[str, Any]) -> None:
    """The walk: drive a failure on **every** route and validate what comes back.

    A method the route does not serve is the one failure that can be provoked without knowing what
    the route wants, what backend it needs, or what it would consider missing — so this reaches the
    whole app, not the handlers someone remembered to test.

    The assertion is the acceptance criterion itself: *whatever* the app answers with, if it is an
    error it validates against the one schema. Mostly that is a 405, but not always, and the
    exceptions are the interesting part — ``GET /studio/campaigns/publish`` is absorbed by
    ``GET /studio/campaigns/{reference}``, which then refuses for its own reasons. That is correct
    routing, and its refusal is under the same contract, which is the whole point.
    """
    client = TestClient(app, raise_server_exceptions=False)
    refused = 0
    for path, methods in _served(document).items():
        unsupported = [method for method in _METHODS if method not in methods]
        assert unsupported, path  # a path serving every verb would make this vacuous
        response = client.request(unsupported[0], _url(path))
        if response.status_code < 400:
            continue  # absorbed by a route that could answer it
        problem = _problem(response)
        assert problem.code in set(ErrorCode), path
        assert problem.detail, path
        refused += 1
    assert refused >= 25, "the walk provoked almost nothing; check the method table"


def test_every_route_with_a_body_answers_one_validation_object(
    app: FastAPI, document: dict[str, Any]
) -> None:
    """The second walk: every route that takes a body, given a body it cannot accept.

    ``errors`` is the structured half a form needs; ``detail`` is the same information as **one
    string**. Together they are what makes ``formatDetail()`` unnecessary — see the module docstring
    of ``_errors.py``.

    Not every route reaches validation: Bench's writes authenticate first, and a body model that
    accepts extra keys is satisfied by anything. Those still have to answer in contract — asserted
    here — but only the ones that reach the validator prove the 422 shape, so those are counted.
    """
    client = TestClient(app, raise_server_exceptions=False)
    validated = 0
    for path, operations in document["paths"].items():
        for verb, operation in operations.items():
            if verb != "post" or "requestBody" not in operation:
                continue
            response = client.post(_url(path), json={"not": "a valid body"})
            if response.status_code < 400:
                continue
            problem = _problem(response)
            if problem.code is not ErrorCode.VALIDATION_FAILED:
                continue
            assert problem.errors, path
            assert isinstance(problem.detail, str) and problem.detail
            validated += 1
    assert validated >= 5, "the body walk reached almost no validators; check the filter"


def test_a_validation_failure_renders_as_words_with_no_client_side_flattening(
    cloud: TestClient,
) -> None:
    """The fixture the acceptance criteria ask for.

    Studio's client carried a ``formatDetail()`` whose whole job was to stop this rendering as
    ``[object Object],[object Object]``. The proof that it is no longer needed is that the naive
    thing a browser does — interpolate ``detail`` into the page — produces a sentence.
    """
    client = cloud
    problem = _problem(client.post("/cloud/jobs", json={"nope": 1}))

    assert problem.code is ErrorCode.VALIDATION_FAILED
    assert f"{problem.detail}" == problem.detail  # a string, not a list of objects
    assert "[object Object]" not in problem.detail
    assert "image" in problem.detail and "Field required" in problem.detail
    # And the structured half is still there for a form that wants to mark its own fields.
    assert [field.field for field in problem.errors] == ["body.image", "body.nope"]
    assert problem.errors[0].type == "missing"


# --- the codes a client branches on ---------------------------------------------------------------


def test_hub_tells_unconfigured_publishing_from_a_refused_namespace() -> None:
    """The exact distinction Hub's UI used to make by reading a bare HTTP status."""
    client = TestClient(hub_app(InMemoryCatalog()))
    body = {"manifest": {}, "digest": "sha256:abc", "publisher": "someone"}

    unconfigured = _problem(client.post("/hub/publish", json=body))
    assert unconfigured.code is ErrorCode.PUBLISH_UNCONFIGURED
    assert unconfigured.status == 503


def test_hub_distinguishes_a_missing_artifact_from_an_unsatisfiable_constraint() -> None:
    """Two 404s that mean different things, and now say so."""
    client = TestClient(hub_app(InMemoryCatalog()))

    missing = _problem(client.get("/hub/artifacts/nobody/1.0.0"))
    assert missing.code is ErrorCode.CONTENT_NOT_FOUND

    unresolvable = _problem(client.post("/hub/resolve", json={"name": "nobody"}))
    assert unresolvable.code is ErrorCode.RESOLUTION_FAILED
    assert missing.status == unresolvable.status == 404


def test_studio_reports_an_unwired_seam_as_a_capability_not_a_client_error() -> None:
    client = TestClient(studio_app())
    problem = _problem(client.get("/studio/catalog/assets"))
    assert problem.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert problem.status == 503
    # And it names something an operator can act on, rather than an install extra that no longer
    # exists in this distribution.
    assert "ASTRO_MINE_HUB_REGISTRY" in problem.detail


def test_cloud_reports_an_unknown_backend_as_an_invalid_request(cloud: TestClient) -> None:
    """A well-formed request asking for something impossible — not a validation failure."""
    image = ImageRef.parse("ghcr.io/astro-mine/x@sha256:" + "ab" * 32)
    job = JobSpec(image=image, command=["run"], seed=7).model_dump(mode="json")
    problem = _problem(cloud.post("/cloud/jobs", params={"backend": "nope"}, json=job))
    assert problem.code is ErrorCode.INVALID_REQUEST
    assert problem.status == 400


def test_bench_refuses_an_unauthenticated_write_with_a_named_code() -> None:
    """No IdP configured ⇒ the deployment cannot authenticate anyone, so it refuses (bench#29)."""
    client = TestClient(bench_app())
    problem = _problem(client.post("/bench/submissions/hub", json={}))
    # The body never validates, because the dependency refuses first — which is the point: a client
    # gets a code for the reason it was actually refused.
    assert problem.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert problem.status == 503


def test_bench_reports_a_missing_job_as_content_not_found() -> None:
    problem = _problem(TestClient(bench_app()).get("/bench/jobs/nope"))
    assert problem.code is ErrorCode.CONTENT_NOT_FOUND


# --- the arms nothing else reaches ----------------------------------------------------------------


def test_an_unrouted_path_is_still_a_problem_document(app: FastAPI) -> None:
    """Starlette's own 404, not a handler's — it has to leave under the same contract."""
    problem = _problem(TestClient(app).get("/hub/no-such-route"))
    assert problem.code is ErrorCode.CONTENT_NOT_FOUND


def test_an_unhandled_exception_leaks_nothing() -> None:
    """The 500 arm: a problem document, and not one word about what actually went wrong."""
    app = add_error_handlers(FastAPI())

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("the database password is hunter2")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    problem = _problem(response)
    assert problem.code is ErrorCode.INTERNAL_ERROR
    assert problem.status == 500
    assert "hunter2" not in response.text and "RuntimeError" not in response.text


def test_the_media_type_fix_drops_only_the_redundant_entry() -> None:
    """Both media types go in, only the untrue one comes out; a success response is untouched."""
    document = only_problem_media_type(
        {
            "paths": {
                "/x": {
                    "get": {
                        "responses": {
                            "200": {"content": {"application/json": {"schema": {}}}},
                            "default": {
                                "content": {
                                    "application/json": {"schema": {}},
                                    PROBLEM_MEDIA_TYPE: {"schema": {}},
                                }
                            },
                        }
                    }
                }
            }
        }
    )
    responses = document["paths"]["/x"]["get"]["responses"]
    assert set(responses["default"]["content"]) == {PROBLEM_MEDIA_TYPE}
    assert set(responses["200"]["content"]) == {"application/json"}


def test_the_media_type_fix_tolerates_a_path_level_key() -> None:
    """A path item may carry keys that are not operations — ``summary``, ``parameters``.

    FastAPI emits none of them today, but this transform runs on every document this distribution
    serves, and "the schema generator raised" is a bad way to find out that changed.
    """
    document = only_problem_media_type(
        {"paths": {"/x": {"parameters": [{"name": "q", "in": "query"}], "summary": "s"}}}
    )
    assert document["paths"]["/x"]["parameters"] == [{"name": "q", "in": "query"}]


def test_an_api_error_takes_its_status_from_its_code() -> None:
    """A raise site names the reason; pairing it with the wrong number is not expressible."""
    for code in ErrorCode:
        assert ApiError(code, "why").status_code == status_code_for(code)


def test_an_upstream_status_can_override_the_code_default() -> None:
    """Bench's audited refusals carry a status and no code; the route maps between them."""
    error = ApiError(ErrorCode.SUBMISSION_REJECTED, "denied", status_code=403)
    assert error.status_code == 403
    assert error.problem.status == 403
    assert error.problem.code is ErrorCode.SUBMISSION_REJECTED


def test_an_api_error_without_handlers_still_answers_its_status() -> None:
    """The degradation: a composition that forgot ``add_error_handlers`` loses ``code``, not the
    status. A missing wiring step must not turn every refusal into a 500."""
    app = FastAPI()

    @app.get("/gone")
    def gone() -> None:
        raise ApiError(ErrorCode.CONTENT_NOT_FOUND, "no such thing")

    response = TestClient(app).get("/gone")
    assert response.status_code == 404
    assert response.json() == {"detail": "no such thing"}
