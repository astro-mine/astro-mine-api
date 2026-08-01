"""The FastAPI/OpenAPI 3.1 discovery + resolve façade (RM-P1-HUB-02; hub.md §3, §4).

A **thin** REST surface over the domain modules — the heavy lifting is in
:mod:`~astro_mine.hub.index`/:mod:`~astro_mine.hub.search`/:mod:`~astro_mine.hub.resolve`/
:mod:`~astro_mine.hub.policy`; this layer only wires HTTP to them (hub.md principle 7 "library
first, service second"). :func:`create_app` is a factory taking an injected catalog (+ optional
registry for attestation status and audit log), so it runs against any backend and is exercised
offline via Starlette's ``TestClient``.

Endpoints, all under the ``/hub`` prefix: ``/hub/health``; ``POST /hub/publish`` (index a Core
manifest); ``GET /hub/search`` (faceted + full-text + semantic); ``GET
/hub/artifacts/{name}/{version}`` (record + attestations); ``POST /hub/resolve`` (pinned-digest
resolution); ``POST /hub/artifacts/{name}/{version}/download`` (the gated boundary — 403, no
digest, when policy denies).

Ported from ``astro_mine.hub.api._app`` (astro-mine-hub) unchanged but for the import paths and
the component prefix: same handlers, same bodies, same status codes.
"""

from __future__ import annotations

from typing import Any

from astro_mine.core.registry import PluginManifest
from astro_mine.hub import __version__
from astro_mine.hub.index import Catalog, CatalogEntry
from astro_mine.hub.policy import (
    DEFAULT_ALLOWED_LICENSES,
    AuditLog,
    DownloadRequest,
    GatedDownload,
    PolicyEngine,
    gate,
)
from astro_mine.hub.registry import RegistryClient
from astro_mine.hub.resolve import ResolutionError, ResolutionRequest, resolve
from astro_mine.hub.search import SearchQuery, search
from astro_mine.hub.supply_chain import SupplyChainError, admit
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from astro_mine_api._cors import add_cors
from astro_mine_api._ids import unique_operation_id

__all__ = ["PREFIX", "build_router", "create_app"]

#: The component prefix every Hub route is served under.
PREFIX = "/hub"


class PublishBody(BaseModel):
    """Index a Core plugin manifest for an already-stored artifact."""

    manifest: dict[str, Any]
    digest: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    namespace: str = "open"


class ResolveBody(BaseModel):
    """A resolution constraint set."""

    name: str
    version_spec: str = ""
    interfaces: dict[str, str] | None = None
    capability_tags: list[str] = Field(default_factory=list)


class DownloadBody(BaseModel):
    """What a requester presents at the gated download boundary."""

    grants: list[str] = Field(default_factory=list)
    allowed_licenses: list[str] | None = None
    require_verified: bool = False


def _hit(entry: CatalogEntry, score: float) -> dict[str, Any]:
    return {
        "reference": entry.reference,
        "digest": entry.digest,
        "name": entry.name,
        "version": entry.version,
        "kind": entry.kind,
        "artifact_kind": entry.artifact_kind,
        "license": entry.license,
        "namespace": entry.namespace,
        "publisher": entry.publisher,
        "deprecated": entry.deprecated,
        "yanked": entry.yanked,
        "score": score,
    }


def _detail(entry: CatalogEntry, registry: RegistryClient | None) -> dict[str, Any]:
    detail = _hit(entry, 1.0)
    detail["record"] = entry.record.model_dump(mode="json")
    detail["attestations"] = (
        sorted(
            {desc.artifact_type for desc in registry.referrers(entry.digest) if desc.artifact_type}
        )
        if registry is not None
        else []
    )
    return detail


def build_router(
    catalog: Catalog,
    *,
    registry: RegistryClient | None = None,
    audit: AuditLog | None = None,
    engine: PolicyEngine | None = None,
) -> APIRouter:
    """The Hub routes, prefixed with ``/hub``, over an injected ``catalog``.

    The routes live on a router rather than straight on an app so a deployment can serve this
    surface alongside the other three in one process (api.md §6); :func:`create_app` is the
    single-surface deployment of the same router.
    """
    router = APIRouter(prefix=PREFIX)

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @router.post("/publish")
    def publish(body: PublishBody) -> dict[str, Any]:
        """Index an already-stored artifact — after proving the caller's claims about it.

        Every field in the body is a *claim*: the digest, the manifest, and the namespace. This
        endpoint used to take all three on the caller's word, which let a request forge content
        provenance rather than merely omit it. Admission now re-derives each from the registry
        (hub.md §2.3), and a caller-asserted trust tier above ``open`` is refused outright —
        promotion is a curated, audited action (hub.md §9), never a field in a publish request.
        """
        if registry is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "publishing is not configured on this deployment: admission requires a "
                    "registry to verify against, and indexing without one cannot be fail-closed"
                ),
            )
        if body.namespace != "open":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"namespace {body.namespace!r} cannot be claimed at publish; artifacts are "
                    f"admitted to 'open' and reach a trusted tier only through an audited "
                    f"promotion that verifies their evidence"
                ),
            )
        manifest = PluginManifest.model_validate(body.manifest)
        try:
            entry = admit(
                registry,
                catalog,
                manifest,
                digest=body.digest,
                publisher=body.publisher,
                namespace="open",
            )
        except SupplyChainError as exc:
            # Fail closed and say why: a rejected publish is an integrity verdict the caller needs
            # to act on, not an opaque 500.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _hit(entry, 1.0)

    @router.get("/search", operation_id="hub_search")
    def do_search(
        text: str | None = None,
        semantic: str | None = None,
        kind: str | None = None,
        artifact_kind: str | None = None,
        license: str | None = None,
        namespace: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        query = SearchQuery(
            text=text,
            semantic=semantic,
            kind=kind,
            artifact_kind=artifact_kind,
            license=license,
            namespace=namespace,
            limit=limit,
        )
        return [_hit(result.entry, result.score) for result in search(catalog, query)]

    @router.get("/artifacts/{name}/{version}", operation_id="hub_get_artifact")
    def artifact(name: str, version: str) -> dict[str, Any]:
        entry = catalog.get(f"{name}:{version}")
        if entry is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        return _detail(entry, registry)

    @router.post("/resolve", operation_id="hub_resolve")
    def do_resolve(body: ResolveBody) -> dict[str, str]:
        request = ResolutionRequest(
            name=body.name,
            version_spec=body.version_spec,
            interfaces=body.interfaces,
            capability_tags=body.capability_tags,
        )
        try:
            resolution = resolve(catalog, request)
        except ResolutionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        primary = resolution.primary
        return {
            "reference": primary.reference,
            "digest": primary.digest,
            "version": primary.version,
        }

    @router.post("/artifacts/{name}/{version}/download")
    def download(name: str, version: str, body: DownloadBody) -> dict[str, str]:
        entry = catalog.get(f"{name}:{version}")
        if entry is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        request = DownloadRequest(
            grants=frozenset(body.grants),
            allowed_licenses=(
                frozenset(body.allowed_licenses)
                if body.allowed_licenses is not None
                else DEFAULT_ALLOWED_LICENSES
            ),
            require_verified=body.require_verified,
        )
        try:
            decision = gate(entry, request, audit=audit, engine=engine)
        except GatedDownload as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        # The policy version travels with the grant: a consumer can record which rules let it in.
        return {
            "digest": entry.digest,
            "policy_version": decision.policy_version,
            "policy_engine": decision.engine,
        }

    return router


def create_app(
    catalog: Catalog,
    *,
    registry: RegistryClient | None = None,
    audit: AuditLog | None = None,
    engine: PolicyEngine | None = None,
) -> FastAPI:
    """Build the Hub REST app over an injected ``catalog`` (+ optional registry/audit/engine).

    ``engine`` selects the download-gating evaluator — the pure-Python default, or an
    :class:`~astro_mine.hub.policy.OpaPolicyEngine` over the versioned Rego bundle when the hosted
    tier runs OPA (``opa_engine_from_env()``); the gate's behaviour is identical either way.
    """
    app = FastAPI(
        title="Astro-Mine Hub",
        version=__version__,
        generate_unique_id_function=unique_operation_id,
    )
    # The browser tier calls this API cross-origin (_cors.py). Applied here as well as in
    # the composed app so a route test drives an app that behaves like the deployed one.
    add_cors(app)
    app.include_router(build_router(catalog, registry=registry, audit=audit, engine=engine))
    return app
