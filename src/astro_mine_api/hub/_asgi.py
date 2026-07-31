"""ASGI entrypoint for the hosted Hub — ``uvicorn --factory astro_mine_api.hub._asgi:make_app``.

Builds the FastAPI app over a **PostgreSQL**-backed catalog (``HUB_POSTGRES_URL``) for the docker
compose / cluster tier; falls back to in-memory SQLite when unset, so the entrypoint imports and
runs with no database (hub.md §7). The catalog is the only backend the app needs injected — the
registry/audit wiring is layered on by the deployment.

Ported from ``astro_mine.hub.api._asgi`` (astro-mine-hub) unchanged but for the import paths.
"""

from __future__ import annotations

import os

from astro_mine.hub.index import sql_catalog
from fastapi import FastAPI

from astro_mine_api.hub._app import create_app

__all__ = ["POSTGRES_URL_ENV", "make_app"]

#: Env var selecting the hosted catalog's database; unset falls back to in-memory SQLite.
POSTGRES_URL_ENV = "HUB_POSTGRES_URL"


def make_app() -> FastAPI:
    """Construct the hosted Hub app over ``HUB_POSTGRES_URL`` (or in-memory SQLite if unset)."""
    url = os.environ.get(POSTGRES_URL_ENV, "sqlite+pysqlite:///:memory:")
    return create_app(sql_catalog(url))
