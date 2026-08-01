"""Leaderboard authN/Z + submission supply-chain verification + the audit trail (bench#29).

The hosted leaderboard's security posture, asserted against the acceptance criteria:

1. ``POST /submissions`` and ``POST /submissions/hub`` **require a valid OIDC bearer token** —
   unauthenticated, malformed, expired, wrong-audience, wrong-issuer, and forged-signature requests
   are all rejected, and a deployment with no IdP configured refuses writes rather than falling
   open;
2. an **OPA-style policy layer** enforces per-user submission **quotas**, **embargo** control, and
   **scenario/metric authoring** rights — with the in-process engine and a real OPA sidecar deciding
   the same input document alike, both fail-closed;
3. Hub-digest submissions are verified for a **cosign signature, SLSA provenance, and an SBOM**
   before evaluation, reusing Seal's primitives through Hub — and **verification failure fails
   closed** (rejected, never silently accepted);
4. every authN/authZ decision and verification outcome lands in a **queryable audit trail**;
5. the local/offline tier's **read/score paths need no account or token**.

The OIDC tests run against a *real* RSA key generated per run
(``tests.bench._factories.make_idp``) — so the verifier is exercised with real RS256
crypto, and no fixture secret is committed (conventions.md §9).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from astro_mine.bench.leaderboard import (
    Action,
    AttestationPolicy,
    AuditDecision,
    AuthenticationError,
    AuthorizationRequest,
    InMemoryAuditLog,
    InMemoryRateLimiter,
    LeaderboardService,
    OidcTokenVerifier,
    OpaPolicyEngine,
    Principal,
    RbacPolicyEngine,
    Role,
    SubmissionRejected,
    SupplyChainRejected,
    attestation_policy_from_env,
    bearer_token,
    oidc_verifier_from_env,
    policy_engine_from_env,
    verify_submission_attestations,
)
from astro_mine.bench.leaderboard._auth import AUDIENCE_ENV, ISSUER_ENV, JWKS_URL_ENV
from astro_mine.bench.leaderboard._authz import EMBARGOED_SCENARIOS_ENV, OPA_URL_ENV
from astro_mine.bench.leaderboard._supply_chain import TRUSTED_KEY_ENV
from astro_mine.bench.sandbox import SandboxScorer
from astro_mine.bench.scenario import ScenarioSpec
from astro_mine.bench.zoo import ANCHOR_SCENARIO_ID, load_scenario
from astro_mine.core.registry import PluginKind
from astro_mine.core.registry.model import ManifestDocument, PluginManifest
from astro_mine.hub.registry import Blob, Registry
from astro_mine.hub.supply_chain import attest, generate_keypair
from fastapi.testclient import TestClient

from astro_mine_api.bench import create_app
from tests.bench._factories import BASELINE_REF, InProcessSandbox, TestIdp, make_idp

ANCHOR_PAYLOAD = {"scenario_id": ANCHOR_SCENARIO_ID, "policy_ref": BASELINE_REF}


@pytest.fixture(scope="module")
def anchor() -> ScenarioSpec:
    return load_scenario(ANCHOR_SCENARIO_ID)


@pytest.fixture(scope="module")
def idp() -> TestIdp:
    return make_idp()


@pytest.fixture(scope="module")
def verifier(idp: TestIdp) -> OidcTokenVerifier:
    return OidcTokenVerifier(issuer=idp.issuer, audience=idp.audience, jwks=idp.jwks)


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def service(verifier: OidcTokenVerifier, audit: InMemoryAuditLog) -> Iterator[LeaderboardService]:
    yield LeaderboardService(authn=verifier, audit=audit, scorer=SandboxScorer(InProcessSandbox()))


def _client(service: LeaderboardService) -> TestClient:
    return TestClient(create_app(service=service))


def principal(subject: str = "lab-1", *roles: str) -> Principal:
    return Principal(
        subject=subject, issuer="https://idp.test", roles=tuple(roles or ("submitter",))
    )


# =================================================================================================
# AC1 — OIDC bearer-token authentication on the write surface
# =================================================================================================


def test_unauthenticated_submission_is_rejected(service: LeaderboardService) -> None:
    """The headline defect bench#29 names: the leaderboard had no auth of any kind."""
    response = _client(service).post("/bench/submissions", json=ANCHOR_PAYLOAD)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_unauthenticated_hub_submission_is_rejected(service: LeaderboardService) -> None:
    response = _client(service).post(
        "/bench/submissions/hub", json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": "a/b:1.0.0"}
    )
    assert response.status_code == 401


def test_authenticated_submission_is_accepted(service: LeaderboardService, idp: TestIdp) -> None:
    response = _client(service).post(
        "/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header()
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer   ",
        "Basic abcdef",
        "abcdef",
        "Token abcdef",
    ],
)
def test_bearer_token_extraction_fails_closed(header: str | None) -> None:
    with pytest.raises(AuthenticationError):
        bearer_token(header)


def test_bearer_token_is_extracted(idp: TestIdp) -> None:
    token = idp.token()
    assert bearer_token(f"Bearer {token}") == token


def test_expired_token_is_rejected(verifier: OidcTokenVerifier, idp: TestIdp) -> None:
    with pytest.raises(AuthenticationError, match="rejected"):
        verifier.verify(idp.token(expires_in=-3600))


def test_wrong_audience_is_rejected(verifier: OidcTokenVerifier, idp: TestIdp) -> None:
    with pytest.raises(AuthenticationError, match="rejected"):
        verifier.verify(idp.token(audience="some-other-service"))


def test_wrong_issuer_is_rejected(verifier: OidcTokenVerifier, idp: TestIdp) -> None:
    with pytest.raises(AuthenticationError, match="rejected"):
        verifier.verify(idp.token(issuer="https://evil.test"))


def test_token_signed_by_an_unknown_key_is_rejected(verifier: OidcTokenVerifier) -> None:
    """A token minted by a *different* IdP key must not authenticate: the signature is the point."""
    attacker = make_idp()  # a valid-looking token, signed with a key our JWKS does not carry
    with pytest.raises(AuthenticationError):
        verifier.verify(attacker.token(subject="attacker", roles=("admin",)))


def test_unsigned_alg_none_token_is_rejected(verifier: OidcTokenVerifier, idp: TestIdp) -> None:
    """The classic JWT forgery: `alg: none`. Only asymmetric algorithms are admissible."""
    import jwt

    forged = jwt.encode(
        {"sub": "attacker", "iss": idp.issuer, "aud": idp.audience, "exp": 9_999_999_999},
        key="",
        algorithm="none",
    )
    with pytest.raises(AuthenticationError):
        verifier.verify(forged)


def test_garbage_token_is_rejected(verifier: OidcTokenVerifier) -> None:
    with pytest.raises(AuthenticationError, match="malformed"):
        verifier.verify("not-a-jwt-at-all")


def test_token_claims_become_the_principal(verifier: OidcTokenVerifier, idp: TestIdp) -> None:
    caller = verifier.verify(
        idp.token(subject="lab-7", roles=("maintainer", "submitter"), email="a@lab.test")
    )
    assert caller.subject == "lab-7"
    assert caller.issuer == idp.issuer
    assert set(caller.roles) == {"maintainer", "submitter"}
    assert caller.scopes == ("openid", "profile")
    assert caller.email == "a@lab.test"
    # The quota/audit key is issuer-qualified and comes from the token, not the request body.
    assert caller.identity == f"{idp.issuer}#lab-7"


def test_no_idp_configured_refuses_writes_rather_than_falling_open() -> None:
    """ "No IdP" must never mean "everyone is trusted" — the deployment fails closed with 503."""
    open_service = LeaderboardService(authn=None, scorer=SandboxScorer(InProcessSandbox()))
    response = _client(open_service).post("/bench/submissions", json=ANCHOR_PAYLOAD)
    assert response.status_code == 503
    assert response.json()["code"] == "capability_unavailable"
    # ...and the read paths still work with no account at all (AC5).
    assert _client(open_service).get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}").status_code == 200


def test_verifier_requires_issuer_audience_and_a_key_source() -> None:
    with pytest.raises(ValueError, match="issuer and an audience"):
        OidcTokenVerifier(issuer="", audience="a", jwks={"keys": []})
    with pytest.raises(ValueError, match="jwks mapping or a jwks_url"):
        OidcTokenVerifier(issuer="https://i", audience="a")


def test_jwks_is_fetched_over_http_and_cached(idp: TestIdp) -> None:
    """A deployment points the verifier at the issuer's JWKS URL; it fetches public keys only."""
    fetches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetches.append(str(request.url))
        return httpx.Response(200, json=idp.jwks)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    remote = OidcTokenVerifier(
        issuer=idp.issuer, audience=idp.audience, jwks_url="https://idp.test/certs", http=http
    )
    assert remote.verify(idp.token()).subject == "lab-1"
    assert remote.verify(idp.token()).subject == "lab-1"
    assert len(fetches) == 1  # cached: the second verification does not refetch


def test_unreachable_jwks_fails_closed(idp: TestIdp) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    remote = OidcTokenVerifier(
        issuer=idp.issuer, audience=idp.audience, jwks_url="https://idp.test/certs", http=http
    )
    with pytest.raises(AuthenticationError, match="could not fetch"):
        remote.verify(idp.token())


def test_unknown_kid_refetches_once_then_fails_closed(idp: TestIdp) -> None:
    """A rotated IdP key is picked up by one refetch; an unknown kid never becomes a fetch loop."""
    fetches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetches.append(str(request.url))
        return httpx.Response(200, json={"keys": []})  # a key set that matches nothing

    http = httpx.Client(transport=httpx.MockTransport(handler))
    remote = OidcTokenVerifier(
        issuer=idp.issuer, audience=idp.audience, jwks_url="https://idp.test/certs", http=http
    )
    with pytest.raises(AuthenticationError, match="no JWKS key matches"):
        remote.verify(idp.token())
    assert len(fetches) == 2  # the initial fetch, then exactly one refresh — and no more


def test_oidc_verifier_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ISSUER_ENV, raising=False)
    monkeypatch.delenv(AUDIENCE_ENV, raising=False)
    assert oidc_verifier_from_env() is None  # unconfigured ⇒ the app refuses writes (503)

    monkeypatch.setenv(ISSUER_ENV, "https://idp.test/realms/x")
    monkeypatch.setenv(AUDIENCE_ENV, "astro-mine-bench")
    monkeypatch.setenv(JWKS_URL_ENV, "https://idp.test/certs")
    built = oidc_verifier_from_env()
    assert built is not None and built.issuer == "https://idp.test/realms/x"


# =================================================================================================
# AC2 — the OPA-style policy layer: RBAC, quotas, embargo, authoring rights
# =================================================================================================


def _request(action: Action, *, roles: tuple[str, ...] = ("submitter",), **context: Any) -> Any:
    return AuthorizationRequest(
        principal=principal("lab-1", *roles),
        action=action,
        resource=context.pop("resource", ANCHOR_SCENARIO_ID),
        context=context,
    )


def test_rbac_grants_by_role() -> None:
    engine = RbacPolicyEngine()
    assert engine.evaluate(_request(Action.SUBMISSION_CREATE)).allow
    assert engine.evaluate(_request(Action.SUBMISSION_CREATE_HUB)).allow
    # ...but a submitter may not mutate the board, author scenarios, or read the audit trail.
    for action in (Action.RANKING_MUTATE, Action.SCENARIO_AUTHOR, Action.AUDIT_READ):
        decision = engine.evaluate(_request(action))
        assert not decision.allow
        assert "do not grant" in decision.reason


def test_rbac_admin_grants_everything() -> None:
    engine = RbacPolicyEngine()
    for action in Action:
        assert engine.evaluate(_request(action, roles=("admin",))).allow


def test_maintainer_holds_the_authoring_rights() -> None:
    """bench.md §9: OPA governs 'metric/scenario authoring rights'."""
    engine = RbacPolicyEngine()
    for action in (Action.SCENARIO_AUTHOR, Action.METRIC_AUTHOR, Action.EMBARGO_READ):
        assert engine.evaluate(_request(action, roles=("maintainer",))).allow
        assert not engine.evaluate(_request(action, roles=("submitter",))).allow


def test_no_known_role_denies_everything() -> None:
    engine = RbacPolicyEngine()
    decision = engine.evaluate(_request(Action.SUBMISSION_CREATE, roles=("wizard",)))
    assert not decision.allow
    assert "no known role" in decision.reason


def test_submission_quota_is_enforced_per_role() -> None:
    """bench.md §9: 'OPA for submission quotas' — the per-user cap on submissions per window."""
    engine = RbacPolicyEngine()
    under = _request(Action.SUBMISSION_CREATE, submissions_in_window=20)
    over = _request(Action.SUBMISSION_CREATE, submissions_in_window=21)
    assert engine.evaluate(under).allow
    assert not engine.evaluate(over).allow
    assert "quota exhausted" in engine.evaluate(over).reason
    # A maintainer's quota is larger; an admin is uncapped.
    assert engine.evaluate(
        _request(Action.SUBMISSION_CREATE, roles=("maintainer",), submissions_in_window=21)
    ).allow
    assert engine.evaluate(
        _request(Action.SUBMISSION_CREATE, roles=("admin",), submissions_in_window=100_000)
    ).allow


def test_quota_does_not_apply_to_non_submission_actions() -> None:
    engine = RbacPolicyEngine()
    assert engine.evaluate(
        _request(Action.AUDIT_READ, roles=("admin",), submissions_in_window=999_999)
    ).allow


def test_embargoed_scenario_needs_the_embargo_right() -> None:
    """bench.md §9: 'OPA for ... embargo control'."""
    engine = RbacPolicyEngine(embargoed_scenarios=frozenset({"secret-scenario-v1"}))
    denied = engine.evaluate(_request(Action.SUBMISSION_CREATE, scenario_id="secret-scenario-v1"))
    assert not denied.allow
    assert "under embargo" in denied.reason
    # A maintainer holds embargo:read, so the same submission is allowed.
    assert engine.evaluate(
        _request(Action.SUBMISSION_CREATE, roles=("maintainer",), scenario_id="secret-scenario-v1")
    ).allow


def test_opa_sidecar_decides_the_same_input_document() -> None:
    """The sidecar and the in-process engine exchange the *same* input/result documents."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content)["input"])
        return httpx.Response(200, json={"result": {"allow": True, "reason": "allowed by OPA"}})

    engine = OpaPolicyEngine(
        "http://opa:8181/v1/data/astromine/bench/decision",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    decision = engine.evaluate(_request(Action.SUBMISSION_CREATE_HUB, submissions_in_window=2))
    assert decision.allow and decision.reason == "allowed by OPA"
    assert seen[0]["action"] == "submission:create_hub"
    assert seen[0]["principal"]["roles"] == ["submitter"]
    assert seen[0]["context"]["submissions_in_window"] == 2


def test_opa_denial_carries_its_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"allow": False, "reason": "quota exhausted"}})

    engine = OpaPolicyEngine(
        "http://opa:8181/x", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    decision = engine.evaluate(_request(Action.SUBMISSION_CREATE))
    assert not decision.allow and decision.reason == "quota exhausted"


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (500, {"result": {"allow": True}}, "failed"),  # a 5xx never means yes
        (200, {}, "undefined policy rule"),  # OPA returns {} for an undefined rule
        (200, {"result": {}}, "denied by OPA"),  # a result with no explicit allow
        (200, {"result": {"allow": "yes"}}, "denied by OPA"),  # allow must be exactly True
    ],
)
def test_opa_fails_closed(status: int, body: dict[str, Any], expected: str) -> None:
    """An authorization service that cannot answer must never mean 'yes' (conventions.md §9)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    engine = OpaPolicyEngine(
        "http://opa:8181/x", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    decision = engine.evaluate(_request(Action.SUBMISSION_CREATE))
    assert not decision.allow
    assert expected in decision.reason


def test_opa_unreachable_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    engine = OpaPolicyEngine(
        "http://opa:8181/x", http=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert not engine.evaluate(_request(Action.SUBMISSION_CREATE)).allow


def test_opa_engine_requires_a_url() -> None:
    with pytest.raises(ValueError, match="URL of an OPA"):
        OpaPolicyEngine("")


def test_policy_engine_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OPA_URL_ENV, raising=False)
    monkeypatch.setenv(EMBARGOED_SCENARIOS_ENV, "a-v1, b-v2")
    engine = policy_engine_from_env()
    assert isinstance(engine, RbacPolicyEngine)
    assert not engine.evaluate(_request(Action.SUBMISSION_CREATE, scenario_id="a-v1")).allow

    monkeypatch.setenv(OPA_URL_ENV, "http://opa:8181/v1/data/astromine/bench/decision")
    assert isinstance(policy_engine_from_env(), OpaPolicyEngine)


def test_quota_is_keyed_on_the_authenticated_subject_not_the_request_body(
    verifier: OidcTokenVerifier, idp: TestIdp
) -> None:
    """The pre-bench#29 rate limiter keyed on a client-supplied `identity` — trivially resettable.

    Now the counter is bound to the token's subject, so a submitter cannot rotate their way out of
    their own quota by editing a JSON field.
    """
    limited = LeaderboardService(
        authn=verifier,
        rate_limiter=InMemoryRateLimiter(limit=1),
        scorer=SandboxScorer(InProcessSandbox()),
    )
    client = _client(limited)
    first = client.post(
        "/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header(subject="lab-9")
    )
    assert first.status_code == 200
    # Same authenticated subject ⇒ still rate-limited, whatever the body says.
    again = client.post(
        "/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header(subject="lab-9")
    )
    assert again.status_code == 429
    # A genuinely different authenticated subject gets its own window.
    other = client.post(
        "/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header(subject="lab-10")
    )
    assert other.status_code == 200


def test_ranking_mutation_is_admin_only(service: LeaderboardService, idp: TestIdp) -> None:
    client = _client(service)
    submission_id = client.post(
        "/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header()
    ).json()["submission_id"]

    denied = client.delete(
        f"/bench/submissions/{submission_id}", headers=idp.header(roles=("submitter",))
    )
    assert denied.status_code == 403

    allowed = client.delete(
        f"/bench/submissions/{submission_id}", headers=idp.header(roles=("admin",))
    )
    assert allowed.status_code == 200
    assert client.get(f"/bench/submissions/{submission_id}").status_code == 404
    assert client.get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}").json() == []


def test_retracting_an_unknown_submission_is_404(service: LeaderboardService, idp: TestIdp) -> None:
    response = _client(service).delete(
        "/bench/submissions/sha256:deadbeef", headers=idp.header(roles=("admin",))
    )
    assert response.status_code == 404


# =================================================================================================
# AC3 — supply-chain verification: cosign + SLSA + SBOM, fail-closed, before execution
# =================================================================================================


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "hub-registry")


def _publish(
    registry: Registry,
    *,
    name: str = "acme/prospector",
    version: str = "1.0.0",
    interfaces: dict[str, str],
    entrypoint: str = BASELINE_REF,
) -> str:
    manifest = PluginManifest(
        name=name,
        version=version,
        kind=PluginKind.POLICY,
        core_interfaces=dict(interfaces),
        inputs=["Observation"],
        outputs=["ActionBatch"],
        attributes={"entrypoint": entrypoint},
    )
    document = ManifestDocument(manifest_version="0.1", manifest=manifest)
    published = registry.publish(
        name=name,
        version=version,
        kind="policy",
        config=document.model_dump(mode="json"),
        layers=[Blob("application/vnd.astro-mine.policy.onnx.v1", b"onnx-model-bytes")],
    )
    return str(published.digest)


def test_unsigned_submission_is_rejected(registry: Registry, anchor: ScenarioSpec) -> None:
    """A content hash is not a signature: an artifact with no attestations must not run.

    This is the gap bench#29 names — intake verified content hashes but no cryptographic signature
    or provenance, so an attacker who could write to the registry could publish a *coherent*
    artifact whose every blob hashed correctly.
    """
    digest = _publish(registry, interfaces=anchor.core_interface)
    with pytest.raises(SupplyChainRejected, match="failed supply-chain verification"):
        verify_submission_attestations(registry, digest, AttestationPolicy())


def test_signed_and_attested_submission_verifies(registry: Registry, anchor: ScenarioSpec) -> None:
    """The happy path: cosign signature + SLSA provenance + SBOM, verified via Seal (RFC-0005)."""
    digest = _publish(registry, interfaces=anchor.core_interface)
    private_pem, public_pem = generate_keypair()
    attest(registry, digest, private_key_pem=private_pem, name="acme/prospector", version="1.0.0")

    verdict = verify_submission_attestations(
        registry, digest, AttestationPolicy(trusted_public_key_pem=public_pem)
    )
    assert verdict.verified
    assert verdict.signer_pinned
    assert set(verdict.required) == {"signature", "slsa", "sbom"}


def test_submission_signed_by_an_untrusted_key_is_rejected(
    registry: Registry, anchor: ScenarioSpec
) -> None:
    """A *valid* signature by the wrong signer is a rejection when the trust root is pinned."""
    digest = _publish(registry, interfaces=anchor.core_interface)
    attacker_private, _ = generate_keypair()
    _, our_public = generate_keypair()
    attest(
        registry, digest, private_key_pem=attacker_private, name="acme/prospector", version="1.0.0"
    )

    with pytest.raises(SupplyChainRejected):
        verify_submission_attestations(
            registry, digest, AttestationPolicy(trusted_public_key_pem=our_public)
        )


def test_verification_cannot_be_switched_off() -> None:
    """A deployment may pin *which* signer it trusts; it may not require *nothing*."""
    with pytest.raises(ValueError, match="must not be empty"):
        AttestationPolicy(required=())


def test_attestation_policy_from_env_loads_the_public_trust_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, public_pem = generate_keypair()
    key_file = tmp_path / "cosign.pub"
    key_file.write_bytes(public_pem)

    monkeypatch.delenv(TRUSTED_KEY_ENV, raising=False)
    assert attestation_policy_from_env().trusted_public_key_pem is None

    monkeypatch.setenv(TRUSTED_KEY_ENV, str(key_file))
    assert attestation_policy_from_env().trusted_public_key_pem == public_pem


def test_an_unresolvable_trust_root_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TRUSTED_KEY_ENV, "/no/such/cosign.pub")
    with pytest.raises(SupplyChainRejected, match="unresolvable trust root"):
        attestation_policy_from_env()


def test_hub_intake_rejects_an_unattested_submission_before_it_executes(
    verifier: OidcTokenVerifier, idp: TestIdp, registry: Registry, anchor: ScenarioSpec
) -> None:
    """AC3 end-to-end: verification failure fails closed — rejected, never silently accepted."""
    sandbox = InProcessSandbox()
    hosted = LeaderboardService(authn=verifier, registry=registry, scorer=SandboxScorer(sandbox))
    digest = _publish(registry, interfaces=anchor.core_interface)  # published, but never attested

    response = _client(hosted).post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest},
        headers=idp.header(),
    )
    assert response.status_code == 422
    # The supply-chain verdict gets Hub's code, not the generic one: "the artifact did not verify"
    # is the arm a submitter acts on differently from "your policy crashed" (api#4).
    assert response.json()["code"] == "admission_rejected"
    # ...and, crucially, the policy never ran: nothing reached the sandbox.
    assert sandbox.invocations == []
    assert hosted.store.list_submissions(ANCHOR_SCENARIO_ID) == []


def test_hub_intake_scores_an_attested_submission(
    verifier: OidcTokenVerifier, idp: TestIdp, registry: Registry, anchor: ScenarioSpec
) -> None:
    hosted = LeaderboardService(
        authn=verifier, registry=registry, scorer=SandboxScorer(InProcessSandbox())
    )
    digest = _publish(registry, interfaces=anchor.core_interface)
    private_pem, _ = generate_keypair()
    attest(registry, digest, private_key_pem=private_pem, name="acme/prospector", version="1.0.0")

    job = (
        _client(hosted)
        .post(
            "/bench/submissions/hub",
            json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest},
            headers=idp.header(),
        )
        .json()
    )
    assert job["status"] == "ranked"


# =================================================================================================
# AC4 — the audit trail
# =================================================================================================


def test_audit_records_authentication_failures(
    service: LeaderboardService, audit: InMemoryAuditLog
) -> None:
    _client(service).post("/bench/submissions", json=ANCHOR_PAYLOAD)  # no token
    denials = audit.query(action="authenticate", decision=AuditDecision.DENY)
    assert len(denials) == 1
    assert "bearer token" in denials[0].reason


def test_audit_records_authorization_decisions(
    service: LeaderboardService, audit: InMemoryAuditLog, idp: TestIdp
) -> None:
    client = _client(service)
    client.post("/bench/submissions", json=ANCHOR_PAYLOAD, headers=idp.header())
    client.delete("/bench/submissions/sha256:x", headers=idp.header(roles=("submitter",)))

    allowed = audit.query(action=str(Action.SUBMISSION_CREATE), decision=AuditDecision.ALLOW)
    assert allowed and allowed[0].subject == "lab-1"
    assert allowed[0].issuer == idp.issuer


def test_audit_records_verification_outcomes(
    verifier: OidcTokenVerifier, idp: TestIdp, registry: Registry, anchor: ScenarioSpec
) -> None:
    audit = InMemoryAuditLog()
    hosted = LeaderboardService(
        authn=verifier, registry=registry, audit=audit, scorer=SandboxScorer(InProcessSandbox())
    )
    digest = _publish(registry, interfaces=anchor.core_interface)
    private_pem, _ = generate_keypair()
    attest(registry, digest, private_key_pem=private_pem, name="acme/prospector", version="1.0.0")

    _client(hosted).post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest},
        headers=idp.header(),
    )
    verified = audit.query(action="submission:verify", decision=AuditDecision.VERIFIED)
    assert len(verified) == 1
    assert verified[0].detail["required"] == ["signature", "slsa", "sbom"]
    assert verified[0].detail["verified"] is True


def test_audit_trail_is_queryable_and_append_only(audit: InMemoryAuditLog) -> None:
    from astro_mine.bench.leaderboard import audit_event

    for index in range(5):
        audit.record(
            audit_event(
                action="submission:create",
                decision=AuditDecision.ALLOW if index % 2 else AuditDecision.DENY,
                subject=f"lab-{index % 2}",
                resource=ANCHOR_SCENARIO_ID,
                reason=f"event {index}",
            )
        )
    assert len(audit) == 5
    assert len(audit.query(subject="lab-1")) == 2
    assert len(audit.query(decision=AuditDecision.DENY)) == 3
    assert len(audit.query(resource=ANCHOR_SCENARIO_ID)) == 5
    assert len(audit.query(action="nope")) == 0
    assert len(audit.query(limit=2)) == 2
    assert audit.query()[0].reason == "event 4"  # newest first

    # Frozen: an event cannot be quietly rewritten after the fact.
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError on a frozen model
        audit.events[0].reason = "rewritten"  # type: ignore[misc]


def test_audit_endpoint_is_admin_only(service: LeaderboardService, idp: TestIdp) -> None:
    client = _client(service)
    assert client.get("/bench/audit").status_code == 401  # unauthenticated
    assert client.get("/bench/audit", headers=idp.header(roles=("submitter",))).status_code == 403
    response = client.get("/bench/audit", headers=idp.header(roles=("admin",)))
    assert response.status_code == 200
    assert any(event["action"] == str(Action.AUDIT_READ) for event in response.json())


def test_sql_audit_log_is_durable_and_queryable(tmp_path: Path) -> None:
    from astro_mine.bench.leaderboard import audit_event
    from astro_mine.bench.leaderboard._sql import SqlAuditLog

    log = SqlAuditLog(f"sqlite:///{tmp_path / 'audit.db'}")
    for index in range(3):
        log.record(
            audit_event(
                action="submission:verify",
                decision=AuditDecision.VERIFIED,
                subject="lab-1",
                resource=f"ref-{index}",
                submission_id=f"sha256:{index}",
                reason=f"verified {index}",
                detail={"required": ["signature", "slsa", "sbom"]},
            )
        )
    log.record(audit_event(action="authenticate", decision=AuditDecision.DENY, reason="bad token"))

    assert len(log.query()) == 4
    assert log.query()[0].action == "authenticate"  # newest first
    assert len(log.query(subject="lab-1")) == 3
    assert len(log.query(decision=AuditDecision.DENY)) == 1
    assert len(log.query(submission_id="sha256:1")) == 1
    assert log.query(resource="ref-2")[0].detail["required"] == ["signature", "slsa", "sbom"]
    assert len(log.query(action="submission:verify", limit=2)) == 2


def test_sql_audit_log_requires_url_or_engine() -> None:
    from astro_mine.bench.leaderboard._sql import SqlAuditLog

    with pytest.raises(ValueError, match="url or an engine"):
        SqlAuditLog()


# =================================================================================================
# The service-level guards
# =================================================================================================


def test_authorize_raises_403_with_the_policy_reason(service: LeaderboardService) -> None:
    with pytest.raises(SubmissionRejected) as caught:
        service.authorize(principal("lab-1", "submitter"), Action.RANKING_MUTATE, "sha256:x")
    assert caught.value.status == 403


def test_ticket_is_keyed_on_the_authenticated_subject() -> None:
    from astro_mine.bench.leaderboard import HubSubmissionRequest

    service = LeaderboardService(scorer=SandboxScorer(InProcessSandbox()))
    request = HubSubmissionRequest(scenario_id=ANCHOR_SCENARIO_ID, hub_ref="a/b:1.0.0")
    mine = service.ticket(request, principal("lab-1"))
    theirs = service.ticket(request, principal("lab-2"))
    assert mine != theirs  # nobody can collide with, or guess, another lab's job ticket
    assert mine == service.ticket(request, principal("lab-1"))  # deterministic


def test_role_and_action_vocabularies_are_stable() -> None:
    """The Rego in policy/bench.rego hard-codes these strings; a rename must break loudly here."""
    assert {str(role) for role in Role} == {"submitter", "maintainer", "admin"}
    assert {str(action) for action in Action} == {
        "submission:create",
        "submission:create_hub",
        "ranking:mutate",
        "scenario:author",
        "metric:author",
        "embargo:read",
        "audit:read",
    }


def test_the_spoofable_identity_field_is_gone(service: LeaderboardService, idp: TestIdp) -> None:
    """bench#29: the wire model must not carry a client-supplied identity at all.

    The pre-bench#29 `HubSubmissionRequest` had an `identity` field that keyed the rate limiter — so
    a submitter could reset their own quota by editing a JSON field. Removing the *use* of it is not
    enough: leaving the field in place, inert but named `identity`, is exactly the shape of a future
    bug. `extra="forbid"` now rejects it outright.
    """
    from astro_mine.bench.leaderboard import HubSubmissionRequest

    assert "identity" not in HubSubmissionRequest.model_fields

    response = _client(service).post(
        "/bench/submissions/hub",
        json={
            "scenario_id": ANCHOR_SCENARIO_ID,
            "hub_ref": "acme/p:1.0.0",
            "identity": "someone-elses-quota",
        },
        headers=idp.header(),
    )
    assert response.status_code == 422  # the request does not even parse
