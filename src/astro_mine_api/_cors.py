"""Cross-origin access for the browser tier (api.md §4, §6; conventions.md §9).

The front end is a **static export**: the browser fetches the bundle from wherever it is hosted
and then calls this API directly, from a different origin. Without the middleware below, every one
of those calls fails at the preflight and the application is inert — which makes this a
prerequisite for the browser tier rather than a hardening pass.

**The policy lives here and only here.** Every app this distribution builds — the composed one and
each single-surface ``create_app`` — installs it through :func:`add_cors`, so a route test drives an
app that behaves like the deployed one in exactly the dimension it would otherwise silently differ.

Three decisions worth stating, because each is the kind that gets loosened by accident:

* **Credentials are never allowed.** Reads are account-free by design and nothing in the browser
  tier carries a cookie, so there is nothing for a cross-origin request to send. This is also what
  keeps a wildcard safe for a deployment that wants a public read API: ``*`` with credentials is
  the combination that turns any page on the internet into an authenticated client, and it cannot
  be reached from here.

  **A bearer token is not a credential in this sense, and conflating the two cost a release.**
  CORS "credentials" are cookies, TLS client certificates and HTTP auth state the *browser*
  attaches by itself; an ``Authorization`` header a page sets explicitly is an ordinary request
  header, governed by the allowlist below and unaffected by ``allow_credentials``. api#2 read the
  two as one thing, permitted ``content-type`` alone, and left Bench's whole authenticated write
  path unreachable from a browser — the preflight refused it before any route was reached. api#8
  added ``authorization`` to the allowlist; ``allow_credentials`` stayed ``False`` and must,
  because permitting it would be both unnecessary and the one change that makes ``*`` dangerous.
* **The allowlist is explicit.** Origins come from the environment, and the built-in default is the
  local development server and nothing else.
* **A request with no ``Origin`` header is untouched.** The CLI, server-to-server callers and
  ``curl`` see byte-identical behaviour to a deployment without this middleware — adding it imposes
  nothing on the local tier (CX-LOCAL, conventions.md §7.2 tier 1).
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

__all__ = [
    "CORS_ORIGINS_ENV",
    "DEFAULT_ORIGINS",
    "add_cors",
    "resolve_origins",
]

#: Env var holding the comma-separated allowlist (e.g. ``https://console.example.org``).
#: Unset ⇒ :data:`DEFAULT_ORIGINS`. Set but empty ⇒ no cross-origin access at all.
CORS_ORIGINS_ENV = "ASTRO_MINE_API_CORS_ORIGINS"

#: The development default: the front end's dev server, and nothing else.
#:
#: Both spellings are listed because a browser treats ``localhost`` and ``127.0.0.1`` as *different*
#: origins, and a developer who reaches the UI by the other name would otherwise get a preflight
#: failure that looks like a bug in this API.
DEFAULT_ORIGINS: tuple[str, ...] = ("http://localhost:3000", "http://127.0.0.1:3000")

#: Methods the browser tier actually uses. ``OPTIONS`` is the preflight itself.
_ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST", "DELETE", "OPTIONS")

#: Request headers a page may send. Starlette adds the CORS-safelisted four to whatever is here.
#:
#: ``content-type`` is why the write routes preflight at all: a JSON body is not a "simple
#: request", so the browser asks permission before sending one.
#:
#: ``authorization`` is what Bench's authenticated routes need in order to be callable — five of
#: them declare it, and Starlette fails a preflight outright on the first requested header it does
#: not find here, so omitting it did not weaken those routes, it removed them from the browser
#: entirely. It permits a header a page sets explicitly and has no bearing on ``allow_credentials``
#: (see the module docstring). Anything added to this tuple should be a header some route actually
#: reads; a speculative entry is a widened surface bought for nothing.
_ALLOWED_HEADERS: tuple[str, ...] = ("authorization", "content-type")


def resolve_origins(origins: str | None = None) -> list[str]:
    """The allowlist: *origins*, else :data:`CORS_ORIGINS_ENV`, else :data:`DEFAULT_ORIGINS`.

    Entries are comma-separated and stripped; blanks are dropped, and order is preserved with
    duplicates removed so a deployment's configuration reads back the way it was written.

    A **set but empty** value is honoured as an empty allowlist rather than falling back to the
    default — a deployment that deliberately turns cross-origin access off must be able to say so,
    and silently re-enabling the dev origins would be the wrong way to read a blank string.
    """
    raw = os.environ.get(CORS_ORIGINS_ENV) if origins is None else origins
    if raw is None:
        return list(DEFAULT_ORIGINS)
    seen: dict[str, None] = {}
    for part in raw.split(","):
        candidate = part.strip()
        if candidate:
            seen.setdefault(candidate, None)
    return list(seen)


def add_cors(app: FastAPI, origins: str | None = None) -> FastAPI:
    """Install the cross-origin policy on *app* and return it.

    Applied by every app factory in this distribution. Returns *app* so a factory can wrap its
    construction in one expression.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolve_origins(origins),
        allow_methods=list(_ALLOWED_METHODS),
        allow_headers=list(_ALLOWED_HEADERS),
        # Never true. See the module docstring: there is no browser credential to carry, and this is
        # what keeps an explicit `*` allowlist a safe choice rather than a dangerous one.
        allow_credentials=False,
    )
    return app
