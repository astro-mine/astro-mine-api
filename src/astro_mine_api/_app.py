"""Composition — mount the surfaces a deployment enables (api.md §3, §6).

One image, one process, any subset of the four surfaces. Because every route is served under its
owning component's prefix (``/hub``, ``/studio``, ``/cloud``, ``/bench``), composing them is
inclusion rather than routing: the surfaces cannot collide, and a client's URL is the same whether
it reached a single-surface deployment or all four behind one address.

Which surfaces are enabled comes from :data:`SURFACES_ENV` — a comma-separated list of component
names, defaulting to every surface. Each is wired from the environment through the same variables
its component repository always used (``HUB_POSTGRES_URL``, ``ASTRO_MINE_BENCH_DB``,
``ASTRO_MINE_HUB_REGISTRY``, …), so a deployment that ran one of these services keeps its
configuration.

Run it with an ASGI server::

    uvicorn --factory astro_mine_api._app:make_app

**The local tier does not need this** (api.md §6, CX-LOCAL): Hub's tier-1 client, Bench's local
scoring, Cloud's local backend and Studio's library API all work with no service running.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from fastapi import FastAPI

from astro_mine_api import __version__
from astro_mine_api._cors import add_cors

__all__ = [
    "SURFACES",
    "SURFACES_ENV",
    "build_app",
    "enabled_surfaces",
    "make_app",
]

#: Env var naming the surfaces a deployment enables, comma-separated (e.g. ``hub,bench``).
#: Unset or empty ⇒ all four.
SURFACES_ENV = "ASTRO_MINE_API_SURFACES"

#: Every surface this distribution can serve, in the order they mount.
SURFACES: tuple[str, ...] = ("hub", "studio", "cloud", "bench")


def enabled_surfaces(names: Iterable[str] | None = None) -> tuple[str, ...]:
    """The surfaces to mount: *names*, else :data:`SURFACES_ENV`, else all of them.

    Names are lower-cased and stripped, and the result keeps :data:`SURFACES` order so a
    deployment's OpenAPI document does not reshuffle when someone reorders an env var. An
    unrecognised name is an error at composition time rather than a silently missing surface —
    the failure mode a typo in a deployment manifest must have.
    """
    if names is None:
        raw = os.environ.get(SURFACES_ENV, "")
        names = [part for part in raw.split(",") if part.strip()]
        if not names:
            return SURFACES
    requested = {name.strip().lower() for name in names}
    unknown = sorted(requested - set(SURFACES))
    if unknown:
        raise ValueError(
            f"unknown API surface(s) {', '.join(unknown)}; known surfaces are {', '.join(SURFACES)}"
        )
    return tuple(name for name in SURFACES if name in requested)


def build_app(surfaces: Iterable[str] | None = None) -> FastAPI:
    """Compose the enabled surfaces into one app, each wired from the environment.

    Every surface is imported *only* if it is enabled: a Hub-only deployment does not pay Studio's
    import cost, and a broken optional backend in a surface nobody enabled cannot fail the
    process.
    """
    names = enabled_surfaces(surfaces)
    app = FastAPI(
        title="Astro-Mine-API",
        version=__version__,
        summary="The Astro-Mine REST tier: the Hub registry API, the Studio API, Cloud's "
        "submission edge and the Bench leaderboard, behind one deployable.",
    )
    # The front end is a static export, so the browser calls this API from another origin
    # (_cors.py). Installed before the routes so every surface this deployment mounts is covered
    # by one policy rather than four.
    add_cors(app)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, object]:
        """Liveness for the deployment as a whole, naming what it serves.

        Each surface keeps its own health endpoint under its own prefix — this one answers "is
        this process up, and which surfaces did it mount?", which is the question a load balancer
        in front of a multi-surface deployment actually asks.
        """
        return {"status": "ok", "version": __version__, "surfaces": list(names)}

    for name in names:
        _mount(app, name)

    return app


def _mount(app: FastAPI, name: str) -> None:
    """Attach one surface, wiring its backends from the environment."""
    if name == "hub":
        from astro_mine.hub.index import sql_catalog

        from astro_mine_api.hub import build_router as hub_router
        from astro_mine_api.hub._asgi import POSTGRES_URL_ENV

        url = os.environ.get(POSTGRES_URL_ENV, "sqlite+pysqlite:///:memory:")
        app.include_router(hub_router(sql_catalog(url)))

    elif name == "studio":
        from astro_mine_api.studio import build_router as studio_router
        from astro_mine_api.studio import mount_static
        from astro_mine_api.studio.serve import (
            DEFAULT_SIGNING_KEY_NAMES,
            DEFAULT_TRUSTED_KEY_NAMES,
            SIGNING_KEY_ENV,
            TRUSTED_KEY_ENV,
            _wire_hub_seams,
            resolve_cache_dir,
            resolve_key,
            resolve_registry,
        )

        registry = resolve_registry(None)
        if registry is None:
            # No registry → the 5 Hub-backed routes 503, honestly (studio.md §6). The surface
            # still captures intent and runs studies, which is the whole local tier.
            app.include_router(studio_router())
            return
        cache_dir = resolve_cache_dir(None)
        kwargs, _seams = _wire_hub_seams(
            registry,
            resolve_key(None, TRUSTED_KEY_ENV, registry, DEFAULT_TRUSTED_KEY_NAMES),
            resolve_key(None, SIGNING_KEY_ENV, registry, DEFAULT_SIGNING_KEY_NAMES),
            cache_dir,
        )
        mount_static(
            app,
            world_cache_dir=str(kwargs["world_cache_dir"]),
            asset_cache_dir=str(kwargs["asset_cache_dir"]),
        )
        app.include_router(
            studio_router(
                publisher=kwargs.get("publisher"),  # type: ignore[arg-type]
                materializer=kwargs["materializer"],  # type: ignore[arg-type]
                catalog=kwargs["catalog"],  # type: ignore[arg-type]
                preview_materializer=kwargs["preview_materializer"],  # type: ignore[arg-type]
            )
        )

    elif name == "cloud":
        from astro_mine_api.cloud import build_router as cloud_router

        app.include_router(cloud_router())

    else:  # "bench" — enabled_surfaces() already rejected anything else
        from astro_mine_api.bench import build_router as bench_router

        app.include_router(bench_router())


def make_app() -> FastAPI:
    """ASGI factory: ``uvicorn --factory astro_mine_api._app:make_app``."""
    return build_app()
