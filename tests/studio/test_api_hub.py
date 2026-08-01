"""RM-P1-STUDIO-06 — the API edge over the Hub seams and the comparison view.

The app never imports ``astro_mine.hub``: it takes the two Protocols. These tests bind them to a
real registry, so what is exercised end-to-end is the same publish/verify path a deployment runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from astro_mine.core.objective import ObjectiveDocument
from astro_mine.core.registry import PluginKind, PluginManifest
from astro_mine.hub.client import HubClient
from astro_mine.hub.registry import Blob, Registry
from astro_mine.hub.supply_chain import generate_keypair
from astro_mine.studio.campaign import author_campaign
from astro_mine.studio.hub import HubArtifactPublisher, HubCapabilityResolver, HubWorldMaterializer
from astro_mine.studio.models import (
    AssetSelection,
    CampaignPhase,
    CandidateScore,
    DesignCandidate,
    EvaluatedCandidate,
    TradeStudy,
)
from astro_mine.studio.orchestrate import (
    LOCAL_STAND_IN_EVALUATOR_ID,
    SiblingClients,
    evaluate_candidate,
)
from astro_mine.studio.provenance import capture_provenance
from fastapi.testclient import TestClient

from astro_mine_api.studio import create_app

BUNDLE_MEDIA_TYPE = "application/vnd.astro-mine.world.bundle.v1.tar"
ROVER_REF = "rover:0.1.0"


#: The `crs` + `tiles_anchor` a Worlds bundle publishes — the Shackleton anchor's own values. The
#: API reads them into `WorldResponse.site` so a design-time swarm has a real place to stand
#: (studio#50); a bundle without them yields `site: null`, which is the degraded path.
ANCHOR_CRS = {
    "body": "MOON",
    "body_fixed_frame": "MOON_ME",
    "reference_radius_m": 1737400.0,
    "projection": "+proj=stere +lat_0=-90",
    "datum": None,
}
TILES_ANCHOR = {
    "frame": "MOON_ME",
    "origin": {
        "latitude_deg": -89.98880693428801,
        "longitude_deg": -45.0,
        "height_m": -984.8629101332172,
    },
}


def _world_tar(*, anchored: bool = True) -> bytes:
    import io
    import tarfile

    manifest: dict[str, object] = {"world_id": "w1", "tiles": "tiles/tileset.json"}
    if anchored:
        manifest |= {"crs": ANCHOR_CRS, "tiles_anchor": TILES_ANCHOR}
    payload = json.dumps(manifest).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("world.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


@pytest.fixture
def registry(tmp_path: Path) -> tuple[Registry, bytes, bytes]:
    private_pem, public_pem = generate_keypair()
    reg = Registry(tmp_path / "registry")
    client = HubClient(reg)
    client.publish(
        name="rover",
        version="0.1.0",
        kind="asset",
        manifest=PluginManifest(name="rover", version="0.1.0", kind=PluginKind.ASSET),
        private_key_pem=private_pem,
    )
    client.publish(
        name="w1",
        version="0.1.0",
        kind="world",
        manifest=PluginManifest(
            name="w1",
            version="0.1.0",
            kind=PluginKind.WORLD_PROVIDER,
            attributes={"world_id": "w1", "bundle_media_type": BUNDLE_MEDIA_TYPE},
        ),
        layers=[Blob(BUNDLE_MEDIA_TYPE, _world_tar())],
        private_key_pem=private_pem,
    )
    return reg, private_pem, public_pem


@pytest.fixture
def app_client(registry: tuple[Registry, bytes, bytes], tmp_path: Path) -> TestClient:
    reg, private_pem, public_pem = registry
    hub_client = HubClient(reg, trusted_public_key_pem=public_pem)
    cache = tmp_path / "worlds"
    cache.mkdir()
    return TestClient(
        create_app(
            publisher=HubArtifactPublisher(
                hub_client,
                capability_resolver=HubCapabilityResolver(reg),
                private_key_pem=private_pem,
            ),
            materializer=HubWorldMaterializer(hub_client, cache_dir=cache),
            world_cache_dir=str(cache),
        )
    )


def _campaign(objective_doc: ObjectiveDocument, clients: SiblingClients):
    candidate = DesignCandidate(id="c1", swarm=[AssetSelection(sadf_ref=ROVER_REF, count=2)])
    chosen = evaluate_candidate(candidate, objective_doc, clients=clients, seed=3)
    return author_campaign(
        objective_doc, chosen, name="Ice", phases=[CampaignPhase(id="p", name="Prospect")]
    )


class TestPublishRoutes:
    def test_publish_then_pull_by_digest(
        self, app_client: TestClient, objective_doc: ObjectiveDocument, clients: SiblingClients
    ) -> None:
        campaign = _campaign(objective_doc, clients)
        response = app_client.post(
            "/studio/campaigns/publish",
            json={"campaign": campaign.model_dump(mode="json"), "name": "ice", "version": "0.1.0"},
        )
        assert response.status_code == 200, response.text
        published = response.json()
        assert published["kind"] == "campaign"
        assert published["digest"].startswith("sha256:")

        pulled = app_client.get(f"/studio/campaigns/{published['digest']}")
        assert pulled.status_code == 200, pulled.text
        assert pulled.json()["id"] == campaign.id

    def test_publish_from_chosen_candidate(
        self, app_client: TestClient, objective_doc: ObjectiveDocument, clients: SiblingClients
    ) -> None:
        # The journey's publish step sends the chosen EvaluatedCandidate + its objective; the route
        # authors the Campaign server-side (proper lineage) — the UI never assembles one by hand.
        candidate = DesignCandidate(id="c1", swarm=[AssetSelection(sadf_ref=ROVER_REF, count=2)])
        chosen = evaluate_candidate(candidate, objective_doc, clients=clients, seed=3)
        response = app_client.post(
            "/studio/campaigns/publish",
            json={
                "name": "ice",
                "version": "0.2.0",
                "objective": objective_doc.model_dump(mode="json"),
                "chosen": chosen.model_dump(mode="json"),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "campaign"

    def test_publish_without_a_campaign_or_chosen_is_422(self, app_client: TestClient) -> None:
        response = app_client.post(
            "/studio/campaigns/publish", json={"name": "x", "version": "0.1.0"}
        )
        assert response.status_code == 422

    def test_republishing_a_version_is_a_conflict(
        self, app_client: TestClient, objective_doc: ObjectiveDocument, clients: SiblingClients
    ) -> None:
        campaign = _campaign(objective_doc, clients)
        body = {"campaign": campaign.model_dump(mode="json"), "name": "ice", "version": "0.1.0"}
        assert app_client.post("/studio/campaigns/publish", json=body).status_code == 200
        conflict = app_client.post("/studio/campaigns/publish", json=body)
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "conflict"

    def test_pulling_an_unknown_campaign_is_a_404(self, app_client: TestClient) -> None:
        assert app_client.get("/studio/campaigns/nope:0.0.1").status_code == 404


class TestWorldRoutes:
    def test_materializes_and_serves_the_bundle(self, app_client: TestClient) -> None:
        response = app_client.get("/studio/worlds/w1:0.1.0")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["world_id"] == "w1"
        assert body["digest"].startswith("sha256:")

        # The URL the embedded <GlobeScene> fetches actually serves the bytes Worlds published.
        served = app_client.get(body["manifest_url"])
        assert served.status_code == 200
        assert served.json()["tiles"] == "tiles/tileset.json"

    def test_an_unknown_world_is_a_404(self, app_client: TestClient) -> None:
        assert app_client.get("/studio/worlds/ghost:9.9.9").status_code == 404

    def test_serves_the_bundles_own_tileset_anchor_as_the_swarm_site(
        self, app_client: TestClient
    ) -> None:
        """`site` is read from the verified bundle, never chosen by Studio (studio#50).

        The inspection pane needs somewhere to stand a *proposed* swarm, and a design has no run,
        so it has no simulated poses. The one position a world bundle publishes that means "this
        terrain is here" is its tileset anchor — the same one View places the terrain at.
        """
        site = app_client.get("/studio/worlds/w1:0.1.0").json()["site"]
        assert site == {
            "body": "MOON",
            "frame": "MOON_ME",
            "reference_radius_m": 1737400.0,
            "latitude_deg": -89.98880693428801,
            "longitude_deg": -45.0,
            "height_m": -984.8629101332172,
        }

    def test_a_bundle_without_an_anchor_yields_no_site_rather_than_a_guess(
        self, tmp_path: Path, registry: tuple[Registry, bytes, bytes]
    ) -> None:
        """An older bundle degrades to "the swarm cannot be placed" — not a 500, not a guess."""
        reg, private_pem, public_pem = registry
        HubClient(reg).publish(
            name="w0",
            version="0.1.0",
            kind="world",
            manifest=PluginManifest(
                name="w0",
                version="0.1.0",
                kind=PluginKind.WORLD_PROVIDER,
                attributes={"world_id": "w0", "bundle_media_type": BUNDLE_MEDIA_TYPE},
            ),
            layers=[Blob(BUNDLE_MEDIA_TYPE, _world_tar(anchored=False))],
            private_key_pem=private_pem,
        )
        client = TestClient(
            create_app(
                materializer=HubWorldMaterializer(
                    HubClient(reg, trusted_public_key_pem=public_pem),
                    cache_dir=tmp_path / "cache-unanchored",
                )
            )
        )
        response = client.get("/studio/worlds/w0:0.1.0")
        assert response.status_code == 200, response.text
        assert response.json()["site"] is None


class TestComparisonRoute:
    def test_returns_bounds_alongside_estimates(self) -> None:
        client = TestClient(create_app())
        evaluated = EvaluatedCandidate(
            candidate=DesignCandidate(id="c1", swarm=[]),
            score=CandidateScore(
                objective_hash="sha256:obj",
                metric_scores={"water": 10.0},
                metric_uncertainty={"water": 2.0},
                aggregate=10.0,
                passed=True,
            ),
            seed=1,
            world_ref="sha256:w",
            provenance=capture_provenance(input_hashes=["sha256:i"], seed=1),
        )
        study = TradeStudy(
            id="ts",
            objective_hash="sha256:obj",
            backend="random",
            evaluator=LOCAL_STAND_IN_EVALUATOR_ID,
            seeds=[1],
            evaluated=[evaluated],
            pareto_front=["c1"],
            provenance=capture_provenance(input_hashes=["sha256:i"], seed=1),
        )
        response = client.post("/studio/studies/comparison", json=study.model_dump(mode="json"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["metrics"] == ["water"]
        assert body["candidates"][0]["metrics"]["water"] == {"value": 10.0, "uncertainty": 2.0}
        assert body["candidates"][0]["on_pareto_front"] is True


class TestWithoutTheHubSeams:
    """Built with no registry to resolve from, the Hub-backed routes say so rather than pretend."""

    def test_terrain_and_pull_report_unavailable(self) -> None:
        client = TestClient(create_app())
        for path in ("/studio/worlds/w1:0.1.0", "/studio/campaigns/anything:1.0.0"):
            response = client.get(path)
            assert response.status_code == 503
            # The code, not the message (api#4). Which seam is unwired is in `detail` for a person
            # to read; a client branches on this.
            assert response.json()["code"] == "capability_unavailable"

    def test_publish_reports_unavailable_once_the_body_validates(
        self, objective_doc: ObjectiveDocument, clients: SiblingClients
    ) -> None:
        # A malformed body is a 422 before the seam is consulted, so send a real campaign.
        campaign = _campaign(objective_doc, clients)
        response = TestClient(create_app()).post(
            "/studio/campaigns/publish",
            json={"campaign": campaign.model_dump(mode="json"), "name": "n", "version": "0.1.0"},
        )
        assert response.status_code == 503
        assert response.json()["code"] == "capability_unavailable"


def test_the_route_module_never_imports_hub() -> None:
    """The narrow-waist guarantee, enforced rather than asserted in a comment.

    The Studio route module takes the Hub seams as Protocols. If it ever imported the client
    directly, the surface would gain a concrete dependency on Hub's implementation (studio.md §2)
    and a deployment with no registry could not even import it. Only the *composition*
    (`astro_mine_api.studio.serve`) is allowed to reach for the client, and only when a registry
    was configured.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import astro_mine_api.studio.app;"
        " assert 'astro_mine.hub' not in sys.modules, sorted("
        "n for n in sys.modules if n.startswith('astro_mine.hub'))"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)
