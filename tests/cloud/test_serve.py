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


def test_healthz_and_backends(client: TestClient) -> None:
    assert client.get("/cloud/healthz").json() == {"status": "ok"}
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
    assert "unknown backend" in response.json()["detail"]


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
    workflow = WorkflowSpec(
        name="pipe", steps=[WorkflowStep(name="a", job=JobSpec(image=IMAGE, command=["run"]))]
    ).model_dump(mode="json")
    assert client.post("/cloud/workflows/compile", json=workflow).json()["kind"] == "Workflow"
