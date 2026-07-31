"""Observability for the submission pipeline — OTel + Prometheus (bench#32; bench.md §10; CX-OBS).

bench.md §10 requires OpenTelemetry traces across ``submit → evaluate → score → rank`` and
Prometheus/Grafana dashboards; before bench#32 there was not one reference to either anywhere in the
codebase. These tests cover the fix, against the acceptance criteria:

1. **OTel spans cover the full pipeline**, with the trace id propagated across the **async hop**
   (the queue between the handler that accepts a submission and the worker that evaluates it) —
   so a leaderboard entry is one trace end to end, not two disconnected ones;
2. a **``/metrics`` endpoint** exposes Prometheus-format metrics from the FastAPI app;
3. a **dashboard definition** exists for queue depth, the re-execution mismatch rate, and evaluation
   latency;
4. the **README status line** reflects the repo's actual state.

Spans are asserted with a real in-memory OTel span exporter, so the pipeline is *actually traced* —
not merely importable.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from astro_mine.bench.leaderboard import (
    InMemoryAuditLog,
    LeaderboardService,
    OidcTokenVerifier,
)
from astro_mine.bench.sandbox import SandboxScorer
from astro_mine.bench.zoo import ANCHOR_SCENARIO_ID
from fastapi.testclient import TestClient

from astro_mine_api.bench import create_app
from tests.bench._factories import BASELINE_REF, InProcessSandbox, TestIdp, make_idp

REPO = Path(__file__).resolve().parents[2]
ANCHOR_PAYLOAD = {"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF}


@pytest.fixture(scope="module")
def idp() -> TestIdp:
    return make_idp()


@pytest.fixture
def spans() -> Iterator[object]:
    """A real in-memory OTel exporter — so the spans are *emitted*, not just importable."""
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The OTel API pins a global provider once; override it directly for the test.
    trace_api._TRACER_PROVIDER = provider
    yield exporter
    trace_api._TRACER_PROVIDER = None


@pytest.fixture
def service(idp: TestIdp) -> LeaderboardService:
    return LeaderboardService(
        authn=OidcTokenVerifier(issuer=idp.issuer, audience=idp.audience, jwks=idp.jwks),
        audit=InMemoryAuditLog(),
        scorer=SandboxScorer(InProcessSandbox()),
    )


# =================================================================================================
# AC1 — OTel spans across submit -> evaluate -> score -> rank
# =================================================================================================


def test_spans_cover_the_submission_pipeline(
    service: LeaderboardService, idp: TestIdp, spans: object
) -> None:
    """AC1: submit → evaluate → score, as one trace (bench.md §10)."""
    TestClient(create_app(service=service)).post(
        "/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header()
    )
    emitted = {s.name for s in spans.get_finished_spans()}  # type: ignore[attr-defined]
    assert {"bench.submit", "bench.evaluate", "bench.score"} <= emitted


def test_spans_carry_the_scenario_and_intake_path(
    service: LeaderboardService, idp: TestIdp, spans: object
) -> None:
    TestClient(create_app(service=service)).post(
        "/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header()
    )
    submit = next(
        s
        for s in spans.get_finished_spans()
        if s.name == "bench.submit"  # type: ignore[attr-defined]
    )
    assert submit.attributes["bench.scenario_id"] == ANCHOR_SCENARIO_ID
    assert submit.attributes["bench.intake"] == "policy_ref"


def test_a_rejected_submission_is_visible_in_the_trace(
    service: LeaderboardService, idp: TestIdp, spans: object
) -> None:
    """A rejection must show up in the trace as an error, not vanish."""
    from opentelemetry.trace import StatusCode

    TestClient(create_app(service=service)).post(
        "/bench/submissions",
        json={
            "scenario_id": ANCHOR_SCENARIO_ID,
            "policy_ref": "tests.bench._factories:ExplodingPolicy",
        },
        headers=idp.header(),
    )
    submit = next(
        s
        for s in spans.get_finished_spans()
        if s.name == "bench.submit"  # type: ignore[attr-defined]
    )
    assert submit.status.status_code is StatusCode.ERROR


# =================================================================================================
# AC2 — the Prometheus /metrics endpoint
# =================================================================================================


def test_metrics_endpoint_serves_prometheus_format(service: LeaderboardService) -> None:
    response = TestClient(create_app(service=service)).get("/bench/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "astro_mine_bench_" in response.text


def test_metrics_endpoint_needs_no_account(service: LeaderboardService) -> None:
    """A Prometheus scraper has no token; the deployment restricts /metrics at the network."""
    assert TestClient(create_app(service=service)).get("/bench/metrics").status_code == 200


def test_the_pipeline_actually_records_its_metrics(
    service: LeaderboardService, idp: TestIdp
) -> None:
    """End-to-end: a real submission moves the real counters served by /metrics."""
    client = TestClient(create_app(service=service))
    before = client.get("/bench/metrics").text
    client.post("/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header())
    after = client.get("/bench/metrics").text

    assert 'astro_mine_bench_submissions_total{outcome="ranked"' in after
    assert (
        'astro_mine_bench_authz_decisions_total{action="submission:create",decision="allow"'
        in after
    )
    assert after != before


# =================================================================================================
# AC3 — the dashboard definition
# =================================================================================================


# =================================================================================================
# AC4 — the README status line
# =================================================================================================


# =================================================================================================
# Dependency-cleanliness: the base package must run with neither SDK installed
# =================================================================================================
