"""One error contract for every surface (api.md §4; api#4).

``api.md`` §4 asks for *"a problem response with a stable machine-readable code, not a string that a
client parses"*. Before this module there was neither: handlers raised ``HTTPException`` with prose,
FastAPI answered validation failures with an **array** of ``{loc, msg}`` objects, and the front end
compensated — Studio's client carried a ``formatDetail()`` whose whole job was to stop a 422
rendering as ``[object Object],[object Object]``, and Hub's UI branched on a bare HTTP status to
tell "publishing is unconfigured" from "the artifact did not verify".

**The shape.** One :class:`Problem` document, served as ``application/problem+json``:

.. code-block:: json

    {
      "code": "admission_rejected",
      "title": "Admission rejected",
      "status": 422,
      "detail": "manifest digest does not match its stored config",
      "errors": []
    }

It follows RFC 9457: ``title``, ``status`` and ``detail`` carry their RFC meanings, and ``code`` is
the extension member. ``type`` is deliberately absent — the RFC defaults it to ``about:blank``, and
a second nominal URI that nothing resolves would be a second name for one concept, which
``conventions.md`` §3.1 has strong and well-earned views about. **``code`` is the only thing a
client should branch on.** ``detail`` is for a human to read; ``errors`` is always present so a
client never has to test for it.

**Registered at composition**, alongside the CORS policy and for the same reason: every app this
distribution builds — the composed one and each single-surface ``create_app`` — installs these
through :func:`add_error_handlers`, so a route test drives an app that fails like the deployed one.
Four handlers cover everything a request can end in: an :class:`ApiError`, a bare ``HTTPException``
raised by the framework (a 405, a 404 on an unrouted path), a request that failed validation, and
the unhandled case.

**In the document, not just the handlers.** :data:`ERROR_RESPONSES` goes on every router, which does
two things: it declares the problem shape to the generated client, and it suppresses FastAPI's
automatic ``HTTPValidationError`` — the array shape this replaces — because FastAPI only adds that
when a route declares no ``422``/``default`` of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

__all__ = [
    "ERROR_RESPONSES",
    "PROBLEM_MEDIA_TYPE",
    "ApiError",
    "ErrorCode",
    "FieldProblem",
    "Problem",
    "add_error_handlers",
    "only_problem_media_type",
    "status_code_for",
]

#: RFC 9457. A client that knows nothing about this API can still recognise a problem response.
PROBLEM_MEDIA_TYPE = "application/problem+json"

#: The keys of an OpenAPI path item that are operations rather than metadata.
_HTTP_VERBS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"})


class ErrorCode(StrEnum):
    """Every error this API can answer with, named rather than inferred from a status.

    A status is not an identity: three different things answer 503 and four answer 404, which is
    exactly why the front end was reduced to reading messages. These are the names it branches on
    instead, and the six the issue called out — publish unconfigured, namespace refused, admission
    rejected, resolution failed, content not found, capability unavailable — are all here.

    **Append-only.** A code is public API the moment a client switches on it; removing or
    repurposing one breaks that client silently, in the arm it takes least often.
    """

    #: The deployment has no registry, so ``POST /hub/publish`` cannot verify what it would index.
    PUBLISH_UNCONFIGURED = "publish_unconfigured"
    #: A publish request claimed a trust tier above ``open``; promotion is audited, never asserted.
    NAMESPACE_REFUSED = "namespace_refused"
    #: The supply-chain verdict: the artifact did not verify, so it was not admitted.
    ADMISSION_REJECTED = "admission_rejected"
    #: No artifact satisfies the name, version spec, interfaces and capability tags requested.
    RESOLUTION_FAILED = "resolution_failed"
    #: The named artifact, submission, job, campaign, world, asset or replay does not exist.
    CONTENT_NOT_FOUND = "content_not_found"
    #: The route exists but this deployment cannot serve it — an unwired seam, not a client error.
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    #: Policy refused a download at the gated boundary (licence, grants, verification).
    DOWNLOAD_DENIED = "download_denied"
    #: The request body, path or query did not validate. ``errors`` carries the field-level detail.
    VALIDATION_FAILED = "validation_failed"
    #: The request was well-formed but asked for something impossible (an unknown backend, …).
    INVALID_REQUEST = "invalid_request"
    #: No bearer token, or one that did not verify against the deployment's IdP.
    NOT_AUTHENTICATED = "not_authenticated"
    #: Authenticated, but the policy engine denied the action.
    NOT_AUTHORIZED = "not_authorized"
    #: Over the per-subject submission quota for the current window.
    RATE_LIMITED = "rate_limited"
    #: A leaderboard submission was refused for a reason that is not a supply-chain verdict —
    #: an invalid manifest, an interface mismatch, or a policy that did not execute cleanly.
    SUBMISSION_REJECTED = "submission_rejected"
    #: The request conflicts with existing state (an immutable artifact already published).
    CONFLICT = "conflict"
    #: The path exists but not for this method.
    METHOD_NOT_ALLOWED = "method_not_allowed"
    #: The handler raised. Nothing about the cause reaches the client.
    INTERNAL_ERROR = "internal_error"


#: The status each code answers with, so a raise site names the *reason* and not the number.
_STATUS: dict[ErrorCode, int] = {
    ErrorCode.PUBLISH_UNCONFIGURED: 503,
    ErrorCode.NAMESPACE_REFUSED: 403,
    ErrorCode.ADMISSION_REJECTED: 422,
    ErrorCode.RESOLUTION_FAILED: 404,
    ErrorCode.CONTENT_NOT_FOUND: 404,
    ErrorCode.CAPABILITY_UNAVAILABLE: 503,
    ErrorCode.DOWNLOAD_DENIED: 403,
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.NOT_AUTHENTICATED: 401,
    ErrorCode.NOT_AUTHORIZED: 403,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.SUBMISSION_REJECTED: 422,
    ErrorCode.CONFLICT: 409,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.INTERNAL_ERROR: 500,
}

#: The human-readable summary line. Short, stable, and never the thing a client branches on.
_TITLE: dict[ErrorCode, str] = {
    ErrorCode.PUBLISH_UNCONFIGURED: "Publishing is not configured",
    ErrorCode.NAMESPACE_REFUSED: "Namespace refused",
    ErrorCode.ADMISSION_REJECTED: "Admission rejected",
    ErrorCode.RESOLUTION_FAILED: "Resolution failed",
    ErrorCode.CONTENT_NOT_FOUND: "Not found",
    ErrorCode.CAPABILITY_UNAVAILABLE: "Capability unavailable",
    ErrorCode.DOWNLOAD_DENIED: "Download denied",
    ErrorCode.VALIDATION_FAILED: "Validation failed",
    ErrorCode.INVALID_REQUEST: "Invalid request",
    ErrorCode.NOT_AUTHENTICATED: "Not authenticated",
    ErrorCode.NOT_AUTHORIZED: "Not authorized",
    ErrorCode.RATE_LIMITED: "Rate limited",
    ErrorCode.SUBMISSION_REJECTED: "Submission rejected",
    ErrorCode.CONFLICT: "Conflict",
    ErrorCode.METHOD_NOT_ALLOWED: "Method not allowed",
    ErrorCode.INTERNAL_ERROR: "Internal error",
}

#: What a status means when the framework — not a handler — produced it. Starlette raises a bare
#: ``HTTPException`` for an unrouted path and a wrong method, and a deployment behind this API can
#: raise one for anything else; every one of them still has to leave as a problem document.
_CODE_FOR_STATUS: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.NOT_AUTHENTICATED,
    403: ErrorCode.NOT_AUTHORIZED,
    404: ErrorCode.CONTENT_NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.CAPABILITY_UNAVAILABLE,
}


def status_code_for(code: ErrorCode) -> int:
    """The HTTP status *code* answers with."""
    return _STATUS[code]


class FieldProblem(BaseModel):
    """One field-level failure inside a :class:`Problem` — structured, so nobody parses prose."""

    #: Dotted path to what failed, from the request root: ``body.candidates.0.name``.
    field: str
    #: Why it failed, in words.
    message: str
    #: Pydantic's machine-readable reason (``missing``, ``int_parsing``, …), for a client that wants
    #: to render its own wording rather than the one this API chose.
    type: str


class Problem(BaseModel):
    """The one error document every surface answers with (RFC 9457; api.md §4).

    ``code`` identifies the failure and is the only member a client should switch on. ``detail`` is
    the message a person reads — **one string, always**, which is what lets a 422 render as words
    with no client-side flattening of an array.
    """

    #: The stable machine-readable identifier. Branch on this.
    code: ErrorCode
    #: A short, stable summary of the code. Not a substitute for ``code``.
    title: str
    #: The HTTP status, repeated in the body so a stored or logged problem stays self-describing.
    status: int
    #: The human-readable explanation. Never parsed.
    detail: str
    #: Field-level problems, when there are any. Always present, empty when there are none, so a
    #: client can iterate it unconditionally.
    errors: list[FieldProblem] = Field(default_factory=list)


class ApiError(HTTPException):
    """Raise this instead of ``HTTPException``: it names the failure rather than numbering it.

    The status comes from the code (:func:`status_code_for`) so a raise site cannot pair a reason
    with the wrong number. ``status_code`` overrides it only where an upstream refusal already
    chose one — Bench's :class:`~astro_mine.bench.leaderboard._service.SubmissionRejected` carries
    a status and no code, and the route maps between them.

    It subclasses ``HTTPException`` so that an app built without :func:`add_error_handlers` still
    answers the status the raise site meant, with FastAPI's default body. That is a worse response
    than a problem document, but it is the right *degradation*: a missing composition step should
    cost a client the ``code``, not turn every refusal into a 500.
    """

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        status_code: int | None = None,
        errors: list[FieldProblem] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            status_code=status_code if status_code is not None else _STATUS[code],
            detail=detail,
            headers=headers,
        )
        self.code = code
        self.errors = list(errors) if errors else []

    @property
    def problem(self) -> Problem:
        """The document this error leaves as."""
        return Problem(
            code=self.code,
            title=_TITLE[self.code],
            status=self.status_code,
            detail=str(self.detail),
            errors=self.errors,
        )


def _problem_response(problem: Problem, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json"),
        media_type=PROBLEM_MEDIA_TYPE,
        headers=headers,
    )


def _from_status(status_code: int, detail: str) -> Problem:
    """The document for a status nobody gave a code — the framework's own failures."""
    code = _CODE_FOR_STATUS.get(status_code)
    if code is None:
        code = ErrorCode.INTERNAL_ERROR if status_code >= 500 else ErrorCode.INVALID_REQUEST
    return Problem(code=code, title=_TITLE[code], status=status_code, detail=detail)


def _field(location: tuple[int | str, ...]) -> str:
    """Pydantic's ``loc`` tuple as a dotted path, root included: ``body.seeds.0``."""
    return ".".join(str(part) for part in location) or "body"


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return _problem_response(exc.problem, exc.headers)


async def _http_exception(request: Request, exc: Exception) -> Response:
    """A bare ``HTTPException``: Starlette's 404/405, or one from a dependency we do not own."""
    assert isinstance(exc, StarletteHTTPException)
    problem = _from_status(exc.status_code, str(exc.detail))
    return _problem_response(problem, getattr(exc, "headers", None))


async def _validation_error(request: Request, exc: Exception) -> Response:
    """FastAPI's array of ``{loc, msg}`` objects, folded into one object.

    This is the handler the front end's ``formatDetail()`` existed to compensate for. ``errors``
    carries the per-field data a form needs; ``detail`` is the same information as one sentence, so
    the simplest possible client renders words rather than ``[object Object]``.
    """
    assert isinstance(exc, RequestValidationError)
    fields = [
        FieldProblem(
            field=_field(tuple(error.get("loc", ()))),
            message=str(error.get("msg", "invalid")),
            type=str(error.get("type", "value_error")),
        )
        for error in exc.errors()
    ]
    summary = "; ".join(f"{field.field}: {field.message}" for field in fields)
    code = ErrorCode.VALIDATION_FAILED
    return _problem_response(
        Problem(
            code=code,
            title=_TITLE[code],
            status=_STATUS[code],
            detail=f"the request did not validate — {summary}" if summary else "invalid request",
            errors=fields,
        )
    )


async def _unhandled(request: Request, exc: Exception) -> Response:
    """The last arm: a handler raised something nobody anticipated.

    The client gets a problem document and nothing else — no message, no type, no traceback. What
    went wrong is in the deployment's logs, where Starlette has already put it, and a 500 is the one
    status whose ``detail`` must not be derived from the exception.
    """
    problem = _from_status(500, "the request could not be completed")
    return _problem_response(problem)


def only_problem_media_type(document: dict[str, Any]) -> dict[str, Any]:
    """Drop ``application/json`` from any response that also declares a problem document.

    :data:`ERROR_RESPONSES` declares both a ``model`` (so FastAPI generates and registers the
    ``Problem`` schema for us) and an explicit ``application/problem+json`` content entry (so the
    document names the media type the handlers actually send). FastAPI merges a ``model`` under the
    route's success media type — ``application/json`` — with no way to redirect it per response, so
    the generated document ends up advertising both. This drops the one that is not true.

    Mutates and returns *document*. A path item may carry keys that are not operations
    (``summary``, ``parameters``); FastAPI emits none today, and this skips them rather than finding
    out the hard way if that changes.
    """
    for operations in document.get("paths", {}).values():
        for verb, operation in operations.items():
            if verb.upper() not in _HTTP_VERBS:
                continue
            for response in operation.get("responses", {}).values():
                content = response.get("content") or {}
                if PROBLEM_MEDIA_TYPE in content:
                    content.pop("application/json", None)
    return document


def _describe_problem_responses(app: FastAPI) -> None:
    """Apply :func:`only_problem_media_type` to every document *app* generates."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        return only_problem_media_type(generate())

    app.openapi = openapi  # type: ignore[method-assign]


def add_error_handlers(app: FastAPI) -> FastAPI:
    """Install the error contract on *app* and return it.

    Applied by every app factory in this distribution, exactly as
    :func:`~astro_mine_api._cors.add_cors` is. Returns *app* so a factory can wrap its construction
    in one expression.

    The ``Exception`` handler is installed on Starlette's ``ServerErrorMiddleware``, which sits
    *outside* the CORS middleware — so a 500 reaches the browser without CORS headers and shows up
    there as a network error rather than as a readable problem. That is Starlette's ordering, not a
    choice made here; the four surfaces answer every *anticipated* failure through the handlers
    below, which run inside.
    """
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)
    _describe_problem_responses(app)
    return app


def _problem_content() -> dict[str, Any]:
    return {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}}


#: Declared on every router in this distribution. Two entries, because they do different jobs: the
#: explicit ``422`` is what stops FastAPI generating its ``HTTPValidationError`` array, and
#: ``default`` covers every other status a route can answer with — a client generated from this
#: document gets one error type for the whole API rather than a shape per route.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {
        "model": Problem,
        "description": "The request did not validate; `errors` carries the field-level detail.",
        "content": _problem_content(),
    },
    "default": {
        "model": Problem,
        "description": "A problem document. Branch on `code`.",
        "content": _problem_content(),
    },
}
