"""The FastAPI submission-edge app factory.

:func:`create_app` builds the REST edge: submit a JobSpec, compile a JobSpec/SweepSpec/
WorkflowSpec to its engine object, preview a sweep, or list backends -- each handler delegating
to the same submission library the CLI drives (``cloud.md`` §3). Every route validates its body
against the pydantic contracts, so a malformed spec is a 422 before anything runs.

Ported from ``astro_mine.cloud.serve.app`` (astro-mine-cloud) unchanged but for the import paths
and the ``/cloud`` component prefix. The lazy ``import fastapi`` the original did inside the
factory is gone: it existed so the base ``astro-mine-cloud`` wheel could import without the
``[serve]`` extra, and in this distribution FastAPI is a base dependency -- there is no install
of astro-mine-api that lacks it.

Backlog: RM-P1-CLOUD-02 -- https://github.com/astro-mine/astro-mine-cloud/issues/13
"""

from __future__ import annotations

from typing import Any

from astro_mine.cloud.artifacts.store import FilesystemArtifactStore
from astro_mine.cloud.engines import (
    compile_sweep,
    compile_workflow,
    get_engine,
    select_engine,
)
from astro_mine.cloud.submission import submit
from astro_mine.cloud.submission.backend import registered_backends
from astro_mine.cloud.submission.jobspec import JobSpec
from astro_mine.cloud.submission.result import RunResult
from astro_mine.cloud.submission.sweepspec import SweepSpec
from astro_mine.cloud.submission.workflowspec import WorkflowSpec

# The `ArtifactStore` *protocol* is Core's, not Cloud's: the consolidation moved it there
# (astro-mine-platform#7) because two components share it. The original route module imported it
# from `astro_mine.cloud.artifacts.store` under TYPE_CHECKING, which no longer resolves — the one
# import this port had to re-point rather than merely re-prefix.
from astro_mine.core.artifacts import ArtifactStore
from fastapi import APIRouter, FastAPI, HTTPException

__all__ = ["PREFIX", "build_router", "create_app"]

#: The component prefix every Cloud route is served under.
PREFIX = "/cloud"


def build_router(
    *, store: ArtifactStore | None = None, default_backend: str = "local"
) -> APIRouter:
    """The submission-edge routes, prefixed with ``/cloud``.

    *store* is the artifact store submitted jobs run against (default: a local
    :class:`~astro_mine.cloud.artifacts.store.FilesystemArtifactStore`); *default_backend* is
    the backend used when a submit request does not name one.
    """
    artifact_store: ArtifactStore = store if store is not None else FilesystemArtifactStore()
    router = APIRouter(prefix=PREFIX)

    @router.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/backends")
    def backends() -> dict[str, list[str]]:
        return {"backends": list(registered_backends())}

    @router.post("/jobs")
    def submit_job(job: JobSpec, backend: str = default_backend) -> RunResult:
        """Submit a JobSpec through *backend* -- the same call site as the CLI/library."""
        try:
            return submit(job, backend=backend, store=artifact_store)
        except ValueError as exc:  # unknown backend, bad input address, ...
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/jobs/compile")
    def compile_job(
        job: JobSpec, namespace: str = "default", engine: str | None = None
    ) -> dict[str, Any]:
        """Compile a JobSpec to its engine manifest (auto-routed unless *engine* is given)."""
        name = engine if engine is not None else select_engine(job)
        try:
            return get_engine(name).compile(job, namespace=namespace)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/sweeps/expand")
    def expand_sweep(sweep: SweepSpec) -> dict[str, Any]:
        """Preview a SweepSpec's deterministic expansion into concrete jobs."""
        variants = sweep.expand()
        return {"size": len(variants), "jobs": [v.model_dump(mode="json") for v in variants]}

    @router.post("/sweeps/compile")
    def compile_sweep_endpoint(sweep: SweepSpec, namespace: str = "default") -> dict[str, Any]:
        """Compile a SweepSpec to its Argo fan-out Workflow."""
        return compile_sweep(sweep, namespace=namespace)

    @router.post("/workflows/compile")
    def compile_workflow_endpoint(
        workflow: WorkflowSpec, namespace: str = "default"
    ) -> dict[str, Any]:
        """Compile a WorkflowSpec to its Argo DAG Workflow."""
        return compile_workflow(workflow, namespace=namespace)

    return router


def create_app(*, store: ArtifactStore | None = None, default_backend: str = "local") -> FastAPI:
    """Build the submission-edge FastAPI app.

    *store* is the artifact store submitted jobs run against (default: a local
    :class:`~astro_mine.cloud.artifacts.store.FilesystemArtifactStore`); *default_backend* is
    the backend used when a submit request does not name one.
    """
    app = FastAPI(
        title="astro-mine-cloud",
        summary="Submission edge -- submit and compile jobs/sweeps/workflows.",
    )
    app.include_router(build_router(store=store, default_backend=default_backend))
    return app
