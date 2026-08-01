"""The leaderboard routes the ported suites left uncovered — authoring, jobs, provenance, 404s.

The three suites that came across from astro-mine-bench cover the submission paths, the security
posture and the View handoff exhaustively. What they never reach is the rest of the route module:
`POST /bench/scenarios` (the hosted zoo's write surface), `GET /bench/jobs/{id}`, the provenance
and replay-manifest 404s, and the "unknown scenario" arm of Hub intake. Those are this
distribution's code, so they are this distribution's tests.

Every route here is exercised through the real service — the same `LeaderboardService` the other
suites build — because a route test that mocks the thing the route delegates to proves only that
the mock was called.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from astro_mine.bench.leaderboard import (
    InMemoryAuditLog,
    InMemoryObjectStore,
    LeaderboardService,
    OidcTokenVerifier,
)
from astro_mine.bench.sandbox import SandboxScorer
from astro_mine.bench.scenario import ScenarioSpec
from astro_mine.bench.zoo import ANCHOR_SCENARIO_ID, CatalogEntry, ScenarioCatalog, load_scenario
from fastapi.testclient import TestClient

from astro_mine_api.bench import create_app
from tests.bench._factories import BASELINE_REF, InProcessSandbox, TestIdp, make_idp

ANCHOR_PAYLOAD = {"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF}


class _WritableZoo:
    """The hosted (Postgres/pgvector) catalog's write surface, in memory.

    `POST /bench/scenarios` branches on `isinstance(zoo, WritableCatalog)` — a runtime-checkable
    Protocol — so what the route needs is something that *has* `upsert`, not the Postgres catalog
    itself. Standing up Postgres to prove a 201 would test SQLAlchemy, not the route.
    """

    def __init__(self, seed: ScenarioSpec) -> None:
        self._specs: dict[str, ScenarioSpec] = {seed.scenario_id: seed}
        self.upserted: list[ScenarioSpec] = []

    # --- ScenarioCatalog ---
    def list_scenarios(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def load_scenario(self, scenario_id: str) -> ScenarioSpec:
        try:
            return self._specs[scenario_id]
        except KeyError as exc:
            raise KeyError(f"no scenario {scenario_id!r}") from exc

    def entries(self) -> tuple[CatalogEntry, ...]:  # pragma: no cover - not on a route path
        raise NotImplementedError

    # --- WritableCatalog ---
    def upsert(self, spec: ScenarioSpec) -> object:
        self._specs[spec.scenario_id] = spec
        self.upserted.append(spec)
        return spec

    def seed_from(self, source: ScenarioCatalog) -> tuple[CatalogEntry, ...]:  # pragma: no cover
        raise NotImplementedError

    def search(self, query: str | Sequence[float], *, limit: int = 5) -> list[object]:
        raise NotImplementedError  # pragma: no cover

    def lineage(self, scenario_id: str) -> tuple[CatalogEntry, ...]:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture(scope="module")
def idp() -> TestIdp:
    return make_idp()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def service(idp: TestIdp, audit: InMemoryAuditLog) -> Iterator[LeaderboardService]:
    yield LeaderboardService(
        object_store=InMemoryObjectStore(),
        authn=OidcTokenVerifier(issuer=idp.issuer, audience=idp.audience, jwks=idp.jwks),
        audit=audit,
        scorer=SandboxScorer(InProcessSandbox()),
    )


@pytest.fixture
def anchor() -> ScenarioSpec:
    return load_scenario(ANCHOR_SCENARIO_ID)


@pytest.fixture
def zoo(anchor: ScenarioSpec) -> _WritableZoo:
    return _WritableZoo(anchor)


@pytest.fixture
def client(service: LeaderboardService) -> TestClient:
    return TestClient(create_app(service=service))


# --- POST /bench/scenarios: the hosted zoo's write surface ----------------------------------


def test_authoring_a_scenario_needs_a_writable_catalog(
    service: LeaderboardService, idp: TestIdp, anchor: ScenarioSpec
) -> None:
    """The packaged filesystem zoo ships in the wheel and must not mutate at runtime, so a
    deployment running on it refuses authoring rather than silently dropping the write."""
    client = TestClient(create_app(service=service))  # default catalog: the packaged zoo
    response = client.post(
        "/bench/scenarios",
        json=anchor.model_dump(mode="json"),
        headers=idp.header(roles=("maintainer",)),
    )
    assert response.status_code == 503
    assert response.json()["code"] == "capability_unavailable"


def test_a_maintainer_can_author_into_the_hosted_catalog(
    service: LeaderboardService, zoo: _WritableZoo, idp: TestIdp, anchor: ScenarioSpec
) -> None:
    client = TestClient(create_app(service=service, catalog=zoo))
    authored = anchor.model_copy(update={"scenario_id": "authored-v1"})
    response = client.post(
        "/bench/scenarios",
        json=authored.model_dump(mode="json"),
        headers=idp.header(roles=("maintainer",)),
    )
    assert response.status_code == 201, response.text
    assert response.json() == {"scenario_id": "authored-v1", "spec_hash": authored.spec_hash}
    assert [spec.scenario_id for spec in zoo.upserted] == ["authored-v1"]
    # It is on the board's read surface immediately — one catalog, not a write-only side channel.
    assert "authored-v1" in client.get("/bench/scenarios").json()


def test_authoring_is_audit_logged(
    service: LeaderboardService,
    zoo: _WritableZoo,
    idp: TestIdp,
    anchor: ScenarioSpec,
    audit: InMemoryAuditLog,
) -> None:
    """Adding to the commons' benchmark catalog is a privileged act, so it leaves a trail."""
    client = TestClient(create_app(service=service, catalog=zoo))
    client.post(
        "/bench/scenarios",
        json=anchor.model_copy(update={"scenario_id": "audited-v1"}).model_dump(mode="json"),
        headers=idp.header(subject="maintainer-7", roles=("maintainer",)),
    )
    recorded = [event for event in audit.query() if event.resource == "audited-v1"]
    assert recorded and recorded[0].subject == "maintainer-7"
    assert str(recorded[0].decision) == "allow"


def test_authoring_is_refused_for_a_plain_submitter(
    service: LeaderboardService, zoo: _WritableZoo, idp: TestIdp, anchor: ScenarioSpec
) -> None:
    client = TestClient(create_app(service=service, catalog=zoo))
    response = client.post(
        "/bench/scenarios",
        json=anchor.model_copy(update={"scenario_id": "sneaky-v1"}).model_dump(mode="json"),
        headers=idp.header(roles=("submitter",)),
    )
    assert response.status_code == 403
    assert zoo.upserted == []  # refused *before* the catalog was touched


def test_authoring_a_malformed_spec_is_422(
    service: LeaderboardService, zoo: _WritableZoo, idp: TestIdp
) -> None:
    client = TestClient(create_app(service=service, catalog=zoo))
    response = client.post(
        "/bench/scenarios",
        json={"scenario_id": "not-a-scenario"},
        headers=idp.header(roles=("maintainer",)),
    )
    assert response.status_code == 422
    assert zoo.upserted == []


def test_authoring_needs_a_token() -> None:
    """Every write is authenticated; the read half of the same path is not (bench#29 AC5)."""
    client = TestClient(create_app(service=LeaderboardService()))
    assert client.post("/bench/scenarios", json={}).status_code == 503  # no IdP ⇒ never open
    assert client.get("/bench/scenarios").status_code == 200


# --- reads that miss ------------------------------------------------------------------------


def test_an_unknown_job_is_404(client: TestClient) -> None:
    response = client.get("/bench/jobs/no-such-job")
    assert response.status_code == 404
    assert response.json()["code"] == "content_not_found"


def test_a_submission_without_a_provenance_bundle_is_404(client: TestClient) -> None:
    assert client.get("/bench/submissions/ghost/provenance").status_code == 404


def test_the_replay_manifest_of_an_unknown_submission_is_404(client: TestClient) -> None:
    """404 on the *submission*, before the replay lookup — a different arm from "no replay"."""
    response = client.get("/bench/submissions/ghost/replay/manifest")
    assert response.status_code == 404
    assert response.json()["code"] == "content_not_found"
    # Both arms carry that code, and *which* one answered is the subject of this test — so this
    # reads the message to tell them apart, which is not the same as branching on it (api#4).
    assert "no submission" in response.json()["detail"]


def test_hub_intake_of_an_unknown_scenario_is_404(idp: TestIdp, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The scenario is resolved before the artifact is: an unknown id is the caller's 404, not a
    supply-chain verdict about a digest that was never looked up."""
    from astro_mine.hub.registry import Registry

    service = LeaderboardService(
        object_store=InMemoryObjectStore(),
        authn=OidcTokenVerifier(issuer=idp.issuer, audience=idp.audience, jwks=idp.jwks),
        registry=Registry(tmp_path / "reg"),
        scorer=SandboxScorer(InProcessSandbox()),
    )
    response = TestClient(create_app(service=service)).post(
        "/bench/submissions/hub",
        json={"scenario_id": "no-such-scenario-v9", "hub_ref": "sha256:" + "a" * 64},
        headers=idp.header(),
    )
    assert response.status_code == 404


def test_a_local_submission_for_an_unknown_scenario_is_404(
    client: TestClient, idp: TestIdp
) -> None:
    response = client.post(
        "/bench/submissions",
        json={"scenario_id": "no-such-scenario-v9", "policy_ref": BASELINE_REF},
        headers=idp.header(),
    )
    assert response.status_code == 404
