"""The OpenAPI document, normalised for comparison (api#3).

The document is the contract the front end's generated client is built from, so it is snapshotted
and gated — see ``tests/test_openapi_contract.py`` and ``scripts/update_openapi_snapshot.py``, which
both call :func:`current_document` so there is one definition of what "the document" means.

**The version is normalised out.** ``info.version`` comes from ``hatch-vcs``, so it is derived from
whatever git history the build happened to see — ``0.1.dev1`` in a shallow checkout, something else
on a runner with different tag depth. Snapshotting it would produce a test that passes on the
machine that wrote it and fails everywhere else: a test measuring the checkout rather than the API.
"""

from __future__ import annotations

import json
from typing import Any

from astro_mine_api._app import build_app

__all__ = ["VERSION_PLACEHOLDER", "current_document"]

#: Substituted for the build-derived ``info.version``. See the module docstring.
VERSION_PLACEHOLDER = "<version>"


def current_document() -> dict[str, Any]:
    """The composed app's OpenAPI document, normalised so two machines agree on it.

    Round-tripped through JSON so the result is plain data — the same thing a client generator
    reads — rather than the objects FastAPI happens to build it from.
    """
    document: dict[str, Any] = json.loads(json.dumps(build_app().openapi()))
    document["info"]["version"] = VERSION_PLACEHOLDER
    return document
