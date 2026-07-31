"""API tests (RM-P1-HUB-02): the FastAPI façade — publish/search/resolve/artifact/gated download."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astro_mine.hub.index import InMemoryCatalog
from astro_mine.hub.policy import InMemoryAuditLog
from astro_mine.hub.registry import Registry
from astro_mine.hub.supply_chain import attest, generate_keypair
from fastapi.testclient import TestClient

from astro_mine_api.hub import create_app

from .conftest import make_manifest


def _app(tmp_path: Path, **kw: Any) -> tuple[TestClient, Registry]:
    """A client over an app wired to a real registry — publishing now verifies against one."""
    registry = Registry(tmp_path / "reg")
    return TestClient(create_app(InMemoryCatalog(), registry=registry, **kw)), registry


def _store(registry: Registry, name: str, version: str, **mkw: Any) -> tuple[Any, str]:
    """Store *and attest* an artifact, the way a real publisher does, and return (manifest, digest).

    `POST /publish` indexes an artifact that already exists in the registry; admission re-derives
    every claim from it, so the fixture has to produce a genuine one.
    """
    manifest = make_manifest(name, version, **mkw)
    published = registry.publish(
        name=name, version=version, kind="policy", config=manifest.model_dump(mode="json")
    )
    private_pem, _ = generate_keypair()
    attest(registry, published.digest, private_key_pem=private_pem, name=name, version=version)
    return manifest, published.digest


def _publish(
    client: TestClient,
    registry: Registry,
    name: str = "excavator",
    version: str = "1.0.0",
    **mkw: Any,
) -> Any:
    manifest, digest = _store(registry, name, version, **mkw)
    response = client.post(
        "/hub/publish",
        json={
            "manifest": manifest.model_dump(mode="json"),
            "digest": digest,
            "publisher": "alice",
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()


def test_health() -> None:
    client = TestClient(create_app(InMemoryCatalog()))
    body = client.get("/hub/health").json()
    assert body["status"] == "ok"


def test_publish_and_search(tmp_path: Path) -> None:
    client, registry = _app(tmp_path)
    _publish(client, registry, description="lunar excavation digging policy")
    hits = client.get("/hub/search", params={"semantic": "excavation"}).json()
    assert hits[0]["name"] == "excavator" and hits[0]["score"] > 0
    assert client.get("/hub/search", params={"kind": "policy"}).json()[0]["name"] == "excavator"
    assert client.get("/hub/search", params={"kind": "world_provider"}).json() == []


def test_artifact_detail_and_404(tmp_path: Path) -> None:
    client, registry = _app(tmp_path)
    _publish(client, registry)
    body = client.get("/hub/artifacts/excavator/1.0.0").json()
    assert body["record"]["name"] == "excavator"
    assert body["attestations"]  # a registry is wired, and admission proved these exist
    assert client.get("/hub/artifacts/nope/1.0.0").status_code == 404


def test_resolve_and_404(tmp_path: Path) -> None:
    client, registry = _app(tmp_path)
    _publish(client, registry, version="1.0.0")
    _publish(client, registry, version="1.2.0")
    resolved = client.post(
        "/hub/resolve", json={"name": "excavator", "version_spec": ">=1.0.0,<2.0.0"}
    )
    assert resolved.status_code == 200 and resolved.json()["version"] == "1.2.0"
    assert client.post("/hub/resolve", json={"name": "nope"}).status_code == 404


def test_download_gate_fails_closed_and_audits(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    client, registry = _app(tmp_path, audit=audit)
    _publish(client, registry, name="secret", tags=["operational_targeting"])
    denied = client.post("/hub/artifacts/secret/1.0.0/download", json={})
    assert denied.status_code == 403 and "digest" not in denied.json()
    allowed = client.post(
        "/hub/artifacts/secret/1.0.0/download", json={"grants": ["operational_targeting"]}
    )
    assert allowed.status_code == 200 and allowed.json()["digest"]
    assert any(record.action == "download" for record in audit.records)
    assert client.post("/hub/artifacts/nope/1.0.0/download", json={}).status_code == 404


def test_artifact_detail_shows_attestations(tmp_path: Path) -> None:
    client, registry = _app(tmp_path)
    _publish(client, registry, name="pol", version="1.0.0")
    attestations = client.get("/hub/artifacts/pol/1.0.0").json()["attestations"]
    assert "application/vnd.astro-mine.signature.v1" in attestations
    assert "application/vnd.astro-mine.sbom.cyclonedx.v1" in attestations


def test_asgi_factory_builds_app() -> None:
    from astro_mine_api.hub._asgi import make_app

    app = make_app()  # HUB_POSTGRES_URL unset → in-memory SQLite
    assert app.title == "Astro-Mine Hub"
