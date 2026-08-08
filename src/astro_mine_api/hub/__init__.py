"""REST/OpenAPI façade over discovery, resolution, and the gated download boundary (RM-P1-HUB-02).

A thin FastAPI surface wiring HTTP to the domain modules — the hosted counterpart of the client
SDK (hub.md principle 7). :func:`create_app` injects the catalog/registry/audit, so the same app
runs on any backend and is TestClient-tested offline; :func:`build_router` is the same routes for
a deployment that serves more than one surface in a process.

Ported from ``astro_mine.hub.api`` (astro-mine-hub) — see :mod:`astro_mine_api.hub._app`.

Backlog: RM-P1-HUB-02 — astro-mine-hub#2
"""

from __future__ import annotations

from astro_mine_api.hub._app import PREFIX, build_router, create_app

__all__ = ["PREFIX", "build_router", "create_app"]
