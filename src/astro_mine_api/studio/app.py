"""FastAPI application factory for the Studio local tier.

Ported from ``astro_mine.studio.api.app`` (astro-mine-studio) unchanged but for the import paths
and the ``/studio`` component prefix. The two static mounts moved under the prefix with the
routes, so the URLs the responses hand View (``manifest_url``, ``document_url``) stay consistent
with wherever the mount actually is — they are built from the same constants.
"""

from __future__ import annotations

import json
from pathlib import Path

from astro_mine.core.objective import ObjectiveDocument, ObjectiveError
from astro_mine.core.registry import CapabilityTag
from astro_mine.studio import __version__
from astro_mine.studio._base import StudioModel
from astro_mine.studio.campaign import author_campaign, freeze_campaign
from astro_mine.studio.compare import ComparisonView, build_comparison
from astro_mine.studio.designspace import build_trade_study

# The →Hub seams are typed Protocols, imported for annotation only: the API layer never imports
# `astro_mine.hub`, so the library keeps its no-sibling-imports guarantee (studio.md §2).
from astro_mine.studio.hub import (
    ArtifactPublisher,
    AssetPreviewMaterializer,
    MenuEntry,
    PublishedArtifactRef,
    StudioCatalog,
    WorldEntry,
    WorldMaterializer,
)
from astro_mine.studio.intent import (
    CapturedObjective,
    MetricVocabulary,
    ObjectiveGateError,
    capture_intent,
)
from astro_mine.studio.models import (
    Campaign,
    CampaignPhase,
    DesignCandidate,
    EvaluatedCandidate,
    IntentDraft,
    TradeStudy,
)
from astro_mine.studio.orchestrate import (
    InMemoryJobStore,
    InMemoryResultCache,
    JobRecord,
    JobStatus,
    LocalDispatcher,
    SiblingClients,
    local_clients,
    run_batch,
)
from astro_mine.studio.workspace import InMemoryWorkspace, WorkspaceStore
from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError

from astro_mine_api._cors import add_cors

__all__ = [
    "ASSET_STATIC_PREFIX",
    "PREFIX",
    "WORLD_STATIC_PREFIX",
    "build_router",
    "create_app",
    "mount_static",
]

#: The component prefix every Studio route is served under.
PREFIX = "/studio"

#: Where the materialized world bundles are served from, so `<GlobeScene world={{ manifestUrl }}>`
#: can fetch `world.json` and its tileset. The Phase-2 View Gateway's tiles proxy does not
#: exist yet.
WORLD_STATIC_PREFIX = f"{PREFIX}/worlds/files"

#: Where materialized asset previews are served from, so `<AssetPreview source={{ documentUrl }}>`
#: fetches the SADF JSON and Cesium fetches the glTF beside it. Sibling of the world mount.
ASSET_STATIC_PREFIX = f"{PREFIX}/assets/files"


class CaptureRequest(StudioModel):
    draft: IntentDraft
    vocabulary: MetricVocabulary | None = None
    model: str | None = None


class StudyRequest(StudioModel):
    objective: ObjectiveDocument
    candidates: list[DesignCandidate]
    seeds: list[int] = Field(default_factory=lambda: [0])
    max_steps: int = 8


class StudyResponse(StudioModel):
    #: Per-candidate/seed job bookkeeping — surfaces a failure honestly rather than hiding it.
    jobs: list[JobRecord]
    #: The reproducible trade study assembled from the evaluated candidates: what the comparison and
    #: publish steps consume. ``None`` only when every candidate failed (see ``jobs`` for why).
    study: TradeStudy | None = None


class PublishCampaignRequest(StudioModel):
    """Publish a campaign. Provide a fully-formed ``campaign``, **or** the authoring-journey form —
    a ``chosen`` evaluated candidate + its ``objective`` (+ optional ``phases``) — which the route
    authors into a ``Campaign`` server-side (proper lineage via :func:`author_campaign`) before
    freezing and publishing. Either shape, one route, zero new endpoints."""

    name: str
    version: str
    campaign: Campaign | None = None
    objective: ObjectiveDocument | None = None
    chosen: EvaluatedCandidate | None = None
    phases: list[CampaignPhase] = Field(
        default_factory=lambda: [CampaignPhase(id="phase-1", name="Prospect")]
    )
    #: The world the design was inspected against, so the published campaign records it.
    world_ref: str | None = None


class WorldSite(StudioModel):
    """Where on a world a design-time swarm is laid out: the bundle's own tileset anchor.

    Read straight out of the verified bundle's ``world.json`` (``crs`` + ``tiles_anchor``), never
    chosen by Studio — a design has no run, so it has no simulated poses, and inventing coordinates
    silently is exactly what `studio.md` §2 principle 7 forbids. The anchor is the one position in a
    world bundle that means "here is where this terrain is", so a layout centred on it is the one
    convention that needs no new authored input (studio#50).

    ``None`` on the response when the bundle predates the published anchor: the swarm then cannot be
    placed, and the surface says so rather than guessing.
    """

    body: str
    #: The body-fixed frame the anchor is expressed in (`tiles_anchor.frame`).
    frame: str
    reference_radius_m: float
    latitude_deg: float
    longitude_deg: float
    #: Height above the CRS reference sphere — the same datum `tiles_anchor` uses.
    height_m: float


class WorldResponse(StudioModel):
    """A world Studio pulled from Hub by digest and is now serving to the embedded View."""

    reference: str
    digest: str
    world_id: str
    #: The URL `<GlobeScene>` fetches. Studio serves bytes it verified; it authored none of them.
    manifest_url: str
    #: The bundle's tileset anchor, for laying a design-time swarm out on it — see
    #: :class:`WorldSite`.
    site: WorldSite | None = None


class AssetPreviewResponse(StudioModel):
    """An asset Studio pulled from Hub by digest and is now serving to the embedded View."""

    reference: str
    digest: str
    #: The URL `<AssetPreview source={{ documentUrl }}>` fetches — the SADF JSON, with its glTF
    #: geometry served beside it. Studio serves bytes it verified; it authored none of them.
    document_url: str


def _read_world_site(manifest_path: Path) -> WorldSite | None:
    """The tileset anchor from a materialized bundle's ``world.json``, or ``None``.

    A read of two published fields, not an interpretation of the bundle: `crs` and `tiles_anchor`
    are exactly the objects Worlds validates against Core's units schema before publishing
    (RFC-0007), and View reads the same anchor to place the terrain. Anything missing or malformed
    yields ``None`` — a design-time layout is a convenience, so an old bundle must degrade to "the
    swarm cannot be placed", never to a guessed position or a 500.
    """
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        crs = manifest["crs"]
        anchor = manifest["tiles_anchor"]
        origin = anchor["origin"]
        frame = anchor["frame"]
        return WorldSite(
            body=str(crs["body"]),
            # `tiles_anchor.frame` is a bare frame name in older bundles and an RFC-0007 structured
            # object in newer ones; View handles both, so this does too.
            frame=str(frame if isinstance(frame, str) else frame["name"]),
            reference_radius_m=float(crs["reference_radius_m"]),
            latitude_deg=float(origin["latitude_deg"]),
            longitude_deg=float(origin["longitude_deg"]),
            height_m=float(origin["height_m"]),
        )
    except (OSError, ValueError, TypeError, KeyError, ValidationError):
        return None


def mount_static(
    app: FastAPI,
    *,
    world_cache_dir: str | None = None,
    asset_cache_dir: str | None = None,
) -> None:
    """Mount the verified, digest-keyed content caches the Studio routes hand View URLs into.

    A mount is an app-level operation, not a route, so it cannot live on the router — this is the
    one piece of the Studio surface a multi-surface deployment has to attach itself
    (:func:`astro_mine_api.create_app` does).
    """
    if world_cache_dir is not None:
        # Serve the verified, digest-keyed cache. Read-only static bytes; Studio renders nothing.
        app.mount(WORLD_STATIC_PREFIX, StaticFiles(directory=world_cache_dir), name="worlds")

    if asset_cache_dir is not None:
        # The materialized asset previews, served the same way the world bundles are.
        app.mount(ASSET_STATIC_PREFIX, StaticFiles(directory=asset_cache_dir), name="assets")


def build_router(
    *,
    workspace: WorkspaceStore | None = None,
    clients: SiblingClients | None = None,
    publisher: ArtifactPublisher | None = None,
    materializer: WorldMaterializer | None = None,
    catalog: StudioCatalog | None = None,
    preview_materializer: AssetPreviewMaterializer | None = None,
) -> APIRouter:
    """The Studio routes, prefixed with ``/studio``, driving the injected (default all-local)
    workspace and sibling clients.

    ``publisher``/``materializer`` and ``catalog``/``preview_materializer`` are the Hub seams
    (RM-P1-STUDIO-06, -09). They are optional because the deployment may not have a registry to
    resolve from: without them the app still captures intent and runs studies, and the
    publish/terrain/catalog routes answer 503 rather than pretending.
    """
    store: WorkspaceStore = workspace if workspace is not None else InMemoryWorkspace()
    siblings: SiblingClients = clients if clients is not None else local_clients()
    router = APIRouter(prefix=PREFIX)

    def _require(seam: object, name: str) -> None:
        if seam is None:
            raise HTTPException(
                status_code=503,
                detail=f"{name} is unavailable: Studio was built without the [hub] extra",
            )

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.post("/intent")
    def capture(request: CaptureRequest) -> CapturedObjective:
        try:
            return capture_intent(
                request.draft,
                workspace=store,
                vocabulary=request.vocabulary,
                model=request.model,
            )
        except (ObjectiveGateError, ObjectiveError, ValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/studies")
    async def run_study(request: StudyRequest) -> StudyResponse:
        # run_batch evaluates each candidate/seed and, given a cache, retains the EvaluatedCandidate
        # it computes (keyed by the content-addressed cache_key on each JobRecord). We read those
        # back and assemble the reproducible TradeStudy the comparison + publish steps consume — the
        # bridge the journey needs, with no new route.
        cache = InMemoryResultCache()
        records = await run_batch(
            request.candidates,
            request.objective,
            dispatcher=LocalDispatcher(siblings),
            seeds=tuple(request.seeds),
            store=InMemoryJobStore(),
            cache=cache,
            max_steps=request.max_steps,
        )
        evaluated = [
            cache.get(record.cache_key)
            for record in records
            if record.status is JobStatus.SUCCEEDED and cache.has(record.cache_key)
        ]
        study = (
            build_trade_study(
                evaluated,
                request.objective,
                backend="batch",
                # What actually scored the candidates, from the injected bundle — not a guess made
                # here. The id rides the artifact so the comparison view and any published campaign
                # can say whether physics ran.
                evaluator=siblings.evaluator,
                seeds=request.seeds,
                extra_input_hashes=[candidate.digest() for candidate in request.candidates],
            )
            if evaluated
            else None
        )
        return StudyResponse(jobs=records, study=study)

    @router.post("/studies/comparison")
    def comparison(study: TradeStudy) -> ComparisonView:
        """The Pareto front with per-metric uncertainty — bounds, not bare point estimates."""
        return build_comparison(study)

    def _resolve_campaign(request: PublishCampaignRequest) -> Campaign:
        """A fully-formed campaign, or one authored from the journey's chosen candidate."""
        if request.campaign is not None:
            return request.campaign
        if request.chosen is not None and request.objective is not None:
            return author_campaign(
                request.objective,
                request.chosen,
                name=request.name,
                phases=request.phases,
                world_ref=request.world_ref,
            )
        raise HTTPException(
            status_code=422,
            detail="provide either `campaign`, or `chosen` + `objective` to author one",
        )

    @router.post("/campaigns/publish")
    def publish_campaign(request: PublishCampaignRequest) -> PublishedArtifactRef:
        """Freeze and publish a campaign to Hub as a signed, content-addressed artifact."""
        _require(publisher, "publishing")
        assert publisher is not None
        campaign = _resolve_campaign(request)  # 422 (not 409) when the request shape is invalid
        try:
            bundle = freeze_campaign(campaign)
            return publisher.publish_campaign(bundle, name=request.name, version=request.version)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/campaigns/{reference:path}")
    def pull_campaign(reference: str) -> Campaign:
        """Pull a published campaign back by reference or digest, re-verified before trusted."""
        _require(publisher, "publishing")
        assert publisher is not None
        try:
            return publisher.pull_campaign(reference)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/worlds/{reference:path}")
    def resolve_world(reference: str) -> WorldResponse:
        """Materialize a Worlds bundle from Hub by digest, and hand View the URL to fetch."""
        _require(materializer, "terrain")
        assert materializer is not None
        try:
            world = materializer.materialize(reference)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return WorldResponse(
            reference=world.reference,
            digest=world.digest,
            world_id=world.world_id,
            manifest_url=f"{WORLD_STATIC_PREFIX}/{world.path.name}/{world.manifest_path.name}",
            site=_read_world_site(world.manifest_path),
        )

    @router.get("/catalog/assets")
    def list_catalog(requires: list[str] = Query(default=[])) -> list[MenuEntry]:
        """The robot menu: the Hub asset catalog, optionally filtered by capability tag."""
        _require(catalog, "catalog")
        assert catalog is not None
        try:
            tags = [CapabilityTag(tag) for tag in requires]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return catalog.list_assets(requires=tags or None)

    @router.get("/catalog/worlds")
    def list_worlds() -> list[WorldEntry]:
        """The world menu: the world bundles present in the configured registry.

        `GET /studio/worlds/{reference}` already materializes whichever of these is chosen — only
        the front door was missing, so terrain was reachable solely by hand-editing `?world=`."""
        _require(catalog, "catalog")
        assert catalog is not None
        return catalog.list_worlds()

    @router.get("/catalog/preview/{reference:path}")
    def preview_asset(reference: str) -> AssetPreviewResponse:
        """Materialize a selected asset's geometry from Hub by digest; hand View a URL to fetch."""
        _require(preview_materializer, "asset preview")
        assert preview_materializer is not None
        try:
            preview = preview_materializer.preview(reference)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return AssetPreviewResponse(
            reference=preview.reference,
            digest=preview.digest,
            document_url=f"{ASSET_STATIC_PREFIX}/{preview.path.name}/{preview.document_path.name}",
        )

    return router


def create_app(
    *,
    workspace: WorkspaceStore | None = None,
    clients: SiblingClients | None = None,
    publisher: ArtifactPublisher | None = None,
    materializer: WorldMaterializer | None = None,
    world_cache_dir: str | None = None,
    catalog: StudioCatalog | None = None,
    preview_materializer: AssetPreviewMaterializer | None = None,
    asset_cache_dir: str | None = None,
) -> FastAPI:
    """Build the Studio FastAPI app, driving the injected (default all-local) workspace
    and sibling clients.

    ``publisher``/``materializer`` and ``catalog``/``preview_materializer`` are the Hub seams
    (RM-P1-STUDIO-06, -09). They are optional because the deployment may not have a registry to
    resolve from: without them the app still captures intent and runs studies, and the
    publish/terrain/catalog routes answer 503 rather than pretending.
    """
    app = FastAPI(title="Astro-Mine-Studio", version=__version__)
    # The browser tier calls this API cross-origin (_cors.py). Applied here as well as in
    # the composed app so a route test drives an app that behaves like the deployed one.
    add_cors(app)
    mount_static(app, world_cache_dir=world_cache_dir, asset_cache_dir=asset_cache_dir)
    app.include_router(
        build_router(
            workspace=workspace,
            clients=clients,
            publisher=publisher,
            materializer=materializer,
            catalog=catalog,
            preview_materializer=preview_materializer,
        )
    )
    return app
