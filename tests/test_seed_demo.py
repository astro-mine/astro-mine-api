"""The seeder produces a deployment that answers (api#14).

``scripts/seed_demo.py`` exists so a fresh bring-up has something to serve, and the only way to
know it does is to bring one up. So this runs the script for real — as a subprocess, through its
own command line, with no import-time shortcuts — and then drives the deployment its output
describes.

**Asserted on the routes, not on the files it wrote.** A test that checked for a registry directory
and a SQLite file would pass on a registry full of artifacts no surface can read; the whole failure
mode a seeder has is producing state that exists and does not serve. So every assertion below is a
request, and each one names the front-end journey that depends on it (``astro-mine-ui#20``).

It is the slowest test in this repository by a wide margin — it publishes and signs artifacts, runs
the Studio design loop, and scores two submissions through the real sandboxed evaluator. That is
the cost of the thing being real, and the alternative is a fixture nobody can trust.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "seed_demo.py"


def _seed_module() -> ModuleType:
    """Import the script by path — ``scripts/`` is deliberately not a package."""
    spec = importlib.util.spec_from_file_location("seed_demo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_demo = _seed_module()


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Run the seeder once and hand every test its manifest.

    Module-scoped because seeding costs real time: two sandboxed evaluations and a design study.
    The tests below only read, so sharing one seeded root between them is safe.

    ``/tmp`` rather than the repository, and that is not only tidiness: the evaluation worker is
    confined with Landlock, and a 9p/drvfs mount (a WSL checkout under ``/mnt``) denies even the
    paths the ruleset grants — so a seed root on one cannot be scored. ``tmp_path_factory`` gives a
    native path on both CI and a developer machine.
    """
    root = tmp_path_factory.mktemp("seed")
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--json"],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert completed.returncode == 0, (
        f"the seeder failed ({completed.returncode}):\n{completed.stderr[-4000:]}"
    )
    return json.loads(completed.stdout)


@pytest.fixture(scope="module")
def client(seeded: dict[str, Any]) -> Iterator[TestClient]:
    """The deployment the seeded environment describes, plus a JWKS server for its token.

    The environment is applied with ``os.environ`` directly rather than through ``monkeypatch``,
    which is function-scoped and cannot hold for a module-scoped app — and restored in the teardown
    below so no other test in the session inherits a Bench store pointing at a temp directory.
    """
    import os

    from astro_mine_api._app import build_app

    seed_demo._JwksHandler.payload = Path(seeded["jwks_path"]).read_bytes()
    server = ThreadingHTTPServer(("127.0.0.1", 0), seed_demo._JwksHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = {
        **seeded["env"],
        "ASTRO_MINE_BENCH_OIDC_JWKS_URL": (
            f"http://127.0.0.1:{server.server_address[1]}/jwks.json"
        ),
    }
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        with TestClient(build_app()) as test_client:
            yield test_client
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def steward(seeded: dict[str, Any]) -> dict[str, str]:
    """The seeded bearer token, as a header."""
    return {"Authorization": f"Bearer {seeded['oidc']['token']}"}


# --- what the deployment serves ----------------------------------------------------------------


def test_the_deployment_mounts_every_surface(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert set(body["surfaces"]) == {"hub", "studio", "cloud", "bench"}


def test_every_seeded_name_obeys_the_artifact_name_rule() -> None:
    """`conventions.md` §13, checked against the platform's own predicate rather than a regex here.

    Cheap and independent of the `seeded` fixture on purpose: `HubClient.publish` enforces §13, so
    a legacy-shaped name does not seed a badly-named artifact, it kills the seeder outright and
    takes every other test in this module down as a fixture error. That is what the artifact-name
    migration did on the day it landed. This one fails in milliseconds and names the offender.
    """
    from astro_mine.hub.registry import is_valid_artifact_name

    names = {
        "WORLD_ID": seed_demo.WORLD_ID,
        "POLICY_ID": seed_demo.POLICY_ID,
        **{f"ASSETS[{i}]": asset["id"] for i, asset in enumerate(seed_demo.ASSETS)},
    }
    bad = {where: name for where, name in names.items() if not is_valid_artifact_name(name)}
    assert not bad, f"names violating conventions.md §13: {bad}"


def test_the_seeded_assets_satisfy_the_example_campaign_s_pins() -> None:
    """The coupling that a rename breaks silently until the seeder dies on `ArtifactNotFound`.

    `seed_studio` publishes the platform's example campaign, and that campaign pins its swarm by
    reference (`astro_mine.studio.seed`). The seeder is what puts those assets in the registry, so
    the two sets of names have to agree -- and nothing else in this repository says so. Asserted
    against the platform's campaign rather than a copied literal, so the platform moving its pin
    fails here on the next canary rather than in a demo bring-up.
    """
    from astro_mine.studio.seed import _author_example

    def sadf_refs(node: Any) -> Iterator[str]:
        """Every `sadf_ref` anywhere in the campaign, found by walking rather than by path.

        The pins sit in two different places today (the objective's `inventory` and each design
        candidate's `swarm`) and a third would be added without anyone thinking of this test. A
        walk cannot go stale against the model's shape; a hard-coded path can, and would go stale
        by silently finding nothing, which is the failure this test exists to catch.
        """
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "sadf_ref" and isinstance(value, str):
                    yield value
                else:
                    yield from sadf_refs(value)
        elif isinstance(node, list | tuple):
            for item in node:
                yield from sadf_refs(item)

    campaign = asyncio.run(_author_example())
    pinned = set(sadf_refs(campaign.model_dump(mode="json")))
    assert pinned, "found no pins to check -- the walk went stale, not the campaign"

    seeded_refs = {f"{asset['id']}:0.1.0" for asset in seed_demo.ASSETS}
    missing = sorted(pinned - seeded_refs)
    assert not missing, f"the example campaign pins assets the seeder never publishes: {missing}"


def test_the_registry_is_searchable(client: TestClient, seeded: dict[str, Any]) -> None:
    """UC-G2 — the registry browse and search pages (`ui#10`).

    Searching, rather than listing, because ``GET /hub/search`` is the route the browse page calls
    and the one api#15 made answer 500 on every unconfigured deployment. Here it is driven against
    a configured one; ``test_composition.py`` drives it against the default.
    """
    hits = client.get("/hub/search", params={"text": "rover"}).json()
    assert [hit["reference"] for hit in hits] == ["prospecting-rover:0.1.0"]

    policies = client.get("/hub/search", params={"kind": "policy"}).json()
    assert {hit["reference"] for hit in policies} == set(seeded["hub"]["policies"])


def test_an_artifact_carries_its_identity_and_its_attestations(client: TestClient) -> None:
    """UC-G3 — the artifact page's identity and supply-chain evidence (`ui#10`).

    The attestations are the point: ``ui.md`` §7 rule 6 says verification is claimed only where it
    happened, so the page distinguishes *evidence present in a registry* from *a verified supply
    chain*. It can only make that distinction against an artifact that actually carries evidence.
    """
    detail = client.get("/hub/artifacts/lawnmower-survey/0.2.0").json()
    assert detail["digest"].startswith("sha256:")
    assert detail["version"] == "0.2.0"
    # HubClient.publish signs and attaches all three; anything less would mean admission let an
    # under-attested artifact through. Their exact spelling is pinned in `test_composition.py` —
    # here what matters is that a *seeded* deployment surfaces them at all, which it could not
    # before the Hub router was given its registry (api#16).
    assert len(detail["attestations"]) == 3


def test_a_version_range_resolves_to_a_pinned_digest(client: TestClient) -> None:
    """UC-G3 — the resolve page (`ui#11`): a tag is a query, the digest is the identity."""
    resolved = client.post(
        "/hub/resolve",
        json={"name": "lawnmower-survey", "version_spec": ">=0.1.0"},
    ).json()
    # Two versions were published precisely so this has something to choose between.
    assert resolved["version"] == "0.2.0"
    assert resolved["digest"].startswith("sha256:")


def test_the_design_surface_has_assets_a_world_and_a_campaign(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    """UC-F1 to UC-F6 — the design pages (`ui#15` through `ui#18`)."""
    assets = client.get("/studio/catalog/assets").json()
    assert {asset["reference"] for asset in assets} == set(seeded["hub"]["assets"])

    # The tag filter has something to filter: only one seeded asset excavates.
    excavators = client.get(
        "/studio/catalog/assets", params={"requires": ["excavation.bucket"]}
    ).json()
    assert [asset["reference"] for asset in excavators] == ["excavator:0.1.0"]

    worlds = client.get("/studio/catalog/worlds").json()
    assert [world["reference"] for world in worlds] == seeded["hub"]["worlds"]

    campaign = client.get(f"/studio/campaigns/{seeded['studio']['campaign']}")
    assert campaign.status_code == 200


def test_the_world_materializes_with_its_anchor(client: TestClient, seeded: dict[str, Any]) -> None:
    """`ui#17` — the 3D candidate inspection lays a swarm out on the bundle's own tileset anchor.

    ``site`` is ``None`` for a bundle that predates the published anchor, and the surface then says
    the swarm cannot be placed. Asserting it is present is asserting the seeded bundle is not that
    bundle — otherwise the inspection journey would exercise the degraded path while looking green.
    """
    world = client.get(f"/studio/worlds/{seeded['hub']['worlds'][0]}").json()
    assert world["manifest_url"].endswith("world.json")
    assert world["site"] is not None
    assert world["site"]["body"] == "MOON"
    assert world["site"]["latitude_deg"] == pytest.approx(-89.66)


def test_the_leaderboard_has_more_than_one_row_to_rank(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    """UC-G5 — the leaderboard and the scorecard (`ui#12`).

    Two rows, because a board with one row is a ranking that never had to sort anything: `rank`
    would read 1 whether the ordering worked or not.
    """
    board = client.get(f"/bench/leaderboard/{seeded['bench']['scenario_id']}").json()
    assert len(board) == len(seeded["bench"]["submissions"])
    assert [row["rank"] for row in board] == list(range(1, len(board) + 1))

    scorecards = client.get(
        f"/bench/leaderboard/{seeded['bench']['scenario_id']}/scorecards"
    ).json()
    assert scorecards["primary_metric"]
    # Every row carries its full per-metric scorecard, which is what the scorecard view renders.
    assert all(row["scores"] for row in scorecards["rows"])


def test_every_seeded_entry_has_a_replay_to_play(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    """UC-B6 — the episode replay (`ui#13`), on whichever row a reader opens."""
    for entry in seeded["bench"]["submissions"]:
        manifest = client.get(f"/bench/submissions/{entry['submission_id']}/replay/manifest")
        assert manifest.status_code == 200, entry["method"]
        assert manifest.json()["submission_id"] == entry["submission_id"]


def test_no_seeded_entry_has_a_provenance_bundle(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    """The gap, asserted rather than left to be discovered — astro-mine-platform#14.

    Only Bench's Hub-digest intake writes a provenance bundle, and it cannot read an artifact this
    platform publishes. Until that is fixed the front end's provenance view opens onto its own "no
    bundle stored" explanation, and **this test is what will fail when the fix lands** — which is
    the point of asserting a known gap instead of skipping it.
    """
    for entry in seeded["bench"]["submissions"]:
        response = client.get(f"/bench/submissions/{entry['submission_id']}/provenance")
        assert response.status_code == 404
        assert response.json()["code"] == "content_not_found"


def test_the_audit_trail_reads_with_the_seeded_token(
    client: TestClient, steward: dict[str, str], seeded: dict[str, Any]
) -> None:
    """UC-G7 — the steward's audit trail (`ui#14`), and two proofs in one request.

    **That the token verifies:** a minted token, a real RS256 signature, and a JWKS fetched over
    HTTP by the deployment's own verifier. If any of the three were wrong this is where it would
    show, rather than in a browser.

    **That the trail is durable:** the events below were written by the *seeder's* process and are
    read here by a second one over the same database. Before api#17 wired ``SqlAuditLog`` this
    returned only the authn events the reading process had just generated — every one of them with
    ``submission_id: None`` — while the submissions themselves persisted fine. That is the exact
    shape of the failure, so it is the exact shape of the assertion.
    """
    events = client.get("/bench/audit", headers=steward).json()
    assert events, "seeding two submissions should have left an audit trail"
    assert {event["submission_id"] for event in events} >= {
        entry["submission_id"] for entry in seeded["bench"]["submissions"]
    }


def test_the_write_path_is_closed_without_a_token(client: TestClient) -> None:
    """The other half of UC-G7: reads are account-free, writes are not (bench#29 AC5)."""
    response = client.delete(f"/bench/submissions/{'sha256:' + 'a' * 64}")
    assert response.status_code == 401


def test_a_reader_can_still_submit_something_new(
    client: TestClient, steward: dict[str, str], seeded: dict[str, Any]
) -> None:
    """UC-G4 — the submit form (`ui#14`) has a policy left to submit.

    The seeder deliberately leaves one policy unsubmitted. Without it the front end's submit journey
    would re-submit a policy already on the board, get the existing entry back — submission ids are
    content addresses, so that is a success — and prove nothing about the write path.
    """
    policy_ref = seeded["bench"]["unsubmitted_policy_ref"]
    board_before = client.get(f"/bench/leaderboard/{seeded['bench']['scenario_id']}").json()
    assert policy_ref not in {row.get("policy_ref") for row in board_before}

    response = client.post(
        "/bench/submissions",
        json={
            "scenario_id": seeded["bench"]["scenario_id"],
            "policy_ref": policy_ref,
            "method": "Submitted by the test",
        },
        headers=steward,
    )
    assert response.status_code == 200, response.text
    assert response.json()["policy_ref"] == policy_ref


def test_compute_answers_without_being_seeded(client: TestClient) -> None:
    """`ui#19` — Cloud's surface is stateless, so it needs no seed and must still answer."""
    assert client.get("/cloud/backends").json()["backends"]


# --- the properties the seeder itself promises ---------------------------------------------------


def test_seeding_twice_changes_nothing(seeded: dict[str, Any]) -> None:
    """Idempotence, run rather than claimed.

    The second run must resolve the same content addresses. A seeder that republished would mint new
    digests on every CI run, and every "the digest is the identity" assertion downstream would be
    asserting a fresh value against itself.
    """
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", seeded["root"], "--json"],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    again = json.loads(completed.stdout)

    assert again["hub"] == seeded["hub"]
    assert again["studio"] == seeded["studio"]
    assert [entry["submission_id"] for entry in again["bench"]["submissions"]] == [
        entry["submission_id"] for entry in seeded["bench"]["submissions"]
    ]
    # ...and the second run knew they were already there.
    assert all(entry["already_present"] for entry in again["bench"]["submissions"])
    # The token is the one thing that must differ: it is minted fresh because it expires.
    assert again["oidc"]["token"] != seeded["oidc"]["token"]


def test_the_seed_root_holds_no_secret_the_repository_would_carry(seeded: dict[str, Any]) -> None:
    """`conventions.md` §9 — key material lives in the seed root, never in the repository.

    Both private keys are generated per root and written with owner-only permissions. Deleting the
    root deletes the identity, which is the intent; a key that leaked into the tree would outlive
    every deployment that trusted it.
    """
    root = Path(seeded["root"])
    for key in (root / "oidc-signing-key.pem", root / "registry" / "keys" / "cosign.key"):
        assert key.is_file()
        assert key.stat().st_mode & 0o077 == 0, f"{key} is readable beyond its owner"
        assert REPO not in key.parents
