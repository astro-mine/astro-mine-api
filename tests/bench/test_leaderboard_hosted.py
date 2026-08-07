"""The hosted public leaderboard: Hub-digest intake, provenance, integrity (RM-P1-BENCH-10).

Covers the four acceptance criteria end-to-end against a local Hub registry and process-local
backends (no external services):

1. a submission referenced **only by Hub digest** is resolved, manifest-validated, run under
   submit-policy-we-run, scored, and ranked;
2. held-out seeds are disclosed only at eval time and a sampled fraction is **re-executed from the
   provenance bundle** — a match verifies, a mismatch flags;
3. every entry carries full lineage and the bundle is **byte-for-byte reproducible**;
4. Bench imports only Core + the Hub client (no Sim) — asserted in ``test_contracts``.

Since bench#29/#30 every submission here is **authenticated** (an OIDC bearer token), its artifact
is **attested** (cosign + SLSA + SBOM, verified via Seal through Hub) before it runs, and executes
**in a sandbox** rather than in the evaluator. The security layer itself is covered in
``tests/test_leaderboard_security.py`` and ``tests/test_sandbox.py``; here it is present because the
pipeline does not work without it — which is the point.

One test (``test_hub_digest_intake_end_to_end_through_the_real_sandbox``) drives the **real**
SubprocessSandbox end to end, so the whole hosted path is proven against genuine out-of-process,
no-egress execution; the rest use the fast in-process double so the suite does not pay a fresh
interpreter per held-out seed.

Plus unit coverage of the object store, job lifecycle, and rate limiter backends.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from astro_mine.bench.baseline import BaselinePolicy, run
from astro_mine.bench.leaderboard import (
    FileObjectStore,
    HubResolutionError,
    InMemoryObjectStore,
    InMemoryRateLimiter,
    JobRecord,
    LeaderboardService,
    ManifestInterfaceError,
    ObjectIntegrityError,
    OidcTokenVerifier,
    RateLimitError,
    SubmissionStatus,
    build_provenance_bundle,
    resolve_submission,
    submission_policy_ref,
    validate_submission_manifest,
)
from astro_mine.bench.leaderboard._objects import blob_digest
from astro_mine.bench.sandbox import SandboxScorer, SubprocessSandbox
from astro_mine.bench.scenario import ScenarioSpec
from astro_mine.bench.zoo import ANCHOR_SCENARIO_ID, load_scenario
from astro_mine.core.registry import PluginKind
from astro_mine.core.registry.model import PluginManifest
from astro_mine.hub.client import HubClient
from astro_mine.hub.registry import Blob, Registry
from astro_mine.hub.supply_chain import generate_keypair
from fastapi.testclient import TestClient
from pydantic import ValidationError

from astro_mine_api.bench import create_app
from tests.bench._factories import (
    REPO_ROOT,
    InProcessSandbox,
    TestIdp,
    make_idp,
    sandbox_enforceable,
)

BASELINE_ENTRYPOINT = "tests.bench._factories:BASELINE_INSTANCE"
NONDETERMINISTIC_ENTRYPOINT = "tests.bench._factories:NondeterministicPolicy"


@pytest.fixture(scope="module")
def anchor() -> ScenarioSpec:
    return load_scenario(ANCHOR_SCENARIO_ID)


@pytest.fixture(scope="module")
def idp() -> TestIdp:
    return make_idp()


@pytest.fixture(scope="module")
def verifier(idp: TestIdp) -> OidcTokenVerifier:
    return OidcTokenVerifier(issuer=idp.issuer, audience=idp.audience, jwks=idp.jwks)


def _publish_policy(
    registry: Registry,
    *,
    name: str,
    version: str = "1.0.0",
    entrypoint: str = BASELINE_ENTRYPOINT,
    interfaces: dict[str, str],
    kind: PluginKind = PluginKind.POLICY,
    onnx_layer: bytes = b"onnx-model-bytes",
    attested: bool = True,
) -> str:
    """Publish a policy artifact to ``registry`` and return its image-manifest digest.

    The config blob is a real Core plugin manifest whose ``entrypoint`` attribute the reference
    policy loader resolves; the ONNX bytes are a payload layer (verified fail-closed on intake).

    **Published through `HubClient`, not `registry.publish`.** This fixture used to build the config
    blob itself, as a `ManifestDocument` envelope — and every publisher in the platform writes a
    *bare* `PluginManifest` (hub.md §2 principle 2). Both sides were internally consistent, so this
    suite stayed green while the whole Hub-intake path could not accept a single real artifact; it
    surfaced only when a deployment was seeded for real (#14, astro-mine-platform#14). A fixture
    that constructs the bytes it is about to parse asserts that a function is its own inverse and
    nothing else. Going through the client means this suite cannot disagree with the platform about
    the shape again — the client owns it.
    """
    manifest = PluginManifest(
        name=name,
        version=version,
        kind=kind,
        core_interfaces=dict(interfaces),
        inputs=["Observation"],
        outputs=["ActionBatch"],
        attributes={"entrypoint": entrypoint},
    )
    artifact_kind = PluginKind.POLICY.value if kind is PluginKind.POLICY else "world"
    layers = [Blob("application/vnd.astro-mine.policy.onnx.v1", onnx_layer)]
    if attested:
        # bench#29: a Hub submission must carry a cosign signature + SLSA provenance + an SBOM
        # before it is allowed to execute. `HubClient.publish` signs, attests and verifies at
        # admission in one step, which is what a real producer does.
        private_pem, _ = generate_keypair()
        published = HubClient(registry).publish(
            name=name,
            version=version,
            kind=artifact_kind,
            manifest=manifest,
            layers=layers,
            private_key_pem=private_pem,
        )
        return str(published.digest)
    # The unsigned case reaches past the client deliberately: `private_key_pem` is required there,
    # because hub.md §9 defines no namespace tier for unsigned content. Publishing without it is
    # precisely what a submission that fails verification looks like, so it has to be built by hand
    # — but from the *same* bare manifest the client would have written.
    published = registry.publish(
        name=name,
        version=version,
        kind=artifact_kind,
        config=manifest.model_dump(mode="json"),
        layers=layers,
    )
    return published.digest


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    return Registry(tmp_path / "hub-registry")


def test_the_hand_built_config_blob_matches_what_the_publisher_writes(registry: Registry) -> None:
    """The unsigned path builds its config by hand; this pins it to the client's shape.

    `_publish_policy(attested=True)` goes through `HubClient` and so cannot get the shape wrong. The
    unsigned branch cannot use the client — a key is required there by design — and neither can
    `test_leaderboard_security`'s `_publish`, which needs publish and attest to be separable. Those
    two are where the envelope bug lived, and where it could come back.

    So they are checked against the real publisher's output rather than against a description of it.
    The platform asserts the same contract from its own side, in
    `tests/platform/test_config_blob_contract`; this asserts that *this suite's fixtures* still
    agree with it, which is the half that was wrong.
    """
    signed = _publish_policy(registry, name="acme/signed", interfaces={"observation": "0.1.0"})
    unsigned = _publish_policy(
        registry, name="acme/unsigned", interfaces={"observation": "0.1.0"}, attested=False
    )

    from_client = json.loads(registry.read_config(signed))
    by_hand = json.loads(registry.read_config(unsigned))

    # hub.md §2 principle 2: the stored config blob *is* the manifest. No envelope to reach through.
    assert "manifest_version" not in by_hand
    assert "manifest" not in by_hand
    assert by_hand.keys() == from_client.keys()


@pytest.fixture
def service(registry: Registry, tmp_path: Path, verifier: OidcTokenVerifier) -> LeaderboardService:
    return LeaderboardService(
        registry=registry,
        object_store=FileObjectStore(tmp_path / "objects"),
        authn=verifier,
        scorer=SandboxScorer(InProcessSandbox()),
    )


def _client(service: LeaderboardService) -> TestClient:
    return TestClient(create_app(service=service))


# --- AC1: resolve-by-digest → validate → submit-we-run → score → rank ----------------------------


def test_hub_digest_intake_scores_and_ranks(
    service: LeaderboardService, registry: Registry, anchor: ScenarioSpec, idp: TestIdp
) -> None:
    digest = _publish_policy(registry, name="acme/prospector", interfaces=anchor.core_interface)
    client = _client(service)

    response = client.post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest, "method": "acme-v1"},
        headers=idp.header(),
    )
    assert response.status_code == 200
    job = response.json()
    assert job["status"] == SubmissionStatus.RANKED
    result_id = job["result_id"]

    submission = client.get(f"/bench/submissions/{result_id}").json()
    assert submission["integrity"] == "verified"

    # The job ticket the submission returned is readable back by id — the async-lifecycle route
    # a client polls while a Hub submission is still running.
    ticket = client.get(f"/bench/jobs/{job['job_id']}")
    assert ticket.status_code == 200
    assert ticket.json()["result_id"] == result_id
    assert submission["source"] == digest  # provenance is the Hub digest, not an upload
    assert submission["provenance_hash"] is not None
    assert len(submission["scores"]) == 7

    board = client.get(f"/bench/leaderboard/{ANCHOR_SCENARIO_ID}").json()
    assert [e["submission_id"] for e in board] == [result_id]
    assert board[0]["source"] == digest


def test_hub_intake_by_name_version_tag(
    service: LeaderboardService, registry: Registry, anchor: ScenarioSpec, idp: TestIdp
) -> None:
    _publish_policy(registry, name="lab/policy", version="2.1.0", interfaces=anchor.core_interface)
    job = (
        _client(service)
        .post(
            "/bench/submissions/hub",
            json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": "lab/policy:2.1.0"},
            headers=idp.header(),
        )
        .json()
    )
    assert job["status"] == SubmissionStatus.RANKED  # a tag resolves to its one immutable digest


# --- AC1: manifest validation against the scenario interface -------------------------------------


def test_manifest_interface_mismatch_is_rejected(
    service: LeaderboardService, registry: Registry, anchor: ScenarioSpec, idp: TestIdp
) -> None:
    # A policy built against a future major of the env interface cannot satisfy the scenario.
    bad = dict(anchor.core_interface)
    bad[next(iter(bad))] = "9.0.0"
    digest = _publish_policy(registry, name="acme/future", interfaces=bad)
    response = _client(service).post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest},
        headers=idp.header(),
    )
    assert response.status_code == 422
    # An interface mismatch is a rejected submission, not a supply-chain verdict (api#4).
    assert response.json()["code"] == "submission_rejected"


def test_non_policy_artifact_is_rejected(registry: Registry, anchor: ScenarioSpec) -> None:
    digest = _publish_policy(
        registry,
        name="acme/world",
        interfaces=anchor.core_interface,
        kind=PluginKind.WORLD_PROVIDER,
    )
    resolved = resolve_submission(registry, digest)
    with pytest.raises(ManifestInterfaceError, match="must be a policy"):
        validate_submission_manifest(resolved, anchor)


def test_unresolvable_digest_is_rejected(service: LeaderboardService, idp: TestIdp) -> None:
    response = _client(service).post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": "sha256:" + "0" * 64},
        headers=idp.header(),
    )
    assert response.status_code == 404


# --- AC2: fail-closed verification + provenance re-execution --------------------------------------


def test_tampered_blob_fails_closed(
    service: LeaderboardService,
    registry: Registry,
    tmp_path: Path,
    anchor: ScenarioSpec,
    idp: TestIdp,
) -> None:
    digest = _publish_policy(registry, name="acme/tampered", interfaces=anchor.core_interface)
    # Corrupt a stored blob so its content address no longer matches — resolution must fail closed.
    image = registry.read_manifest(digest)
    layer_hex = image["layers"][0]["digest"].split(":", 1)[1]
    blob_path = tmp_path / "hub-registry" / "blobs" / "sha256" / layer_hex
    blob_path.write_bytes(b"tampered-onnx-bytes")

    with pytest.raises(HubResolutionError, match="integrity verification failed"):
        resolve_submission(registry, digest)
    response = _client(service).post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest},
        headers=idp.header(),
    )
    assert response.status_code == 404


def test_nondeterministic_submission_is_flagged(
    service: LeaderboardService, registry: Registry, anchor: ScenarioSpec, idp: TestIdp
) -> None:
    digest = _publish_policy(
        registry,
        name="acme/flaky",
        interfaces=anchor.core_interface,
        entrypoint=NONDETERMINISTIC_ENTRYPOINT,
    )
    job = (
        _client(service)
        .post(
            "/bench/submissions/hub",
            json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest},
            headers=idp.header(),
        )
        .json()
    )
    assert job["status"] == SubmissionStatus.FLAGGED
    assert "mismatch" in job["detail"]
    submission = service.store.get_submission(job["result_id"])
    assert submission is not None and submission.integrity == "flagged"


# --- AC3: full lineage + byte-for-byte reproducibility -------------------------------------------


def test_provenance_bundle_carries_full_lineage(
    service: LeaderboardService, registry: Registry, anchor: ScenarioSpec, idp: TestIdp
) -> None:
    digest = _publish_policy(registry, name="acme/lineage", interfaces=anchor.core_interface)
    client = _client(service)
    result_id = client.post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest},
        headers=idp.header(),
    ).json()["result_id"]

    bundle = client.get(f"/bench/submissions/{result_id}/provenance").json()
    assert bundle["scenario_spec_hash"] == anchor.spec_hash
    assert bundle["core_interface_version"] == anchor.core_interface
    assert bundle["source"] == digest
    assert set(bundle["content_hashes"]) == {ref.id for ref in anchor.content_refs()}
    assert bundle["environment_lockfile"].startswith("sha256:")
    assert len(bundle["per_seed"]) == 12  # scored on every held-out seed


def test_provenance_bundle_hash_is_reproducible(anchor: ScenarioSpec) -> None:
    policy = BaselinePolicy()
    seeds = (900001, 900002, 900003)
    first = build_provenance_bundle(
        anchor, run(anchor, policy, seeds=seeds), source="ref:x", code_version="0.0.0"
    )
    second = build_provenance_bundle(
        anchor, run(anchor, policy, seeds=seeds), source="ref:x", code_version="0.0.0"
    )
    assert first.bundle_hash == second.bundle_hash  # env excluded → machine-independent lineage


def test_the_reexecution_audit_also_runs_in_the_sandbox(
    registry: Registry, anchor: ScenarioSpec, verifier: OidcTokenVerifier, idp: TestIdp
) -> None:
    """The integrity audit re-runs the *same untrusted code*, so it must be no less contained.

    A re-execution that ran in-process would hand a submission — one already suspected of tampering
    — everything the evaluator has, at exactly the moment it is being audited (bench.md §9).
    """
    sandbox = InProcessSandbox()
    hosted = LeaderboardService(registry=registry, authn=verifier, scorer=SandboxScorer(sandbox))
    digest = _publish_policy(registry, name="acme/audit", interfaces=anchor.core_interface)
    _client(hosted).post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest},
        headers=idp.header(),
    )
    heldout = set(load_scenario(ANCHOR_SCENARIO_ID).seeds.public) | set(
        inv.seed for inv in sandbox.invocations
    )
    assert heldout  # sanity
    # 12 held-out seeds scored + ceil(0.25 * 12) = 3 re-executed = 15 sandboxed rollouts.
    assert len(sandbox.invocations) == 15


@pytest.mark.skipif(
    not sandbox_enforceable(),
    reason="needs Linux with a seccomp egress filter and a Landlock-capable filesystem "
    "(a 9p/drvfs checkout cannot enforce the confinement; CI on a native filesystem does)",
)
def test_hub_digest_intake_end_to_end_through_the_real_sandbox(
    registry: Registry, anchor: ScenarioSpec, verifier: OidcTokenVerifier, idp: TestIdp
) -> None:
    """The whole hosted pipeline, against the **real** SubprocessSandbox — not a double.

    Authenticate → authorize → resolve by digest → verify cosign/SLSA/SBOM → validate the interface
    → score every held-out seed **out-of-process under a seccomp no-egress filter and a Landlock
    filesystem confinement** → bundle the provenance → re-execute a sample → rank. This is the
    issue's exit criterion: an external lab publishes a policy to Hub and it lands on the public
    leaderboard, reproducibly.
    """
    hosted = LeaderboardService(
        registry=registry,
        authn=verifier,
        scorer=SandboxScorer(SubprocessSandbox(python_path=(REPO_ROOT,))),
    )
    digest = _publish_policy(registry, name="lab/real", interfaces=anchor.core_interface)

    client = _client(hosted)
    job = client.post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": digest, "method": "real-sandbox"},
        headers=idp.header(),
    ).json()
    assert job["status"] == SubmissionStatus.RANKED

    submission = client.get(f"/bench/submissions/{job['result_id']}").json()
    assert submission["integrity"] == "verified"
    assert submission["source"] == digest
    assert len(submission["scores"]) == 7
    # The sandboxed score is byte-identical to the in-process local tier's — sandboxing costs no
    # reproducibility (bench.md §5).
    local = run(anchor, BaselinePolicy(), seeds=load_scenario(ANCHOR_SCENARIO_ID).seeds.public)
    assert (
        local.content_hash != submission["scorecard_hash"]
    )  # different seeds (public vs held-out)
    assert submission["scorecard_hash"].startswith("sha256:")


# --- rate limiting -------------------------------------------------------------------------------


def test_rate_limit_rejects_over_limit(
    registry: Registry, anchor: ScenarioSpec, verifier: OidcTokenVerifier, idp: TestIdp
) -> None:
    limited = LeaderboardService(
        registry=registry,
        rate_limiter=InMemoryRateLimiter(limit=1),
        authn=verifier,
        scorer=SandboxScorer(InProcessSandbox()),
    )
    _publish_policy(registry, name="acme/a", version="1.0.0", interfaces=anchor.core_interface)
    _publish_policy(registry, name="acme/b", version="1.0.0", interfaces=anchor.core_interface)
    client = _client(limited)
    # Keyed on the *authenticated* subject since bench#29 — not a client-supplied identity field.
    first = client.post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": "acme/a:1.0.0"},
        headers=idp.header(subject="lab-1"),
    )
    second = client.post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": "acme/b:1.0.0"},
        headers=idp.header(subject="lab-1"),
    )
    assert first.status_code == 200
    assert second.status_code == 429


def test_hub_intake_unavailable_without_registry(verifier: OidcTokenVerifier, idp: TestIdp) -> None:
    service = LeaderboardService(authn=verifier, scorer=SandboxScorer(InProcessSandbox()))
    response = TestClient(create_app(service=service)).post(
        "/bench/submissions/hub",
        json={"scenario_id": ANCHOR_SCENARIO_ID, "hub_ref": "x:1.0.0"},
        headers=idp.header(),
    )
    assert response.status_code == 503


# --- object store backend ------------------------------------------------------------------------


@pytest.fixture(params=["memory", "file"])
def object_store(request: pytest.FixtureRequest, tmp_path: Path) -> object:
    return InMemoryObjectStore() if request.param == "memory" else FileObjectStore(tmp_path / "obj")


def test_object_store_roundtrip_and_absence(object_store: object) -> None:
    data = b"provenance-bundle-bytes"
    digest = object_store.put(data)  # type: ignore[attr-defined]
    assert digest == blob_digest(data)
    assert object_store.get(digest) == data  # type: ignore[attr-defined]
    assert object_store.contains(digest)  # type: ignore[attr-defined]
    assert object_store.get("sha256:" + "0" * 64) is None  # type: ignore[attr-defined]


def test_object_store_put_is_idempotent(object_store: object) -> None:
    a = object_store.put(b"same")  # type: ignore[attr-defined]
    b = object_store.put(b"same")  # type: ignore[attr-defined]
    assert a == b


def test_object_store_verifies_on_read(tmp_path: Path) -> None:
    store = FileObjectStore(tmp_path / "obj")
    digest = store.put(b"authentic")
    # Corrupt the stored bytes: a swapped object must not be served as authentic.
    hexpart = digest.split(":", 1)[1]
    (tmp_path / "obj" / "sha256" / hexpart[:2] / hexpart).write_bytes(b"corrupted")
    with pytest.raises(ObjectIntegrityError):
        store.get(digest)


# --- job lifecycle + rate limiter units ----------------------------------------------------------


def test_rate_limiter_window_reset() -> None:
    limiter = InMemoryRateLimiter(limit=2)
    limiter.check("id")
    limiter.check("id")
    with pytest.raises(RateLimitError):
        limiter.check("id")
    limiter.reset("id")
    limiter.check("id")  # window advanced


def test_job_record_is_frozen_and_typed() -> None:
    record = JobRecord(job_id="j", status=SubmissionStatus.QUEUED)
    assert record.status is SubmissionStatus.QUEUED
    with pytest.raises(ValidationError):  # frozen
        record.status = SubmissionStatus.RANKED  # type: ignore[misc]


def test_a_manifest_without_an_entrypoint_is_rejected(
    registry: Registry, anchor: ScenarioSpec
) -> None:
    """The evaluator *reads* the entrypoint (a string); it never imports it (bench#30)."""
    manifest = PluginManifest(
        name="acme/no-entry",
        version="1.0.0",
        kind=PluginKind.POLICY,
        core_interfaces=dict(anchor.core_interface),
    )
    # The bare manifest, as every publisher writes it (hub.md §2 principle 2). Built by hand rather
    # than through `HubClient` because this artifact is deliberately malformed for Bench's purposes
    # — it has no `entrypoint` — and the point is what `submission_policy_ref` does with it.
    published = registry.publish(
        name="acme/no-entry",
        version="1.0.0",
        kind="policy",
        config=manifest.model_dump(mode="json"),
    )
    resolved = resolve_submission(registry, published.digest)
    with pytest.raises(HubResolutionError, match="no 'entrypoint'"):
        submission_policy_ref(resolved)


def test_submission_policy_ref_reads_without_importing(
    registry: Registry, anchor: ScenarioSpec
) -> None:
    """bench#30: the service handles the reference *string*, never a live Policy object."""
    digest = _publish_policy(
        registry, name="acme/ref", interfaces=anchor.core_interface, entrypoint=BASELINE_ENTRYPOINT
    )
    assert submission_policy_ref(resolve_submission(registry, digest)) == BASELINE_ENTRYPOINT
