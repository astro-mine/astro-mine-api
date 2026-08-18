"""RM-P1-STUDIO-09 — the `/catalog` endpoints: the robot menu + a servable geometry preview."""

from __future__ import annotations

from pathlib import Path

import pytest
from astro_mine.core.registry import CapabilityTag
from astro_mine.hub.client import HubClient
from astro_mine.hub.registry import Registry
from astro_mine.hub.supply_chain import generate_keypair
from astro_mine.studio.hub import HubAssetCatalog, HubAssetPreviewMaterializer
from fastapi.testclient import TestClient

from astro_mine_api.studio import create_app

from ._assets import HOPPER, publish_asset


@pytest.fixture
def catalog_client(tmp_path: Path) -> TestClient:
    private_pem, public_pem = generate_keypair()
    reg = Registry(tmp_path / "registry")
    publish_asset(
        reg,
        private_pem,
        asset_id="relay-orbiter",
        kind="orbiter",
        name="Relay Orbiter",
        tags=[CapabilityTag("mobility.orbiter"), CapabilityTag("comms.relay")],
    )
    publish_asset(
        reg,
        private_pem,
        asset_id="hopper",
        kind="hopper",
        name="Hopper Mk1",
        tags=[CapabilityTag("mobility.wheeled")],
        geometry={"geometry/hopper.glb": b"GLB-BYTES-123"},
    )
    cache = tmp_path / "assets"
    cache.mkdir()
    client = HubClient(reg, trusted_public_key_pem=public_pem)
    return TestClient(
        create_app(
            catalog=HubAssetCatalog(reg),
            preview_materializer=HubAssetPreviewMaterializer(client, cache_dir=cache),
            asset_cache_dir=str(cache),
        )
    )


def test_menu_lists_the_published_assets(catalog_client: TestClient) -> None:
    response = catalog_client.get("/studio/catalog/assets")
    assert response.status_code == 200
    by_kind = {row["kind"]: row for row in response.json()}
    assert set(by_kind) == {"orbiter", "hopper"}
    assert "comms.relay" in by_kind["orbiter"]["capability_tags"]


def test_menu_requires_filters(catalog_client: TestClient) -> None:
    response = catalog_client.get(
        "/studio/catalog/assets", params={"requires": ["mobility.wheeled"]}
    )
    assert [row["kind"] for row in response.json()] == ["hopper"]


def test_menu_unknown_tag_is_422(catalog_client: TestClient) -> None:
    assert (
        catalog_client.get("/studio/catalog/assets", params={"requires": ["not.a.tag"]}).status_code
        == 422
    )


def test_preview_returns_a_servable_document_url(catalog_client: TestClient) -> None:
    response = catalog_client.get(f"/studio/catalog/preview/{HOPPER}")
    assert response.status_code == 200
    document_url = response.json()["document_url"]
    assert document_url.startswith("/studio/assets/files/")

    # The URL actually serves the SADF JSON, and the glTF resolves beside it — the View contract.
    assert catalog_client.get(document_url).status_code == 200
    glb_url = document_url.rsplit("/", 1)[0] + "/geometry/hopper.glb"
    assert catalog_client.get(glb_url).content == b"GLB-BYTES-123"


def test_preview_of_an_unknown_reference_is_404(catalog_client: TestClient) -> None:
    assert catalog_client.get("/studio/catalog/preview/no.such.asset:9.9.9").status_code == 404


def test_catalog_routes_are_503_without_the_hub_extra() -> None:
    client = TestClient(create_app())
    assert client.get("/studio/catalog/assets").status_code == 503
    assert client.get("/studio/catalog/preview/x:0.1.0").status_code == 503


# --------------------------------------------------------------------------- #
# The world menu (#45) — UC-F5's missing front door
# --------------------------------------------------------------------------- #


def test_world_menu_lists_only_worlds_not_assets(catalog_client: TestClient) -> None:
    """The same registry query as the robot menu, a different kind filter. The fixture registry
    holds assets only, so the world menu must be empty rather than offering rows that error the
    moment they are clicked."""
    response = catalog_client.get("/studio/catalog/worlds")
    assert response.status_code == 200
    assert response.json() == []


def test_world_menu_is_503_without_the_hub_extra() -> None:
    """No catalog seam bound: answer honestly rather than pretending there are no worlds."""
    client = TestClient(create_app())
    assert client.get("/studio/catalog/worlds").status_code == 503
