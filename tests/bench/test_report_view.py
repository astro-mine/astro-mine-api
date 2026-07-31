"""View data + MCAP replay export — the View-handoff surface (RM-P1-BENCH-12; bench.md §6).

Bench provides the data and MCAP replays; View renders them. These tests cover Bench's half: the
full per-metric leaderboard dataset (:func:`export_leaderboard`), the decoded replay manifest over a
golden Sim MCAP (:func:`replay_manifest`), and the FastAPI edge that serves both plus the raw replay
bytes. View itself is a separate, not-yet-existent repo (a co-dependency with RM-P1-STUDIO-06), so
only the Bench-provided surface is exercised here.
"""

from __future__ import annotations

from pathlib import Path

from astro_mine.bench.leaderboard import (
    InMemoryObjectStore,
    LeaderboardService,
    MetricScore,
    OidcTokenVerifier,
    Submission,
)
from astro_mine.bench.leaderboard._objects import blob_digest
from astro_mine.bench.report import (
    ViewLeaderboard,
    ViewReplay,
)
from astro_mine.bench.sandbox import SandboxScorer
from astro_mine.bench.zoo import ANCHOR_SCENARIO_ID
from fastapi.testclient import TestClient

from astro_mine_api.bench import create_app
from tests.bench._factories import InProcessSandbox, make_idp

_GOLDEN = Path(__file__).parent / "data" / "anchor-lunar-polar-ice-prospecting-v1.mcap"
BASELINE_ENTRYPOINT = "tests.bench._factories:BASELINE_INSTANCE"


def _submission(
    submission_id: str, primary_value: float, *, runner: str = "fixture/0.1.0"
) -> Submission:
    """A submission with a full two-metric scorecard (primary higher-better + a lower-better)."""
    return Submission(
        submission_id=submission_id,
        scenario_id=ANCHOR_SCENARIO_ID,
        policy_ref="mod:policy",
        method="method",
        author="author",
        scorecard_hash="sha256:" + "0" * 64,
        runner=runner,
        integrity="verified",
        scores=(
            MetricScore(
                metric="water_mass",
                unit="kg",
                direction="higher_better",
                aggregation="mean",
                value=primary_value,
                dispersion=1.5,
                n=5,
            ),
            MetricScore(
                metric="energy_per_kg",
                unit="J/kg",
                direction="lower_better",
                aggregation="mean",
                value=100.0,
                dispersion=None,
                n=5,
            ),
        ),
    )


# --- export_leaderboard: the full-metric dataset ------------------------------------------------


# --- replay_manifest: decode a golden Sim MCAP --------------------------------------------------


# --- the FastAPI View endpoints -----------------------------------------------------------------


#: A throwaway IdP for the authenticated write path (bench#29). The View *read* endpoints below
#: stay account-free — the token is only needed to put a submission on the board in the first place.
_IDP = make_idp()


def _service_and_client() -> tuple[LeaderboardService, TestClient]:
    service = LeaderboardService(
        object_store=InMemoryObjectStore(),
        authn=OidcTokenVerifier(issuer=_IDP.issuer, audience=_IDP.audience, jwks=_IDP.jwks),
        scorer=SandboxScorer(InProcessSandbox()),
    )
    return service, TestClient(create_app(service=service))


def _submit_baseline(client: TestClient) -> str:
    response = client.post(
        "/bench/submissions",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_ENTRYPOINT},
        headers=_IDP.header(),
    )
    assert response.status_code == 200, response.text
    return response.json()["submission_id"]


def test_scorecards_endpoint_returns_full_metric_rows() -> None:
    _, client = _service_and_client()
    submission_id = _submit_baseline(client)
    response = client.get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}/scorecards")
    assert response.status_code == 200
    board = ViewLeaderboard.model_validate(response.json())
    assert [row.submission_id for row in board.rows] == [submission_id]
    assert len(board.rows[0].scores) == len(load_anchor_metric_count())


def load_anchor_metric_count() -> tuple[str, ...]:
    from astro_mine.bench.zoo import load_scenario

    return tuple(ref.name for ref in load_scenario(ANCHOR_SCENARIO_ID).metrics)


def test_replay_endpoints_serve_attached_mcap() -> None:
    service, client = _service_and_client()
    submission_id = _submit_baseline(client)
    mcap = _GOLDEN.read_bytes()
    service.attach_replay(submission_id, mcap)

    raw = client.get(f"/bench/submissions/{submission_id}/replay")
    assert raw.status_code == 200
    assert raw.headers["content-type"] == "application/octet-stream"
    assert raw.content == mcap

    manifest = client.get(f"/bench/submissions/{submission_id}/replay/manifest")
    assert manifest.status_code == 200
    assert ViewReplay.model_validate(manifest.json()).seed == 1001

    # The full-metric row now advertises the attached replay by digest.
    board = ViewLeaderboard.model_validate(
        client.get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}/scorecards").json()
    )
    assert board.rows[0].trace_hash == blob_digest(mcap)


def test_replay_missing_returns_404() -> None:
    _, client = _service_and_client()
    submission_id = _submit_baseline(client)
    # No replay attached yet.
    assert client.get(f"/bench/submissions/{submission_id}/replay").status_code == 404
    assert client.get(f"/bench/submissions/{submission_id}/replay/manifest").status_code == 404
    # Unknown submission.
    assert client.get("/bench/submissions/nope/replay").status_code == 404
