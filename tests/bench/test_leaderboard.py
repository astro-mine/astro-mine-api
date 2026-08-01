"""The leaderboard service (RM-P0-BENCH-06; bench#29, bench#30).

Covers the dependency-clean core (policy-ref resolution, held-out seed disclosure, submit-we-run
evaluation with sampled re-execution, ranking), the pluggable store (in-memory + the SQLAlchemy
``SqlStore`` on SQLite), and the FastAPI endpoints — the last two parametrized so the SQL backend
is exercised end-to-end through the API.

Since bench#29/#30 the write surface is **authenticated** (an OIDC bearer token, minted here against
a freshly-generated RSA key) and submissions are **scored through a sandbox seam** rather than
imported into the evaluator. The isolation the sandbox actually enforces is asserted in
``tests/test_sandbox.py``; the authN/authZ/audit/supply-chain layer in
``tests/test_leaderboard_security.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from astro_mine.bench.baseline import BaselinePolicy
from astro_mine.bench.leaderboard import (
    InMemoryStore,
    LeaderboardService,
    LeaderboardStore,
    MetricScore,
    PolicyReferenceError,
    Submission,
    build_submission,
    evaluate,
    load_heldout_seeds,
    rank,
    resolve_policy,
    validate_policy_ref,
)
from astro_mine.bench.leaderboard._sql import SqlStore
from astro_mine.bench.sandbox import SandboxScorer
from astro_mine.bench.scenario import ScenarioSpec
from astro_mine.bench.zoo import ANCHOR_SCENARIO_ID, load_scenario
from astro_mine.core.messages import Action, ActionBatch, ModeCommand
from astro_mine.core.messages.enums import ActionKind
from astro_mine.core.policy import DecisionContext
from fastapi.testclient import TestClient

from astro_mine_api.bench import create_app
from astro_mine_api.bench._app import DB_ENV, _default_store
from tests.bench._factories import InProcessSandbox, TestIdp, make_idp

BASELINE_REF = "astro_mine.bench.baseline:BaselinePolicy"
INSTANCE_REF = "tests.bench._factories:BASELINE_INSTANCE"
FACTORY_REF = "tests.bench._factories:idle_baseline"
ANCHOR_METRICS = frozenset(
    {
        "water_mass",
        "energy_per_kg",
        "information_gain",
        "psr_area_characterized",
        "nights_survived",
        "comms_robustness",
        "discovery_latency",
    }
)


@pytest.fixture(scope="module")
def anchor() -> ScenarioSpec:
    return load_scenario(ANCHOR_SCENARIO_ID)


@pytest.fixture(scope="module")
def idp() -> TestIdp:
    """A throwaway IdP: a real RSA key, its JWKS, and a minter for real RS256 bearer tokens."""
    return make_idp()


@pytest.fixture(scope="module")
def scorer() -> SandboxScorer:
    """The execution seam, over the fast in-process sandbox double (see tests/_factories)."""
    return SandboxScorer(InProcessSandbox())


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest, tmp_path: object) -> Iterator[LeaderboardStore]:
    if request.param == "sql":
        sql = SqlStore(f"sqlite:///{tmp_path / 'lb.db'}")  # type: ignore[operator]
        yield sql
        sql.dispose()
    else:
        yield InMemoryStore()


def _service(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer, **kwargs: object
) -> LeaderboardService:
    from astro_mine.bench.leaderboard import OidcTokenVerifier

    return LeaderboardService(
        store=store,
        authn=OidcTokenVerifier(issuer=idp.issuer, audience=idp.audience, jwks=idp.jwks),
        scorer=scorer,
        **kwargs,  # type: ignore[arg-type]
    )


def _client(store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer) -> TestClient:
    return TestClient(create_app(service=_service(store, idp, scorer)))


def _submission(
    submission_id: str,
    *,
    scenario: str = "s",
    value: float | None = 1.0,
    direction: str = "higher_better",
    method: str | None = None,
) -> Submission:
    return Submission(
        submission_id=submission_id,
        scenario_id=scenario,
        policy_ref="m:p",
        method=method,
        author=None,
        scorecard_hash="sha256:" + "0" * 64,
        runner="fixture/0.1.0",
        integrity="verified",
        scores=(
            MetricScore(
                metric="water_mass",
                unit="kg",
                direction=direction,
                aggregation="mean",
                value=value,
                dispersion=None,
                n=1,
            ),
        ),
    )


class _NondeterministicPolicy:
    """A stateful policy whose action stream drifts across calls — trips the integrity check."""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, observations: object, context: DecisionContext) -> ActionBatch:
        self.calls += 1
        return ActionBatch(
            actions=[
                Action(agent_id="x", kind=ActionKind.MODE, mode=ModeCommand(mode=str(self.calls)))
            ]
        )


# --- store (parametrized over in-memory + SqlStore/SQLite) --------------------------------------


def test_store_roundtrip(store: LeaderboardStore) -> None:
    submission = _submission("sha256:aaa")
    store.add_submission(submission)
    assert store.get_submission("sha256:aaa") == submission
    assert store.get_submission("missing") is None


def test_store_lists_by_scenario_in_stable_order(store: LeaderboardStore) -> None:
    store.add_submission(_submission("sha256:b", scenario="x"))
    store.add_submission(_submission("sha256:a", scenario="x"))
    store.add_submission(_submission("sha256:c", scenario="y"))
    assert [s.submission_id for s in store.list_submissions("x")] == ["sha256:a", "sha256:b"]


def test_store_add_is_idempotent(store: LeaderboardStore) -> None:
    store.add_submission(_submission("sha256:a", value=1.0))
    store.add_submission(_submission("sha256:a", value=2.0))
    listed = store.list_submissions("s")
    assert len(listed) == 1 and listed[0].scores[0].value == 2.0


# --- policy-ref resolution ----------------------------------------------------------------------


def test_resolve_policy_class_instance_and_factory() -> None:
    assert isinstance(resolve_policy(BASELINE_REF), BaselinePolicy)  # a class
    assert isinstance(resolve_policy(INSTANCE_REF), BaselinePolicy)  # an instance
    assert isinstance(resolve_policy(FACTORY_REF), BaselinePolicy)  # a zero-arg factory


@pytest.mark.parametrize(
    "ref",
    [
        "no-colon-here",
        "astro_mine.bench.baseline:Missing",
        "no_such_module_xyz:thing",
        "astro_mine.bench.leaderboard:InMemoryStore",  # resolves, but is not a Policy
    ],
)
def test_resolve_policy_rejects_bad_refs(ref: str) -> None:
    with pytest.raises(PolicyReferenceError):
        resolve_policy(ref)


# --- held-out seeds -----------------------------------------------------------------------------


def test_load_heldout_seeds_for_the_anchor() -> None:
    seeds = load_heldout_seeds(ANCHOR_SCENARIO_ID)
    assert len(seeds) == 12
    assert all(isinstance(seed, int) for seed in seeds)


def test_load_heldout_seeds_missing(tmp_path: object) -> None:
    with pytest.raises(FileNotFoundError):
        load_heldout_seeds("nope", embargo_root=tmp_path)  # type: ignore[arg-type]


# --- submit-we-run evaluation -------------------------------------------------------------------


def test_evaluate_scores_heldout_and_verifies(anchor: ScenarioSpec, scorer: SandboxScorer) -> None:
    seeds = load_heldout_seeds(ANCHOR_SCENARIO_ID)
    card, integrity = evaluate(anchor, BASELINE_REF, seeds=seeds, scorer=scorer)
    assert integrity == "verified"
    assert {m.metric for m in card.metrics} == ANCHOR_METRICS
    assert all(m.seeds == seeds for m in card.metrics)  # scored on the held-out seeds


def test_evaluate_takes_a_reference_not_a_policy_object(scorer: SandboxScorer) -> None:
    """bench#30: the evaluator's scoring API is typed on a *string*, never a live Policy.

    A signature that accepts a ``Policy`` has already imported and constructed untrusted code in the
    evaluator's process — the exact posture bench.md §9 forbids. This is a type-level guarantee, so
    it is worth pinning as a test: passing a Policy object must not be silently accepted.
    """
    spec = load_scenario(ANCHOR_SCENARIO_ID)
    with pytest.raises((AttributeError, TypeError, ValueError)):
        scorer(spec, BaselinePolicy(), seeds=(1,))  # type: ignore[arg-type]


def test_evaluate_flags_a_nondeterministic_policy(
    anchor: ScenarioSpec, scorer: SandboxScorer
) -> None:
    seeds = load_heldout_seeds(ANCHOR_SCENARIO_ID)
    _, integrity = evaluate(
        anchor, "tests.bench._factories:NondeterministicPolicy", seeds=seeds, scorer=scorer
    )
    assert integrity == "flagged"


def test_build_submission_is_content_addressed(anchor: ScenarioSpec, scorer: SandboxScorer) -> None:
    from astro_mine.bench.leaderboard import SubmissionRequest

    seeds = load_heldout_seeds(ANCHOR_SCENARIO_ID)
    card, integrity = evaluate(anchor, BASELINE_REF, seeds=seeds, scorer=scorer)
    request = SubmissionRequest(scenario_id=ANCHOR_SCENARIO_ID, policy_ref=BASELINE_REF)
    first = build_submission(request, card, integrity)
    assert first.submission_id.startswith("sha256:")
    assert first == build_submission(request, card, integrity)  # deterministic
    assert len(first.scores) == 7


def test_validate_policy_ref_checks_shape_without_importing() -> None:
    """bench#30: the edge rejects a malformed ref without *importing* it (importing = executing)."""
    assert validate_policy_ref(BASELINE_REF) == BASELINE_REF
    # A well-shaped but unimportable reference passes the *shape* check here — discovering it does
    # not import is the sandboxed worker's job, and it hands that back as data.
    assert validate_policy_ref("no_such_module_xyz:thing") == "no_such_module_xyz:thing"
    for bad in ("no-colon-here", ":attr", "module:", "  :  "):
        with pytest.raises(PolicyReferenceError):
            validate_policy_ref(bad)


# --- ranking ------------------------------------------------------------------------------------


def test_rank_empty() -> None:
    assert rank([]) == []


def test_rank_orders_higher_better_descending() -> None:
    entries = rank(
        [
            _submission("sha256:a", value=1.0),
            _submission("sha256:b", value=3.0),
            _submission("sha256:c", value=2.0),
        ]
    )
    assert [e.submission_id for e in entries] == ["sha256:b", "sha256:c", "sha256:a"]
    assert [e.rank for e in entries] == [1, 2, 3]


def test_rank_orders_lower_better_ascending() -> None:
    entries = rank(
        [
            _submission("sha256:a", value=3.0, direction="lower_better"),
            _submission("sha256:b", value=1.0, direction="lower_better"),
        ]
    )
    assert [e.submission_id for e in entries] == ["sha256:b", "sha256:a"]


def test_rank_puts_na_last_and_breaks_ties_by_id() -> None:
    entries = rank(
        [
            _submission("sha256:z", value=None),
            _submission("sha256:b", value=5.0),
            _submission("sha256:a", value=5.0),
        ]
    )
    assert [e.submission_id for e in entries] == ["sha256:a", "sha256:b", "sha256:z"]


# --- the FastAPI service (parametrized over both stores) ----------------------------------------
#
# Every write now carries an OIDC bearer token (bench#29). The unauthenticated-rejection cases live
# in tests/test_leaderboard_security.py; here the token is present and the focus stays on the
# submit → score → rank behaviour.


def test_healthz(store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer) -> None:
    body = _client(store, idp, scorer).get("/bench/healthz").json()
    # The one shape every surface answers with (api#4); `tests/test_health.py` owns the convergence.
    assert body["status"] == "ok" and body["component"] == "bench"


def test_submit_scores_on_heldout_and_verifies(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer
) -> None:
    response = _client(store, idp, scorer).post(
        "/bench/submissions",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF, "method": "baseline"},
        headers=idp.header(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["integrity"] == "verified"
    assert body["scenario_id"] == ANCHOR_SCENARIO_ID
    assert len(body["scores"]) == 7
    assert body["submission_id"].startswith("sha256:")


def test_submit_dispatches_through_the_sandbox_seam(store: LeaderboardStore, idp: TestIdp) -> None:
    """bench#30 AC1: the local ``policy_ref`` path executes out-of-process, not in the evaluator.

    Asserted structurally: every held-out seed must have been dispatched to the sandbox as a
    :class:`WorkerInvocation` carrying the *reference string*. If the service ever went back to
    importing the policy itself, the sandbox would see nothing.
    """
    sandbox = InProcessSandbox()
    client = TestClient(create_app(service=_service(store, idp, SandboxScorer(sandbox))))
    client.post(
        "/bench/submissions",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF},
        headers=idp.header(),
    )
    heldout = load_heldout_seeds(ANCHOR_SCENARIO_ID)
    dispatched = {inv.seed for inv in sandbox.invocations}
    assert dispatched >= set(heldout)  # every held-out seed went through the sandbox
    assert {inv.policy_ref for inv in sandbox.invocations} == {BASELINE_REF}


def test_submit_is_idempotent(store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer) -> None:
    client = _client(store, idp, scorer)
    payload = {"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF}
    first = client.post("/bench/submissions", json=payload, headers=idp.header()).json()
    again = client.post("/bench/submissions", json=payload, headers=idp.header()).json()
    assert first["submission_id"] == again["submission_id"]
    assert len(client.get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}").json()) == 1


def test_leaderboard_ranks_two_policies(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer
) -> None:
    client = _client(store, idp, scorer)
    for ref in (BASELINE_REF, FACTORY_REF):
        client.post(
            "/bench/submissions",
            json={"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": ref},
            headers=idp.header(),
        )
    board = client.get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}").json()
    assert [e["rank"] for e in board] == [1, 2]
    assert board[0]["primary_metric"] == "water_mass"
    assert board[0]["primary_value"] >= board[1]["primary_value"]  # higher-better, sorted


def test_read_paths_need_no_account(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer
) -> None:
    """bench#29 AC5: the local/offline tier's read + score paths stay usable with no token."""
    client = _client(store, idp, scorer)
    client.post(
        "/bench/submissions",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF},
        headers=idp.header(),
    )
    # No Authorization header on any of these.
    assert client.get("/bench/healthz").status_code == 200
    assert client.get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}").status_code == 200
    assert client.get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}/scorecards").status_code == 200
    assert client.get("/bench/scenarios").status_code == 200
    assert client.get("/bench/metrics").status_code == 200


def test_get_submission_roundtrip_and_missing(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer
) -> None:
    client = _client(store, idp, scorer)
    submission_id = client.post(
        "/bench/submissions",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF},
        headers=idp.header(),
    ).json()["submission_id"]
    assert (
        client.get(f"/bench/submissions/{submission_id}").json()["submission_id"] == submission_id
    )
    assert client.get("/bench/submissions/sha256:deadbeef").status_code == 404


def test_submit_rejects_bad_policy_ref(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer
) -> None:
    response = _client(store, idp, scorer).post(
        "/bench/submissions",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": "not-a-ref"},
        headers=idp.header(),
    )
    assert response.status_code == 400


def test_submit_rejects_a_policy_that_will_not_run(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer
) -> None:
    """A submission that raises inside the sandbox is rejected as data — never seen by the app."""
    response = _client(store, idp, scorer).post(
        "/bench/submissions",
        json={
            "scenario_id": ANCHOR_SCENARIO_ID,
            "policy_ref": "tests.bench._factories:ExplodingPolicy",
        },
        headers=idp.header(),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "submission_rejected"


def test_submit_unknown_scenario(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer
) -> None:
    response = _client(store, idp, scorer).post(
        "/bench/submissions",
        json={"scenario_id": "nope", "policy_ref": BASELINE_REF},
        headers=idp.header(),
    )
    assert response.status_code == 404


def test_submit_malformed_body_is_422(
    store: LeaderboardStore, idp: TestIdp, scorer: SandboxScorer
) -> None:
    response = _client(store, idp, scorer).post(
        "/bench/submissions", json={"scenario_id": ANCHOR_SCENARIO_ID}, headers=idp.header()
    )
    assert response.status_code == 422


# --- default store selection --------------------------------------------------------------------


def test_sqlstore_requires_url_or_engine() -> None:
    with pytest.raises(ValueError, match="url or an engine"):
        SqlStore()


def test_store_remove_submission(store: LeaderboardStore) -> None:
    store.add_submission(_submission("sha256:a", scenario="x"))
    store.remove_submission("sha256:a")
    assert store.get_submission("sha256:a") is None
    store.remove_submission("sha256:a")  # removing an absent entry is a no-op


def test_default_store_selects_sql_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setenv(DB_ENV, f"sqlite:///{tmp_path / 'x.db'}")  # type: ignore[operator]
    selected = _default_store()
    assert isinstance(selected, SqlStore)
    selected.dispose()


def test_create_app_without_store_defaults_to_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DB_ENV, raising=False)
    assert isinstance(_default_store(), InMemoryStore)
    assert TestClient(create_app()).get("/bench/healthz").status_code == 200


def test_submit_missing_heldout_seeds_is_404(
    idp: TestIdp, scorer: SandboxScorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import astro_mine.bench.leaderboard._service as service_module

    def _no_seeds(scenario_id: str) -> tuple[int, ...]:
        raise FileNotFoundError("no sealed seed set")

    monkeypatch.setattr(service_module, "load_heldout_seeds", _no_seeds)
    client = _client(InMemoryStore(), idp, scorer)
    response = client.post(
        "/bench/submissions",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF},
        headers=idp.header(),
    )
    assert response.status_code == 404


def test_sample_reproduces_rejects_an_unknown_metric() -> None:
    from astro_mine.bench.leaderboard._eval import _sample_reproduces
    from astro_mine.bench.metrics import AggregateScore, Scorecard
    from astro_mine.core.objective import MetricAggregation, MetricDirection

    def _card(metric: str) -> Scorecard:
        return Scorecard(
            scenario_id="s",
            runner="fixture/0.1.0",
            metrics=(
                AggregateScore(
                    metric=metric,
                    version="0.1.0",
                    unit="u",
                    direction=MetricDirection.HIGHER_BETTER,
                    aggregation=MetricAggregation.MEAN,
                    value=1.0,
                    dispersion=None,
                    n=1,
                    seeds=(1,),
                    per_seed=(1.0,),
                ),
            ),
        )

    # a metric present in the re-execution but absent from the full run cannot be verified
    assert _sample_reproduces(_card("water_mass"), _card("other_metric")) is False
