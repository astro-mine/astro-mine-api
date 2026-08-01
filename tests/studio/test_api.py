"""Thin FastAPI local-tier edge — intent capture + synchronous study runs."""

from __future__ import annotations

from astro_mine.core.objective import ObjectiveDocument
from astro_mine.studio.intent import MetricVocabulary
from astro_mine.studio.models import AssetSelection, DesignCandidate, IntentDraft
from astro_mine.studio.orchestrate import local_clients
from astro_mine.studio.workspace import InMemoryWorkspace
from fastapi.testclient import TestClient

from astro_mine_api.studio import create_app


def test_healthz() -> None:
    client = TestClient(create_app())
    body = client.get("/studio/healthz").json()
    # The one shape every surface answers with (api#4); `tests/test_health.py` owns the convergence.
    assert body["status"] == "ok" and body["component"] == "studio"


def test_capture_intent_endpoint(lunar_draft: IntentDraft) -> None:
    # exercise the injected-dependency branches
    client = TestClient(create_app(workspace=InMemoryWorkspace(), clients=local_clients()))
    response = client.post(
        "/studio/intent",
        json={
            "draft": lunar_draft.model_dump(mode="json"),
            "vocabulary": {"metrics": {"water_production_rate": "", "power_margin": ""}},
            "model": "claude-opus-4-8",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["digest"].startswith("sha256:")
    assert body["document"]["objective"]["id"] == "lunar-ice"


def test_capture_rejects_unknown_metric(lunar_draft: IntentDraft) -> None:
    client = TestClient(create_app())
    response = client.post(
        "/studio/intent",
        json={
            "draft": lunar_draft.model_dump(mode="json"),
            "vocabulary": {"metrics": {"only_this": ""}},
        },
    )
    assert response.status_code == 422


def test_capture_rejects_empty_objective(lunar_draft: IntentDraft) -> None:
    empty = lunar_draft.model_copy(update={"products": [], "constraints": []})
    response = TestClient(create_app()).post(
        "/studio/intent", json={"draft": empty.model_dump(mode="json")}
    )
    assert response.status_code == 422


def test_run_study_endpoint(objective_doc: ObjectiveDocument) -> None:
    client = TestClient(create_app())
    candidate = DesignCandidate(id="cand", swarm=[AssetSelection(sadf_ref="rover", count=3)])
    response = client.post(
        "/studio/studies",
        json={
            "objective": objective_doc.model_dump(mode="json"),
            "candidates": [candidate.model_dump(mode="json")],
            "seeds": [1, 2],
            "max_steps": 4,
        },
    )
    assert response.status_code == 200
    statuses = [job["status"] for job in response.json()["jobs"]]
    assert statuses == ["succeeded", "succeeded"]


def test_run_study_returns_a_comparable_trade_study(objective_doc: ObjectiveDocument) -> None:
    # The journey bridge: /studies must return the reproducible TradeStudy the comparison consumes,
    # not only job bookkeeping. Two candidates so the Pareto front is meaningful.
    client = TestClient(create_app())
    candidates = [
        DesignCandidate(id="a", swarm=[AssetSelection(sadf_ref="rover", count=2)]),
        DesignCandidate(id="b", swarm=[AssetSelection(sadf_ref="rover", count=4)]),
    ]
    study_resp = client.post(
        "/studio/studies",
        json={
            "objective": objective_doc.model_dump(mode="json"),
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            "seeds": [7],
        },
    )
    assert study_resp.status_code == 200, study_resp.text
    study = study_resp.json()["study"]
    assert study is not None
    assert {evaluated["candidate"]["id"] for evaluated in study["evaluated"]} == {"a", "b"}
    assert study["pareto_front"]  # a non-empty front
    assert study["backend"] == "batch"

    # The returned study feeds /studies/comparison directly — the loop connects with no new route.
    comparison = client.post("/studio/studies/comparison", json=study)
    assert comparison.status_code == 200, comparison.text
    assert {row["candidate_id"] for row in comparison.json()["candidates"]} == {"a", "b"}


def test_capture_then_study_shares_objective(lunar_draft: IntentDraft) -> None:
    # a small end-to-end: goal-in via forms, then a study over the captured objective
    client = TestClient(create_app())
    vocab = MetricVocabulary(metrics={"water_production_rate": "", "power_margin": ""})
    captured = client.post(
        "/studio/intent",
        json={
            "draft": lunar_draft.model_dump(mode="json"),
            "vocabulary": vocab.model_dump(mode="json"),
        },
    ).json()
    candidate = DesignCandidate(id="cand", swarm=[AssetSelection(sadf_ref="rover", count=2)])
    study = client.post(
        "/studio/studies",
        json={"objective": captured["document"], "candidates": [candidate.model_dump(mode="json")]},
    )
    assert study.status_code == 200
    assert study.json()["jobs"][0]["status"] == "succeeded"
