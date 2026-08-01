"""The leaderboard FastAPI application (RM-P0-BENCH-06 → RM-P1-BENCH-10; bench.md §3, §7, §9, §10).

The public leaderboard's REST/OpenAPI edge, and the boundary where the platform's security posture
is actually enforced. It carries two intake paths over one catalog and one scoring harness
(bench.md §2.6):

- **local ``policy_ref``** (P0, RM-P0-BENCH-06): ``POST /bench/submissions`` runs an importable
  policy on the embargoed held-out seeds and ranks it;
- **Hub digest** (P1, RM-P1-BENCH-10): ``POST /bench/submissions/hub`` resolves a community
  submission from Hub **by content hash**, verifies it fail-closed, validates its Core plugin
  manifest against the scenario interface, runs it under submit-policy-we-run, re-executes a
  sampled fraction from its provenance bundle for the integrity verdict, and ranks it — the
  *flywheel* path.

**Two tiers, one rule (bench#29).** Every *write* — both submission routes, the ranking-mutation
route, the scenario-authoring route, and the audit trail — requires a valid **OIDC bearer token**
and passes the OPA-style policy engine (RBAC + per-user quota + embargo). Every *read* — the board,
a scorecard, a provenance bundle, a replay, liveness — stays **account-free and token-free**,
because
the local/offline tier is sacred (CX-LOCAL; bench#29 AC5). A deployment with no IdP configured
refuses writes with 503; it never falls open.

``GET /bench/metrics`` exposes the Prometheus series bench.md §10 names — queue depth,
re-execution mismatch rate, evaluation latency (bench#32) — and every route is traced through the
``submit → evaluate → score → rank`` span pipeline.

Ported from ``astro_mine.bench.leaderboard._app`` (astro-mine-bench) unchanged but for the import
paths and the ``/bench`` component prefix. The leaderboard *library* it drives — the service
layer, SQL, auth, authorization, evaluation, provenance and audit modules — stays in
``astro_mine.bench.leaderboard`` in the platform, which is where it would live even with no HTTP
in the picture (api.md §2).

Backlog: RM-P1-BENCH-10 — https://github.com/astro-mine/astro-mine-bench/issues/18;
bench#29, bench#30, bench#32
"""

from __future__ import annotations

import os
from typing import Annotated

from astro_mine.bench._version import __version__
from astro_mine.bench.leaderboard._audit import AuditDecision, AuditEvent, audit_event
from astro_mine.bench.leaderboard._auth import Principal, oidc_verifier_from_env
from astro_mine.bench.leaderboard._authz import Action, policy_engine_from_env
from astro_mine.bench.leaderboard._eval import rank
from astro_mine.bench.leaderboard._hub import open_registry
from astro_mine.bench.leaderboard._jobs import JobRecord as _PlatformJobRecord
from astro_mine.bench.leaderboard._models import (
    HubSubmissionRequest,
    LeaderboardEntry,
    Submission,
    SubmissionRequest,
)
from astro_mine.bench.leaderboard._provenance import ProvenanceBundle
from astro_mine.bench.leaderboard._service import LeaderboardService, SubmissionRejected
from astro_mine.bench.leaderboard._store import InMemoryStore, LeaderboardStore
from astro_mine.bench.leaderboard._supply_chain import attestation_policy_from_env
from astro_mine.bench.report import ViewLeaderboard, ViewReplay, export_leaderboard, replay_manifest
from astro_mine.bench.sandbox import SandboxLimits, SandboxScorer, SubprocessSandbox
from astro_mine.bench.telemetry import metrics_exposition, span
from astro_mine.bench.zoo import ScenarioCatalog, WritableCatalog, default_catalog
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Response

from astro_mine_api._cors import add_cors
from astro_mine_api._ids import unique_operation_id

__all__ = [
    "DB_ENV",
    "OBJECTS_ENV",
    "PREFIX",
    "REGISTRY_ENV",
    "SANDBOX_CPU_SECONDS_ENV",
    "SANDBOX_MEMORY_BYTES_ENV",
    "SANDBOX_PYTHONPATH_ENV",
    "SANDBOX_WALL_SECONDS_ENV",
    "build_router",
    "create_app",
]

#: The component prefix every Bench route is served under.
PREFIX = "/bench"


class BenchJobRecord(_PlatformJobRecord):
    """Bench's evaluation job record, named for the document rather than for the module.

    Bench and Studio each own a ``JobRecord``, and they are genuinely different things — one tracks
    an evaluation, the other a design study. Mounted in one process they collide in the OpenAPI
    document, and FastAPI disambiguates by prefixing the module path, which produces
    ``astro_mine__bench__leaderboard___jobs__JobRecord`` and a generated client method returning a
    type nobody can type.

    The collision is an artifact of *composition* — it exists only because this distribution serves
    both surfaces from one document — so it is resolved here rather than by renaming a platform type
    that is unambiguous on its own. This subclass adds nothing: every field is inherited, so the
    document and the platform model cannot drift.
    """


#: Env var selecting the durable submission store URL; unset falls back to the in-memory backend.
DB_ENV = "ASTRO_MINE_BENCH_DB"
#: Env var pointing at the content-addressed Hub registry for digest intake (the workspace
#: ``files/hub-registry`` convention). Unset ⇒ the Hub intake path returns 503.
REGISTRY_ENV = "ASTRO_MINE_HUB_REGISTRY"
#: Env var pointing at the on-disk object-store root (traces + provenance bundles). Unset ⇒
#: in-memory.
OBJECTS_ENV = "ASTRO_MINE_BENCH_OBJECTS"
#: Sandbox envelope overrides for the evaluation worker (bench#30). The defaults already deny egress
#: and expose no GPU; these tune the resource caps to the deployment's hardware.
SANDBOX_CPU_SECONDS_ENV = "ASTRO_MINE_BENCH_SANDBOX_CPU_SECONDS"
SANDBOX_MEMORY_BYTES_ENV = "ASTRO_MINE_BENCH_SANDBOX_MEMORY_BYTES"
SANDBOX_WALL_SECONDS_ENV = "ASTRO_MINE_BENCH_SANDBOX_WALL_SECONDS"
#: Extra import roots the sandboxed worker gets (its environment is otherwise scrubbed).
SANDBOX_PYTHONPATH_ENV = "ASTRO_MINE_BENCH_SANDBOX_PYTHONPATH"

_OPENAPI_TAGS = [
    {"name": "submissions", "description": "Submit and read policies (local ref or Hub digest)."},
    {"name": "leaderboard", "description": "Ranked results and per-entry provenance lineage."},
    {"name": "jobs", "description": "Async Hub-submission lifecycle state."},
    {"name": "scenarios", "description": "The scenario zoo catalog."},
    {"name": "admin", "description": "Privileged board administration and the audit trail."},
    {"name": "meta", "description": "Service liveness and Prometheus metrics."},
]


def _default_store() -> LeaderboardStore:
    """Pick the store from the environment: a SQL URL if set, else the in-memory backend."""
    url = os.environ.get(DB_ENV)
    if url:
        from astro_mine.bench.leaderboard._sql import SqlStore

        return SqlStore(url)
    return InMemoryStore()


def _default_sandbox_limits() -> SandboxLimits:
    """The submitted-policy envelope, tuned from the environment (never *loosened* below deny)."""
    defaults = SandboxLimits()
    return SandboxLimits(
        cpu_seconds=int(os.environ.get(SANDBOX_CPU_SECONDS_ENV, defaults.cpu_seconds)),
        wall_seconds=float(os.environ.get(SANDBOX_WALL_SECONDS_ENV, defaults.wall_seconds)),
        memory_bytes=int(os.environ.get(SANDBOX_MEMORY_BYTES_ENV, defaults.memory_bytes)),
    )


def _default_service(store: LeaderboardStore) -> LeaderboardService:
    """Build the hosted service, wiring each backend from the environment (fail-closed)."""
    from astro_mine.bench.leaderboard._objects import FileObjectStore, InMemoryObjectStore

    registry_path = os.environ.get(REGISTRY_ENV)
    objects_root = os.environ.get(OBJECTS_ENV)
    python_path = tuple(
        part for part in os.environ.get(SANDBOX_PYTHONPATH_ENV, "").split(os.pathsep) if part
    )
    sandbox = SubprocessSandbox(limits=_default_sandbox_limits(), python_path=python_path)
    return LeaderboardService(
        store=store,
        object_store=FileObjectStore(objects_root) if objects_root else InMemoryObjectStore(),
        registry=open_registry(registry_path) if registry_path else None,
        # No IdP configured ⇒ authn is None ⇒ every write route refuses with 503 (bench#29).
        authn=oidc_verifier_from_env(),
        policy_engine=policy_engine_from_env(),
        attestation_policy=attestation_policy_from_env(),
        scorer=SandboxScorer(sandbox),
    )


def build_router(
    store: LeaderboardStore | None = None,
    *,
    service: LeaderboardService | None = None,
    catalog: ScenarioCatalog | None = None,
) -> APIRouter:
    """The leaderboard routes, prefixed with ``/bench``.

    ``store`` selects the submission catalog (default: env-selected — see :data:`DB_ENV`);
    ``service`` injects a fully-wired :class:`LeaderboardService` (its ``store`` is authoritative
    when given); otherwise one is built from the environment. ``catalog`` selects the scenario zoo
    (default: env-selected — the packaged filesystem zoo, or the Postgres/pgvector catalog when
    ``ASTRO_MINE_BENCH_CATALOG_DSN`` is set). The Hub intake path is available only when the service
    has a registry configured, and every write route is available only when it has an OIDC verifier.
    """
    if service is None:
        backend = store if store is not None else _default_store()
        service = _default_service(backend)
    backend = service.store
    zoo: ScenarioCatalog = catalog if catalog is not None else default_catalog()
    bound = service

    router = APIRouter(prefix=PREFIX)

    def authenticated(
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        """FastAPI dependency: the caller's verified :class:`Principal`, or 401/503 (bench#29)."""
        try:
            return bound.authenticate(authorization)
        except SubmissionRejected as exc:
            headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
            raise HTTPException(status_code=exc.status, detail=str(exc), headers=headers) from exc

    # NOTE: the `principal: Principal = Depends(authenticated)` default-value form, not
    # `Annotated[Principal, Depends(...)]`. Under `from __future__ import annotations` FastAPI
    # resolves annotations against the *module* globals, and an alias defined here inside
    # build_router() is not there — it would silently degrade the dependency into a query parameter,
    # i.e. an unauthenticated route. Keep the annotation a module-level name.

    # --- meta (account-free) ---------------------------------------------------------------------

    @router.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get(
        "/metrics",
        tags=["meta"],
        # Prometheus text exposition, not JSON — declared so the document says so rather than
        # leaving a client to guess from an empty schema.
        response_class=Response,
        responses={
            200: {
                "content": {"text/plain": {"schema": {"type": "string"}}},
                "description": "Prometheus exposition format.",
            }
        },
    )
    def prometheus_metrics() -> Response:
        """Prometheus exposition for the submission pipeline (bench.md §10; bench#32).

        Queue depth, re-execution mismatch rate (the key integrity signal), evaluation latency,
        authorization decisions, supply-chain verifications, and sandbox terminations. Left
        unauthenticated so a Prometheus scraper needs no account — the deployment restricts it at
        the network layer, as is conventional.
        """
        body, content_type = metrics_exposition()
        return Response(content=body, media_type=content_type)

    # --- submissions (authenticated writes) ------------------------------------------------------

    @router.post("/submissions", response_model=Submission, tags=["submissions"])
    def submit(
        request: SubmissionRequest, principal: Principal = Depends(authenticated)
    ) -> Submission:
        """Submit an importable ``policy_ref``; it runs **in a sandbox**, never in-process."""
        try:
            spec = zoo.load_scenario(request.scenario_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return bound.submit_local(spec, request, principal)
        except SubmissionRejected as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @router.post("/submissions/hub", response_model=BenchJobRecord, tags=["submissions"])
    def submit_hub(
        request: HubSubmissionRequest, principal: Principal = Depends(authenticated)
    ) -> _PlatformJobRecord:
        """Submit a community artifact by Hub digest; verified (cosign/SLSA/SBOM) then sandboxed."""
        if bound.registry is None:
            raise HTTPException(
                status_code=503, detail="Hub-digest intake is not configured on this deployment"
            )
        try:
            spec = zoo.load_scenario(request.scenario_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return bound.submit_hub(spec, request, principal)
        except SubmissionRejected as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @router.get("/submissions/{submission_id}", response_model=Submission, tags=["submissions"])
    def get_submission(submission_id: str) -> Submission:
        submission = backend.get_submission(submission_id)
        if submission is None:
            raise HTTPException(status_code=404, detail=f"no submission {submission_id!r}")
        return submission

    @router.delete("/submissions/{submission_id}", response_model=Submission, tags=["admin"])
    def retract_submission(
        submission_id: str, principal: Principal = Depends(authenticated)
    ) -> Submission:
        """Retract an entry from the board — ``ranking:mutate``, admin-only, audit-logged."""
        try:
            return bound.retract(submission_id, principal)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except SubmissionRejected as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    # --- scenarios (authoring is authenticated; listing is not) -----------------------------------

    @router.get("/scenarios", tags=["scenarios"])
    def list_scenarios() -> list[str]:
        """The scenario ids in the zoo — account-free, like every read path."""
        return list(zoo.list_scenarios())

    @router.post("/scenarios", tags=["scenarios"], status_code=201)
    def author_scenario(
        spec: dict[str, object], principal: Principal = Depends(authenticated)
    ) -> dict[str, str]:
        """Publish a ScenarioSpec into the hosted catalog — ``scenario:author`` (bench#29).

        The write surface of the Postgres/pgvector zoo catalog (bench#33): only a maintainer or an
        admin may add to the commons' benchmark catalog, and the act is audit-logged. Returns 503 on
        a deployment whose catalog is the read-only packaged filesystem zoo.
        """
        from astro_mine.bench.scenario import ScenarioSpec

        if not isinstance(zoo, WritableCatalog):
            raise HTTPException(
                status_code=503,
                detail="this deployment's zoo catalog is read-only; configure "
                "ASTRO_MINE_BENCH_CATALOG_DSN to author scenarios",
            )
        try:
            parsed = ScenarioSpec.model_validate(spec)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            bound.authorize(principal, Action.SCENARIO_AUTHOR, parsed.scenario_id)
        except SubmissionRejected as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        with span("bench.scenario.author", **{"bench.scenario_id": parsed.scenario_id}):
            zoo.upsert(parsed)
        bound.audit.record(
            audit_event(
                action=str(Action.SCENARIO_AUTHOR),
                decision=AuditDecision.ALLOW,
                subject=principal.subject,
                issuer=principal.issuer,
                resource=parsed.scenario_id,
                reason="scenario published to the hosted zoo catalog",
            )
        )
        return {"scenario_id": parsed.scenario_id, "spec_hash": parsed.spec_hash}

    # --- leaderboard reads (account-free) --------------------------------------------------------

    @router.get(
        "/submissions/{submission_id}/provenance",
        response_model=ProvenanceBundle,
        tags=["leaderboard"],
    )
    def get_provenance(submission_id: str) -> ProvenanceBundle:
        bundle = bound.get_provenance(submission_id)
        if bundle is None:
            raise HTTPException(
                status_code=404, detail=f"no provenance bundle for {submission_id!r}"
            )
        return bundle

    @router.get("/jobs/{job_id}", response_model=BenchJobRecord, tags=["jobs"])
    def get_job(job_id: str) -> _PlatformJobRecord:
        job = bound.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
        return job

    @router.get(
        "/leaderboard/{scenario_id}", response_model=list[LeaderboardEntry], tags=["leaderboard"]
    )
    def leaderboard(scenario_id: str) -> list[LeaderboardEntry]:
        return rank(backend.list_submissions(scenario_id))

    @router.get(
        "/leaderboard/{scenario_id}/scorecards",
        response_model=ViewLeaderboard,
        tags=["leaderboard"],
        operation_id="bench_leaderboard_scorecards",
    )
    def scorecards(scenario_id: str) -> ViewLeaderboard:
        """The full per-metric leaderboard dataset View renders (bench.md §6; RM-P1-BENCH-12).

        Same ranking as ``/bench/leaderboard/{scenario_id}`` but every row carries its complete
        scorecard with per-metric uncertainty, so View shows scorecards and bounds, not just the
        primary.
        """
        return export_leaderboard(scenario_id, backend.list_submissions(scenario_id))

    @router.get(
        "/submissions/{submission_id}/replay",
        tags=["leaderboard"],
        # A binary download, not JSON. Without this the document advertises an empty schema and a
        # generated client types the result as `unknown` instead of a blob — the same defect as an
        # untyped object, wearing a different disguise.
        response_class=Response,
        responses={
            200: {
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
                "description": "The MCAP episode log.",
            }
        },
    )
    def get_replay(submission_id: str) -> Response:
        """The MCAP episode replay bytes View plays (``application/octet-stream``), 404 if none.

        Bench provides the MCAP replays; View renders them (bench.md §6). A replay is present only
        when one was attached to the entry (``LeaderboardService.attach_replay``).
        """
        mcap = bound.get_replay(submission_id)
        if mcap is None:
            raise HTTPException(status_code=404, detail=f"no replay for {submission_id!r}")
        return Response(
            content=mcap,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{submission_id}.mcap"'},
        )

    @router.get(
        "/submissions/{submission_id}/replay/manifest",
        response_model=ViewReplay,
        tags=["leaderboard"],
    )
    def get_replay_manifest(submission_id: str) -> ViewReplay:
        """The decoded replay manifest (frames, agents, sim-time span); 404 if no replay."""
        submission = backend.get_submission(submission_id)
        if submission is None:
            raise HTTPException(status_code=404, detail=f"no submission {submission_id!r}")
        mcap = bound.get_replay(submission_id)
        if mcap is None:
            raise HTTPException(status_code=404, detail=f"no replay for {submission_id!r}")
        return replay_manifest(
            mcap, scenario_id=submission.scenario_id, submission_id=submission_id
        )

    # --- the audit trail (admin) -----------------------------------------------------------------

    @router.get("/audit", response_model=list[AuditEvent], tags=["admin"])
    def audit_trail(
        principal: Principal = Depends(authenticated),
        subject: str | None = None,
        action: str | None = None,
        decision: AuditDecision | None = None,
        resource: str | None = None,
        submission_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Query the authN/authZ + verification audit trail — ``audit:read``, admin-only (bench#29).

        The queryable half of "disputes are auditable" (bench.md §9): filter by who, what, and the
        outcome, newest first.
        """
        try:
            bound.authorize(principal, Action.AUDIT_READ, "audit")
        except SubmissionRejected as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return bound.audit.query(
            subject=subject,
            action=action,
            decision=decision,
            resource=resource,
            submission_id=submission_id,
            limit=limit,
        )

    return router


def create_app(
    store: LeaderboardStore | None = None,
    *,
    service: LeaderboardService | None = None,
    catalog: ScenarioCatalog | None = None,
) -> FastAPI:
    """Build the leaderboard app.

    ``store`` selects the submission catalog (default: env-selected — see :data:`DB_ENV`);
    ``service`` injects a fully-wired :class:`LeaderboardService` (its ``store`` is authoritative
    when given); otherwise one is built from the environment. ``catalog`` selects the scenario zoo
    (default: env-selected — the packaged filesystem zoo, or the Postgres/pgvector catalog when
    ``ASTRO_MINE_BENCH_CATALOG_DSN`` is set). The Hub intake path is available only when the service
    has a registry configured, and every write route is available only when it has an OIDC verifier.
    """
    app = FastAPI(
        title="Astro-Mine-Bench leaderboard",
        version=__version__,
        summary="Public leaderboard: submit-policy-we-run + Hub-digest intake, held-out seeds, "
        "provenance re-execution. Writes require an OIDC bearer token; reads are account-free.",
        openapi_tags=_OPENAPI_TAGS,
        generate_unique_id_function=unique_operation_id,
    )
    # The browser tier calls this API cross-origin (_cors.py). Applied here as well as in
    # the composed app so a route test drives an app that behaves like the deployed one.
    add_cors(app)
    app.include_router(build_router(store, service=service, catalog=catalog))
    return app
