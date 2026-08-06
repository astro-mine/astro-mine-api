"""Thin web edge over the design-orchestration library (studio.md §2 principle 8, §3).

The orchestration logic is an importable library in the platform; this FastAPI surface is a
*deployment* of it, not a separate codebase. Phase-1 exposes the local tier (intent capture +
synchronous study runs driving the in-process loop) so a single FastAPI process works on a laptop
(studio.md §7). SSE/WebSocket result streaming is deferred; the front end is the ``/design`` pages
of the one application, in ``astro-mine-ui``.

:mod:`astro_mine_api.studio.app` is the surface itself; :mod:`astro_mine_api.studio.serve` is the
composition that wires its →Hub / ←Hub seams against a local OCI-layout registry and mounts the
built UI.

Ported from ``astro_mine.studio.api`` (astro-mine-studio) and, for the composition, from that
repository's ``astro_mine.studio.cli``.
"""

from __future__ import annotations

from astro_mine_api.studio.app import (
    ASSET_STATIC_PREFIX,
    PREFIX,
    WORLD_STATIC_PREFIX,
    build_router,
    create_app,
    mount_static,
)
from astro_mine_api.studio.serve import SeamState, ServeReport, build_serve_app, render_banner

__all__ = [
    "ASSET_STATIC_PREFIX",
    "PREFIX",
    "WORLD_STATIC_PREFIX",
    "SeamState",
    "ServeReport",
    "build_router",
    "build_serve_app",
    "create_app",
    "mount_static",
    "render_banner",
]
