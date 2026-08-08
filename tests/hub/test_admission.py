"""Admission — the publish-side half of verify-twice (hub#32; RM-P1-HUB-03; hub.md §2.3, §9).

``hub.md`` §2 principle 3 promises evidence is checked at **publish** and at **pull**, and that
*"unsigned content is never promoted to a verified namespace"*. Only the pull side shipped: the
library indexed unsigned artifacts, ``POST /publish`` indexed a caller-asserted digest into a
caller-asserted namespace, and ``promote`` gated on nothing but the spelling of the tier.

These prove the three admission paths now reach one fail-closed gate, that a rejected artifact
leaves **nothing** indexed, and — the property the endpoint most lacked — that a caller cannot
forge content provenance by asserting it.

Everything here runs offline against a local OCI-layout registry: no network, no Rekor, no
account (CX-LOCAL).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from astro_mine.hub.index import InMemoryCatalog, ingest
from astro_mine.hub.registry import Registry
from astro_mine.hub.supply_chain import (
    SupplyChainError,
    UnsignedArtifactError,
    admit,
    attest,
    generate_keypair,
    verify_admissible,
)
from fastapi.testclient import TestClient

from astro_mine_api.hub import create_app

from .conftest import make_manifest

_OTHER_DIGEST = "sha256:" + "b" * 64


def _signed(registry: Registry, name: str = "pol", version: str = "1.0.0") -> tuple[Any, str]:
    manifest = make_manifest(name, version)
    published = registry.publish(
        name=name, version=version, kind="policy", config=manifest.model_dump(mode="json")
    )
    private_pem, _ = generate_keypair()
    attest(registry, published.digest, private_key_pem=private_pem, name=name, version=version)
    return manifest, published.digest


def _unsigned(registry: Registry, name: str = "pol", version: str = "1.0.0") -> tuple[Any, str]:
    manifest = make_manifest(name, version)
    published = registry.publish(
        name=name, version=version, kind="policy", config=manifest.model_dump(mode="json")
    )
    return manifest, published.digest


# --- the gate itself ---------------------------------------------------------------------


def test_a_signed_artifact_is_admitted(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "reg")
    manifest, digest = _signed(registry)
    catalog = InMemoryCatalog()
    entry = admit(registry, catalog, manifest, digest=digest, publisher="alice")
    assert entry.digest == digest
    assert catalog.get("pol:1.0.0") is not None


def test_an_unsigned_artifact_is_refused_and_nothing_is_indexed(tmp_path: Path) -> None:
    registry = Registry(tmp_path / "reg")
    manifest, digest = _unsigned(registry)
    catalog = InMemoryCatalog()
    with pytest.raises(UnsignedArtifactError, match="unsigned"):
        admit(registry, catalog, manifest, digest=digest, publisher="alice")
    assert catalog.get("pol:1.0.0") is None


def test_an_unsigned_artifact_cannot_buy_an_exemption_by_narrowing_require(tmp_path: Path) -> None:
    """`require=()` relaxes *which attestations* are demanded — never whether it is signed."""
    registry = Registry(tmp_path / "reg")
    manifest, digest = _unsigned(registry)
    with pytest.raises(UnsignedArtifactError):
        verify_admissible(registry, manifest, digest=digest, require=())


def test_a_manifest_that_disagrees_with_the_stored_config_is_refused(tmp_path: Path) -> None:
    """Otherwise the index describes something other than the bytes a consumer pulls."""
    registry = Registry(tmp_path / "reg")
    _, digest = _signed(registry)
    catalog = InMemoryCatalog()
    impostor = make_manifest("pol", "1.0.0", description="not what was stored")
    with pytest.raises(SupplyChainError, match="does not match its stored config"):
        admit(registry, catalog, impostor, digest=digest, publisher="alice")
    assert catalog.get("pol:1.0.0") is None


def test_a_digest_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    """A digest is a claim until the registry confirms it — and the refusal is a SupplyChainError,
    not the registry's raw KeyError, so every caller can fail closed on one error type."""
    registry = Registry(tmp_path / "reg")
    catalog = InMemoryCatalog()
    with pytest.raises(SupplyChainError, match="not present in this registry"):
        admit(
            registry,
            catalog,
            make_manifest("pol", "1.0.0"),
            digest=_OTHER_DIGEST,
            publisher="alice",
        )
    assert catalog.get("pol:1.0.0") is None


def test_a_tampered_artifact_is_refused_and_leaves_nothing_indexed(tmp_path: Path) -> None:
    """Bytes mutated after signing: the content address no longer holds (AC1)."""
    root = tmp_path / "reg"
    registry = Registry(root)
    manifest, digest = _signed(registry)

    blob = root / "blobs" / "sha256" / digest.split(":", 1)[1]
    payload = json.loads(blob.read_text())
    payload["tampered"] = True
    blob.write_text(json.dumps(payload))

    catalog = InMemoryCatalog()
    with pytest.raises(Exception):  # noqa: B017 - integrity failure surfaces from the registry
        admit(registry, catalog, manifest, digest=digest, publisher="alice")
    assert catalog.get("pol:1.0.0") is None


# --- POST /publish: the caller's claims are claims ---------------------------------------


def _client(tmp_path: Path) -> tuple[TestClient, Registry]:
    registry = Registry(tmp_path / "reg")
    return TestClient(create_app(InMemoryCatalog(), registry=registry)), registry


def test_publish_endpoint_rejects_a_nonexistent_digest(tmp_path: Path) -> None:
    client, _registry = _client(tmp_path)
    manifest = make_manifest("pol", "1.0.0")
    response = client.post(
        "/hub/publish",
        json={
            "manifest": manifest.model_dump(mode="json"),
            "digest": _OTHER_DIGEST,
            "publisher": "mallory",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "admission_rejected"
    # All three refusals below carry that one code — it is the verdict, and it is what a client
    # branches on (api#4). Which of the three fired is the subject of these tests and lives only in
    # the relayed message, so they read it to tell the cases apart, not to identify the failure.
    assert "not present in this registry" in response.json()["detail"]
    assert client.get("/hub/artifacts/pol/1.0.0").status_code == 404


def test_publish_endpoint_rejects_a_manifest_that_disagrees_with_the_stored_config(
    tmp_path: Path,
) -> None:
    client, registry = _client(tmp_path)
    _, digest = _signed(registry)
    impostor = make_manifest("pol", "1.0.0", description="not what was stored")
    response = client.post(
        "/hub/publish",
        json={
            "manifest": impostor.model_dump(mode="json"),
            "digest": digest,
            "publisher": "mallory",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "admission_rejected"
    assert "does not match its stored config" in response.json()["detail"]
    assert client.get("/hub/artifacts/pol/1.0.0").status_code == 404


def test_publish_endpoint_rejects_an_unsigned_artifact(tmp_path: Path) -> None:
    client, registry = _client(tmp_path)
    manifest, digest = _unsigned(registry)
    response = client.post(
        "/hub/publish",
        json={
            "manifest": manifest.model_dump(mode="json"),
            "digest": digest,
            "publisher": "mallory",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "admission_rejected"
    assert "unsigned" in response.json()["detail"]
    assert client.get("/hub/artifacts/pol/1.0.0").status_code == 404


def test_publish_endpoint_refuses_a_caller_asserted_trust_tier(tmp_path: Path) -> None:
    """A trust tier is granted by an audited promotion, never claimed in a publish request."""
    client, registry = _client(tmp_path)
    manifest, digest = _signed(registry)
    response = client.post(
        "/hub/publish",
        json={
            "manifest": manifest.model_dump(mode="json"),
            "digest": digest,
            "publisher": "mallory",
            "namespace": "verified",
        },
    )
    assert response.status_code == 403
    assert client.get("/hub/artifacts/pol/1.0.0").status_code == 404


def test_publish_endpoint_is_unavailable_without_a_registry() -> None:
    """Indexing with nothing to verify against cannot be fail-closed, so it is refused outright."""
    client = TestClient(create_app(InMemoryCatalog()))
    response = client.post(
        "/hub/publish",
        json={
            "manifest": make_manifest("pol", "1.0.0").model_dump(mode="json"),
            "digest": _OTHER_DIGEST,
            "publisher": "p",
        },
    )
    assert response.status_code == 503


# --- one gate, not three -----------------------------------------------------------------


def test_the_library_and_the_service_reach_the_same_gate(tmp_path: Path) -> None:
    """Deliverable 4: the drift this issue exists to prevent — a check on one path, absent on
    another. The same unsigned artifact must be refused identically through both doors."""
    registry = Registry(tmp_path / "reg")
    manifest, digest = _unsigned(registry)

    library_catalog = InMemoryCatalog()
    with pytest.raises(UnsignedArtifactError) as library_error:
        admit(registry, library_catalog, manifest, digest=digest, publisher="p")

    service = TestClient(create_app(InMemoryCatalog(), registry=registry))
    response = service.post(
        "/hub/publish",
        json={
            "manifest": manifest.model_dump(mode="json"),
            "digest": digest,
            "publisher": "p",
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "admission_rejected"
    # Not merely "both refused" — the *same* verdict text, which is what proves one gate. An
    # equality against the library's own message asserts the route **relays** it; it is the one
    # thing a message assertion is still for once there is a code (api#4).
    assert response.json()["detail"] == str(library_error.value)
    assert library_catalog.get("pol:1.0.0") is None


def test_promotion_reaches_the_same_gate(tmp_path: Path) -> None:
    """The third door: curation re-runs admission's checks rather than trusting publish time."""
    from astro_mine.hub.curation import CurationError, promote

    registry = Registry(tmp_path / "reg")
    manifest, digest = _unsigned(registry)
    catalog = InMemoryCatalog()
    ingest(catalog, manifest, digest=digest, publisher="p")  # indexed by a pre-gate path

    with pytest.raises(CurationError, match="does not verify"):
        promote(catalog, "pol:1.0.0", to="verified", registry=registry)


def test_publish_endpoint_rejects_a_malformed_manifest_as_422(tmp_path: Path) -> None:
    """The second instance of #21: a domain model built from a caller-supplied mapping.

    `body.manifest` is an untyped object, so FastAPI validates the *envelope* and not the manifest
    inside it. `PluginManifest.model_validate` therefore raises a pydantic error that is not
    FastAPI's request-validation error, reaches no handler, and answered 500 — telling the caller
    the server broke when in fact they sent an unparseable manifest.

    Asserted as a status rather than a message: a 500 makes a client retry and page someone, and
    neither response is right for input that will never parse however many times it is sent.
    """
    client, registry = _client(tmp_path)
    _, digest = _signed(registry)
    response = client.post(
        "/hub/publish",
        json={
            "manifest": {"name": "pol", "kind": "not-a-real-kind"},  # missing fields, bad enum
            "digest": digest,
            "publisher": "mallory",
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_failed"
    assert client.get("/hub/artifacts/pol/1.0.0").status_code == 404
