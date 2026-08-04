"""The FastAPI submission edge -- in-process via TestClient (no server, no cluster).

Proves the REST edge delegates to the same submission library the CLI uses: a job submitted
over HTTP runs the identical container path, and specs are validated against the pydantic
contracts before anything runs.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from astro_mine.cloud.artifacts.store import FilesystemArtifactStore
from astro_mine.cloud.packaging import ImageRef
from astro_mine.cloud.submission.jobspec import JobSpec
from astro_mine.cloud.submission.sweepspec import SweepSpec
from astro_mine.cloud.submission.workflowspec import WorkflowSpec, WorkflowStep
from fastapi.testclient import TestClient

from astro_mine_api.cloud import create_app

IMAGE = ImageRef.parse("ghcr.io/astro-mine/x@sha256:" + "ab" * 32)
_WRITE_SEED = (
    "import os, pathlib;"
    "o=pathlib.Path(os.environ['ASTRO_MINE_OUTPUTS']);"
    "(o/'y.txt').write_text(os.environ['ASTRO_MINE_SEED'])"
)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(store=FilesystemArtifactStore(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def _job_body(**overrides: object) -> dict:  # type: ignore[type-arg]
    base = JobSpec(image=IMAGE, command=["run"], seed=7)
    return base.model_copy(update=overrides).model_dump(mode="json")


def _workflow_body() -> dict:  # type: ignore[type-arg]
    workflow = WorkflowSpec(
        name="pipe", steps=[WorkflowStep(name="a", job=JobSpec(image=IMAGE, command=["run"]))]
    )
    return workflow.model_dump(mode="json")


def test_healthz_and_backends(client: TestClient) -> None:
    body = client.get("/cloud/healthz").json()
    # The one shape every surface answers with (api#4); `tests/test_health.py` owns the convergence.
    assert body["status"] == "ok" and body["component"] == "cloud"
    assert "local" in client.get("/cloud/backends").json()["backends"]


def test_submit_runs_the_job(client: TestClient) -> None:
    body = _job_body(command=[sys.executable, "-c", _WRITE_SEED], outputs=["y.txt"], seed=42)
    response = client.post("/cloud/jobs", json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "succeeded"
    assert "y.txt" in result["outputs"]


def test_submit_unknown_backend_is_400(client: TestClient) -> None:
    response = client.post("/cloud/jobs", params={"backend": "nope"}, json=_job_body())
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_malformed_spec_is_422(client: TestClient) -> None:
    # an unpinned image can never enter a JobSpec -> validation error before anything runs
    response = client.post(
        "/cloud/jobs", json={"image": {"repository": "x", "digest": "not-a-digest"}}
    )
    assert response.status_code == 422


def test_compile_job_auto_and_forced(client: TestClient) -> None:
    assert client.post("/cloud/jobs/compile", json=_job_body()).json()["kind"] == "Job"
    ray = client.post("/cloud/jobs/compile", params={"engine": "ray"}, json=_job_body())
    assert ray.json()["kind"] == "RayJob"


def test_compile_job_unknown_engine_is_400(client: TestClient) -> None:
    response = client.post("/cloud/jobs/compile", params={"engine": "bogus"}, json=_job_body())
    assert response.status_code == 400


def test_expand_and_compile_sweep(client: TestClient) -> None:
    sweep = SweepSpec(
        base=JobSpec(image=IMAGE, command=["run"]), grid={"lr": [0.1, 0.2, 0.3]}
    ).model_dump(mode="json")
    assert client.post("/cloud/sweeps/expand", json=sweep).json()["size"] == 3
    assert client.post("/cloud/sweeps/compile", json=sweep).json()["kind"] == "Workflow"


def test_compile_workflow(client: TestClient) -> None:
    body = client.post("/cloud/workflows/compile", json=_workflow_body()).json()
    assert body["kind"] == "Workflow"


def test_a_compiled_manifest_answers_the_declared_envelope(client: TestClient) -> None:
    """The four keys the OpenAPI document now promises, in an actual response (api#12).

    Two engines, so what is asserted is the shape rather than one engine's object: the envelope is
    identical and only ``apiVersion``/``kind``/``spec`` differ, which is why the three compile
    routes share one response model.
    """
    for engine, api_version, kind in [(None, "batch/v1", "Job"), ("ray", "ray.io/v1", "RayJob")]:
        params = {} if engine is None else {"engine": engine}
        body = client.post(
            "/cloud/jobs/compile", params={**params, "namespace": "tenant-a"}, json=_job_body()
        ).json()
        assert (body["apiVersion"], body["kind"]) == (api_version, kind)
        assert body["metadata"]["namespace"] == "tenant-a"
        assert body["metadata"]["name"].startswith("amc-")
        assert body["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "astro-mine-cloud"
        # Declared and defaulted rather than absent: this job names no inputs or outputs.
        assert body["metadata"]["annotations"] == {}
        assert body["spec"]


def test_a_compiled_manifest_keeps_fields_the_model_does_not_declare(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A response model filters. This one must not, because engines are a registry seam.

    Stands in an engine emitting a key the model has never heard of — an out-of-tree engine, or an
    in-tree one the day Argo grows a field — and asserts it reaches the client rather than being
    quietly deleted between the compiler and the wire.
    """
    compiled = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {"name": "w", "namespace": "default", "labels": {}, "generateName": "w-"},
        "spec": {"entrypoint": "main"},
        "hooks": {"exit": {"template": "notify"}},
    }
    monkeypatch.setattr("astro_mine_api.cloud.app.compile_workflow", lambda *a, **k: compiled)

    body = client.post("/cloud/workflows/compile", json=_workflow_body()).json()
    assert body["hooks"] == {"exit": {"template": "notify"}}
    assert body["metadata"]["generateName"] == "w-"
