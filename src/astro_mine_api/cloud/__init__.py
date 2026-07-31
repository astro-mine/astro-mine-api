"""The REST submission edge -- a FastAPI front door to the control plane.

The gRPC/REST submission edge (``cloud.md`` §3, §6): a thin HTTP surface over the *same*
``submit()`` / engine paths the CLI uses -- it adds a transport, not a capability, so a job
submitted over REST runs the identical container as one submitted in-process (no code fork).

Run it with an ASGI server, e.g. ``uvicorn --factory astro_mine_api.cloud.app:create_app``.

Ported from ``astro_mine.cloud.serve`` (astro-mine-cloud) -- see :mod:`astro_mine_api.cloud.app`.

Backlog: RM-P1-CLOUD-02 -- https://github.com/astro-mine/astro-mine-cloud/issues/13
"""

from __future__ import annotations

from astro_mine_api.cloud.app import PREFIX, build_router, create_app

__all__ = ["PREFIX", "build_router", "create_app"]
