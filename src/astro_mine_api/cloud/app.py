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
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel, ConfigDict, Field

from astro_mine_api._cors import add_cors
from astro_mine_api._errors import ERROR_RESPONSES, ApiError, ErrorCode, add_error_handlers
from astro_mine_api._health import Health, health
from astro_mine_api._ids import unique_operation_id

__all__ = ["PREFIX", "build_router", "create_app"]

#: The component prefix every Cloud route is served under.
PREFIX = "/cloud"


class SweepExpansion(BaseModel):
    """A sweep's deterministic expansion into concrete jobs.

    Astro-Mine's own shape end to end: what a SweepSpec *means*, before any engine sees it.
    """

    size: int
    jobs: list[JobSpec]


# Unlike `spec`, none of this belongs to an engine: `astro_mine.cloud.k8s.object_meta` builds it,
# and every engine in the platform calls that one function. Declaring it is therefore not a guess
# about someone else's schema — it is writing down our own.
class CompiledManifestMetadata(BaseModel):
    """A compiled object's ``metadata`` -- the half of a manifest Astro-Mine writes.

    Built the same way for a Kubernetes ``Job``, a KubeRay ``RayJob`` and an Argo ``Workflow``, and
    the keys inside it are the platform's own label schema (``app.kubernetes.io/*``,
    ``astro-mine.org/*``, plus Kueue's queue label). This is the part of a compiled object that can
    be read without knowing which engine produced it: what the run will be called, where it lands,
    and whose quota it is charged to.
    """

    # Open for the same reason `CompiledManifest` is; see the comment above it.
    model_config = ConfigDict(extra="allow")

    #: The object's name, deterministic in the spec that produced it: ``<prefix>-<12 hex of the
    #: spec's content address>`` for a job or a sweep, the sanitised ``WorkflowSpec.name`` for a
    #: workflow. Equal specs compile to equal names by construction (``cloud.md`` §2 principle 4),
    #: which is what makes a compile preview comparable across two requests.
    name: str
    #: Where the object lands: the route's ``namespace`` parameter, RFC-1123 sanitised. Required
    #: rather than optional because these three routes always pass one -- ``object_meta`` leaves it
    #: optional only for the callers that legitimately omit it, and no compile route is one.
    namespace: str
    #: managed-by / part-of / component, plus the tenant and Kueue ``LocalQueue`` labels when the
    #: spec named a tenant. A map rather than fields: these are Kubernetes label keys, and one of
    #: them is Kueue's domain rather than ours.
    labels: dict[str, str] = Field(default_factory=dict)
    #: The content-addressed I/O the workload will stage (``astro-mine.org/inputs`` and
    #: ``.../outputs``); the manifest omits the key entirely when the spec declared neither.
    #: Defaulted to an empty map rather than made nullable -- "no annotations" and "an empty
    #: annotation set" are the same object to Kubernetes, and the client gets a total type.
    annotations: dict[str, str] = Field(default_factory=dict)


# A docstring on a response model is published verbatim as the schema's `description`, so what
# belongs to the *reader of the contract* stays in the docstring below and the reasoning behind the
# shape stays here.
#
# **One model, because there is one shape.** These are four different objects — a Kubernetes Job, a
# KubeRay RayJob, and an Argo Workflow from each of the two remaining routes — but every one of them
# is a Kubernetes object, so every one has the same four keys and differs only in the value of
# `kind` and the contents of `spec`. Three classes differing only in a docstring would tell a
# generated client less than one that says so.
#
# **Only `spec` was ever genuinely open.** That is what the three routes used to say about their
# whole payload, and for the spec it is still true: a Job's is Kubernetes', a RayJob's is KubeRay's,
# a Workflow's is Argo's, and declaring one here would be a second copy of someone else's API to
# chase and a lie the first time an engine added a field. The envelope was never theirs in that
# sense — apiVersion/kind/metadata/spec is the meta-schema every Kubernetes object has, and metadata
# is mostly Astro-Mine's own writing — so leaving it open bought nothing and cost the front end the
# ability to name what it was holding (api#12).
#
# **`extra="allow"` is load-bearing, not decoration.** A declared response model *filters*: FastAPI
# serialises what the model declares and drops the rest. Engines are a registry seam
# (`register_engine`), so a closed model would silently delete a top-level key an out-of-tree engine
# emitted. Declaring what is always present must not mean discarding what is sometimes present.
class CompiledManifest(BaseModel):
    """A spec compiled to the cluster object that would run it.

    The response of all three ``compile`` routes: a ``batch/v1`` ``Job`` or a ``ray.io/v1``
    ``RayJob`` from ``/cloud/jobs/compile`` depending on which engine the job routes to, and an
    ``argoproj.io/v1alpha1`` ``Workflow`` from ``/cloud/sweeps/compile`` and
    ``/cloud/workflows/compile``. The manifest is served as it would be applied, so ``apiVersion``,
    ``kind`` and ``metadata`` can be read without knowing which of those you asked for.

    ``spec`` is deliberately an open object: it is the schema of whichever engine compiled it, and
    ``kind`` is what says which one that is. The model is open for the same reason -- an engine
    outside this repository may emit fields nothing here has heard of, and they are passed through
    rather than dropped.
    """

    model_config = ConfigDict(extra="allow")

    #: The API group and version -- ``batch/v1``, ``ray.io/v1``, ``argoproj.io/v1alpha1``. Aliased
    #: rather than renamed: the response is a manifest that must stay applicable as it arrives, so
    #: the wire keeps Kubernetes' spelling and only the attribute is snake_case.
    api_version: str = Field(alias="apiVersion")
    #: ``Job``, ``RayJob`` or ``Workflow``. With ``api_version``, what tells a client whose schema
    #: ``spec`` follows.
    kind: str
    metadata: CompiledManifestMetadata
    #: The engine's own object spec, served verbatim. See the class docstring.
    spec: dict[str, Any]


def build_router(
    *, store: ArtifactStore | None = None, default_backend: str = "local"
) -> APIRouter:
    """The submission-edge routes, prefixed with ``/cloud``.

    *store* is the artifact store submitted jobs run against (default: a local
    :class:`~astro_mine.cloud.artifacts.store.FilesystemArtifactStore`); *default_backend* is
    the backend used when a submit request does not name one.
    """
    artifact_store: ArtifactStore = store if store is not None else FilesystemArtifactStore()
    router = APIRouter(prefix=PREFIX, responses=ERROR_RESPONSES)

    @router.get("/healthz")
    def healthz() -> Health:
        return health("cloud")

    @router.get("/backends")
    def backends() -> dict[str, list[str]]:
        return {"backends": list(registered_backends())}

    @router.post("/jobs")
    def submit_job(job: JobSpec, backend: str = default_backend) -> RunResult:
        """Submit a JobSpec through *backend* -- the same call site as the CLI/library."""
        try:
            return submit(job, backend=backend, store=artifact_store)
        except ValueError as exc:  # unknown backend, bad input address, ...
            raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc

    @router.post("/jobs/compile")
    def compile_job(
        job: JobSpec, namespace: str = "default", engine: str | None = None
    ) -> CompiledManifest:
        """Compile a JobSpec to its engine manifest (auto-routed unless *engine* is given).

        A ``batch/v1`` Job or a ``ray.io/v1`` RayJob depending on what
        :func:`~astro_mine.cloud.engines.select_engine` routes *job* to; :class:`CompiledManifest`
        is the shape either way.
        """
        name = engine if engine is not None else select_engine(job)
        try:
            compiled = get_engine(name).compile(job, namespace=namespace)
        except ValueError as exc:  # unknown engine, unroutable job, ...
            raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        # Outside the `try` on purpose: pydantic's ValidationError *is* a ValueError, so validating
        # in there would report an engine that emitted an unusable manifest as the caller's bad
        # request. A manifest this model cannot read is a 500, which is what it is.
        return CompiledManifest.model_validate(compiled)

    @router.post("/sweeps/expand")
    def expand_sweep(sweep: SweepSpec) -> SweepExpansion:
        """Preview a SweepSpec's deterministic expansion into concrete jobs."""
        variants = sweep.expand()
        return SweepExpansion(size=len(variants), jobs=list(variants))

    @router.post("/sweeps/compile", operation_id="cloud_compile_sweep")
    def compile_sweep_endpoint(sweep: SweepSpec, namespace: str = "default") -> CompiledManifest:
        """Compile a SweepSpec to its Argo fan-out Workflow -- one task per expanded variant."""
        return CompiledManifest.model_validate(compile_sweep(sweep, namespace=namespace))

    @router.post("/workflows/compile", operation_id="cloud_compile_workflow")
    def compile_workflow_endpoint(
        workflow: WorkflowSpec, namespace: str = "default"
    ) -> CompiledManifest:
        """Compile a WorkflowSpec to its Argo DAG Workflow -- one task per step, edges as deps."""
        return CompiledManifest.model_validate(compile_workflow(workflow, namespace=namespace))

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
        generate_unique_id_function=unique_operation_id,
    )
    # The browser tier calls this API cross-origin (_cors.py), and every refusal leaves as a problem
    # document (_errors.py). Applied here as well as in the composed app so a route test drives an
    # app that behaves -- and fails -- like the deployed one.
    add_cors(app)
    add_error_handlers(app)
    app.include_router(build_router(store=store, default_backend=default_backend))
    return app
