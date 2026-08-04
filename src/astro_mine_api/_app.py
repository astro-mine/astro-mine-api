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
from astro_mine_api._errors import ERROR_RESPONSES, add_error_handlers
from astro_mine_api._health import Health, health
from astro_mine_api._ids import unique_operation_id

__all__ = [
    "HUB_REGISTRY_ENV",
    "IN_MEMORY_CATALOG_URL",
    "SURFACES",
    "SURFACES_ENV",
    "build_app",
    "enabled_surfaces",
    "hub_catalog_url",
    "make_app",
]

#: Env var naming the surfaces a deployment enables, comma-separated (e.g. ``hub,bench``).
#: Unset or empty ⇒ all four.
SURFACES_ENV = "ASTRO_MINE_API_SURFACES"

#: Every surface this distribution can serve, in the order they mount.
SURFACES: tuple[str, ...] = ("hub", "studio", "cloud", "bench")

#: The local OCI-layout registry three of the four surfaces read.
#:
#: Spelled here as well as in ``studio.serve`` and ``bench._app`` — which each carry their own
#: constant for the same variable, inherited from the component repositories they were ported from —
#: because importing either of theirs from this module would drag a surface's package in on a
#: deployment that did not enable it, and "every surface is imported *only* if it is enabled" is a
#: property :func:`build_app` promises a few lines below.
HUB_REGISTRY_ENV = "ASTRO_MINE_HUB_REGISTRY"


class DeploymentHealth(Health):
    """Liveness for the deployment as a whole, naming what it mounted.

    The surface health shape plus one field, rather than a shape of its own: a probe or a status
    page that reads ``/hub/healthz`` reads this the same way, and only has to know about
    ``surfaces`` if it cares which ones this process serves.
    """

    surfaces: list[str]


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
        generate_unique_id_function=unique_operation_id,
    )
    # The front end is a static export, so the browser calls this API from another origin
    # (_cors.py), and every refusal leaves as one problem document (_errors.py). Both installed
    # before the routes so every surface this deployment mounts is covered by one policy and one
    # error contract rather than four.
    add_cors(app)
    add_error_handlers(app)

    @app.get("/healthz", tags=["meta"], responses=ERROR_RESPONSES)
    def healthz() -> DeploymentHealth:
        """Liveness for the deployment as a whole, naming what it serves.

        Each surface answers ``/healthz`` under its own prefix — this one answers "is this process
        up, and which surfaces did it mount?", which is the question a load balancer in front of a
        multi-surface deployment actually asks.
        """
        return DeploymentHealth(**health("api").model_dump(), surfaces=list(names))

    for name in names:
        _mount(app, name)

    return app


#: The Hub catalog's in-memory fallback, as a **shared-cache** SQLite URI (api#15).
#:
#: ``sqlite+pysqlite:///:memory:`` looks like the obvious spelling and is not one. SQLAlchemy serves
#: a ``:memory:`` SQLite URL from a ``SingletonThreadPool`` — one connection *per thread* — and
#: every SQLite in-memory connection is its own private database. So ``SqlCatalog``'s
#: ``create_all`` ran on the constructing thread, and every request, served on a worker thread,
#: arrived at an empty database:
#:
#:     sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) no such table: catalog
#:
#: which made ``GET /hub/search`` answer 500 on every deployment that did not set
#: ``HUB_POSTGRES_URL`` — that is to say, on the default one. A shared-cache URI names one database
#: that every connection in the process attaches to, so the fallback now behaves the way
#: ``hub/_asgi.py`` has always claimed it does: in-memory, process-lifetime, no configuration.
#:
#: It is still in-memory, and that is still the right *default* rather than the right deployment:
#: the catalog is an index over a registry, so losing it on restart loses no content. A deployment
#: that wants it durable sets ``HUB_POSTGRES_URL``, which is the variable this reads first.
IN_MEMORY_CATALOG_URL = (
    "sqlite+pysqlite:///file:astro_mine_hub_catalog?mode=memory&cache=shared&uri=true"
)


def hub_catalog_url() -> str:
    """The Hub catalog's database URL: ``HUB_POSTGRES_URL``, else the shared in-memory fallback."""
    from astro_mine_api.hub._asgi import POSTGRES_URL_ENV

    return os.environ.get(POSTGRES_URL_ENV) or IN_MEMORY_CATALOG_URL


def _mount(app: FastAPI, name: str) -> None:
    """Attach one surface, wiring its backends from the environment."""
    if name == "hub":
        from astro_mine.hub.index import sql_catalog
        from astro_mine.hub.registry import Registry

        from astro_mine_api.hub import build_router as hub_router

        # **The registry, not only the catalog** (api#16). `build_router` takes both, and without
        # the registry the surface silently loses two things: every artifact reports
        # `attestations: []` — because they are read as OCI referrers off the registry, not off the
        # index — and `POST /hub/publish` refuses with 503 forever, because admission has nothing to
        # verify against and correctly declines to index on a caller's word. Neither failure looks
        # like missing configuration from the outside; the first looks like content with no
        # evidence, which is precisely the impression `ui.md` §7 rule 6 exists to prevent.
        #
        # `ASTRO_MINE_HUB_REGISTRY` is the same variable Studio and Bench already read, so a
        # deployment that pointed those at a registry gets this with no new configuration; a
        # deployment that set nothing keeps exactly today's behaviour.
        registry_path = os.environ.get(HUB_REGISTRY_ENV)
        app.include_router(
            hub_router(
                sql_catalog(hub_catalog_url()),
                registry=Registry(registry_path) if registry_path else None,
            )
        )

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
