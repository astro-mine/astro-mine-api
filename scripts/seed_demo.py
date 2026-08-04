#!/usr/bin/env python
"""Seed a deployment with content, so a fresh bring-up has something to serve (api#14).

    uv run python scripts/seed_demo.py --root .demo
    uv run python scripts/seed_demo.py --root .demo --json      # machine-readable only

A deployment brought up from nothing answers ``/healthz`` and then has nothing to say: the registry
is empty, the leaderboard has no rows, the catalog has no assets. Every surface reports that
correctly and the result is useless — for a demo, for the guide's local walkthrough, and above all
for the front end's end-to-end suite (``astro-mine-ui#20``), whose whole argument is that a browser
test driving a **real** API proves something a mocked component test cannot.

**Everything here is a call into the platform, which is why it lives in this repository** rather
than in the front end: this is the distribution that owns the deployment and its environment
wiring, and a seeder is the deployment's own knowledge of how to fill its stores.

**The seeded state is a state the API can reach.** Bench entries are created by POSTing to the real
``/bench/submissions`` route with a real minted token, through the real sandboxed evaluator — not by
writing rows into the store. A seeder that fabricates records the service itself cannot produce is a
fixture that certifies its own fiction, and the first thing it hides is the route being broken.

The one exception is the episode replay, and it is exception by absence rather than by choice: the
platform offers ``LeaderboardService.attach_replay`` as a producer seam and **no REST route**, so
attaching is a library call here too. It is marked where it happens.

**Idempotent.** Every step checks for its own output first, so a second run against the same root is
a no-op that reprints the same handles — except the bearer token, which is minted fresh on every run
because tokens expire and a stale one is worse than none.

**Offline.** No network, no hosted Hub, no Docker, no IdP. The registry is a local OCI layout, the
catalog and submission store are SQLite files, and the "IdP" is an RSA keypair generated here whose
public half is written out as a JWKS for the caller to serve.

**What is a stand-in, and says so.** The world bundle is a synthetic three-tile stub named for what
it is, not the LOLA-derived Shackleton bundle the anchor scenario really uses (that one is hundreds
of megabytes and lives in the workspace's registry mirror). It carries a real CRS and a real tileset
anchor so the surfaces that read those read something true; it carries no terrain. ``ui.md`` §7
rule 1 — a stand-in must never look like the real thing — is the reason its name and description say
"demo stand-in" everywhere they surface.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from astro_mine.core.registry import CapabilityTag, PluginKind, PluginManifest, Provenance
from astro_mine.hub.client import HubClient
from astro_mine.hub.index import sql_catalog
from astro_mine.hub.registry import Blob, Registry
from astro_mine.hub.supply_chain import generate_keypair
from astro_mine.studio.hub.catalog import MEDIA_GEOMETRY_GLTF, MEDIA_SADF_JSON

#: This repository's root — the seeder resolves ``embargo/`` and the sample replay against it.
REPO = Path(__file__).resolve().parents[1]

#: The scenario every seeded submission is scored on: the lunar polar water-ice anchor.
SCENARIO_ID = "lunar-polar-ice-prospecting-v1"

#: The committed MCAP the seeded leaderboard's replay is attached from — the same episode log the
#: Bench route tests decode, so the replay a reader plays is one the platform really produced.
SAMPLE_REPLAY = REPO / "tests" / "bench" / "data" / f"anchor-{SCENARIO_ID}.mcap"

#: The bundle media type a world's ``bundle_media_type`` attribute names, so the materializer picks
#: the right layer without pulling every layer's bytes to find it.
MEDIA_WORLD_BUNDLE = "application/vnd.astro-mine.world.bundle.v1+tar"

#: Written at the end of a successful run; its presence is what makes a re-run a no-op.
SEED_MANIFEST = "seed.json"


# --- the content ------------------------------------------------------------------------------
#
# Deliberately small and deliberately plural. Two versions of the policy so a version *range* has
# something to resolve between (a single version makes `/hub/resolve` look like it works when all it
# did was echo); three assets with different capability tags so the asset menu's tag filter has
# something to filter; one world so terrain has a front door.

ASSETS: tuple[dict[str, Any], ...] = (
    {
        "id": "astro-mine.fleet.prospecting-rover",
        "kind": "rover",
        "name": "Prospecting Rover (demo stand-in)",
        "tags": [
            CapabilityTag.MOBILITY_WHEELED,
            CapabilityTag.PROSPECTING_NEUTRON,
            CapabilityTag.SENSING_IMAGING,
            CapabilityTag.POWER_STORAGE,
        ],
    },
    {
        "id": "astro-mine.fleet.excavator",
        "kind": "excavator",
        "name": "Excavator (demo stand-in)",
        "tags": [
            CapabilityTag.MOBILITY_TRACKED,
            CapabilityTag.EXCAVATION_BUCKET,
            CapabilityTag.POWER_STORAGE,
        ],
    },
    {
        "id": "astro-mine.fleet.hauler",
        "kind": "hauler",
        "name": "Hauler (demo stand-in)",
        "tags": [
            CapabilityTag.MOBILITY_WHEELED,
            CapabilityTag.RETURN_BULK_HAULER,
            CapabilityTag.POWER_STORAGE,
        ],
    },
)

POLICY_ID = "astro-mine.mind.lawnmower-survey"
POLICY_VERSIONS = ("0.1.0", "0.2.0")

#: Every Core interface the anchor scenario pins, declared on the published policy so the artifact
#: is a well-formed submission candidate.
#:
#: **No seeded board row carries a provenance bundle, and that is a platform defect rather than a
#: shortcut.** Only Bench's Hub-digest intake produces one (``Submission.provenance_hash`` is
#: ``None`` for the ``policy_ref`` path by design), and that intake cannot read anything this
#: platform publishes: ``bench.leaderboard._hub.resolve_submission`` parses the OCI config with
#: ``load_manifest``, which requires a ``ManifestDocument`` envelope, while ``HubClient.publish``
#: and the CLI both write a bare ``PluginManifest`` — as do the five other readers across Hub,
#: Fleet, Prospect, Surrogate and Studio. Publishing the envelope instead would satisfy Bench and
#: break ``catalog_from_registry`` for every artifact in the same registry. Filed as
#: astro-mine-platform#14; until it is fixed the front end's provenance view opens onto its own
#: honest "no bundle stored" explanation, which is a state worth having a test for either way.
POLICY_INTERFACES = {
    "env": "0.1.0",
    "messages": "0.1.0",
    "policy": "0.1.0",
    "sadf": "0.1.0",
    "objective": "0.1.0",
    "resource_field": "0.1.0",
    "world_provider": "0.1.0",
    "registry": "0.1.0",
}

WORLD_ID = "shackleton-demo-v1"
WORLD_NAME = "Shackleton rim (demo stand-in)"

#: Shackleton's rim, near enough. Real coordinates on a synthetic bundle: the anchor is what places
#: a design-time swarm, so a wrong one would put robots in the wrong hemisphere while looking fine.
WORLD_CRS = {"body": "MOON", "body_fixed_frame": "MOON_ME", "reference_radius_m": 1737400.0}
WORLD_ANCHOR = {
    "frame": "MOON_ME",
    "origin": {"latitude_deg": -89.66, "longitude_deg": 129.2, "height_m": 0.0},
}


def _minimal_gltf(name: str) -> bytes:
    """A valid, empty glTF 2.0 document — geometry the preview can fetch and render as nothing.

    The alternative was shipping no geometry at all, which makes the preview route 404 and hides
    whether it works. An empty scene exercises the whole path (resolve → verify → materialize →
    serve) and draws an honest nothing.
    """
    return json.dumps(
        {
            "asset": {"version": "2.0", "generator": f"astro-mine seed_demo ({name})"},
            "scene": 0,
            "scenes": [{"nodes": []}],
            "nodes": [],
        }
    ).encode()


def _sadf(asset_id: str, kind: str, name: str, geometry_uri: str) -> bytes:
    """The SADF-JSON layer an asset carries.

    Studio treats this as **opaque** — it copies the bytes and never parses them — so the shape only
    has to satisfy the front end's asset preview, which resolves ``geometry.uri`` relative to the
    served document.
    """
    return json.dumps(
        {
            "asset": {
                "identity": {"id": asset_id, "kind": kind, "name": name},
                "geometry": {"uri": geometry_uri},
            }
        }
    ).encode()


def _world_bundle() -> bytes:
    """The world bundle tar: ``world.json`` plus a 3D-Tiles tileset with no tiles.

    The materializer unpacks exactly this and the front end fetches ``world.json`` from the served
    cache, so the two files below are the whole contract. An empty tileset is a tileset: the viewer
    mounts, resolves the anchor, and draws no terrain — which is the truth about this bundle.
    """
    world = {
        "world_id": WORLD_ID,
        "name": WORLD_NAME,
        "description": (
            "A synthetic stand-in for the Shackleton polar bundle: a real CRS and a real tileset "
            "anchor, and no terrain. Seeded by scripts/seed_demo.py."
        ),
        "crs": WORLD_CRS,
        "tiles_anchor": WORLD_ANCHOR,
        "tileset": "tileset.json",
    }
    tileset = {
        "asset": {"version": "1.1"},
        "geometricError": 0.0,
        "root": {
            "boundingVolume": {"sphere": [0.0, 0.0, 0.0, 1.0]},
            "geometricError": 0.0,
            "refine": "REPLACE",
            "children": [],
        },
    }

    buffer = io.BytesIO()
    # `w` (uncompressed) rather than `w:gz`: the materializer opens the payload with a bare
    # `tarfile.open`, which sniffs compression, and an uncompressed tar of two small JSON documents
    # is smaller than the gzip framing would save.
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for filename, document in (("world.json", world), ("tileset.json", tileset)):
            data = json.dumps(document, indent=2).encode()
            info = tarfile.TarInfo(filename)
            info.size = len(data)
            # A fixed mtime, so the same content produces the same bytes and therefore the same
            # digest on every run. Without it the world's content address moves every seed, and
            # "idempotent" would be a claim rather than a property.
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _manifest(
    *,
    name: str,
    version: str,
    kind: PluginKind,
    description: str,
    tags: list[CapabilityTag] | None = None,
    attributes: dict[str, Any] | None = None,
    interfaces: dict[str, str] | None = None,
    digest: str,
    source_hashes: dict[str, str] | None = None,
) -> PluginManifest:
    """A Core plugin manifest for one seeded artifact."""
    return PluginManifest(
        name=name,
        version=version,
        kind=kind,
        core_interfaces=dict(interfaces or {}),
        capability_tags=list(tags or []),
        license="Apache-2.0",
        description=description,
        attributes=dict(attributes or {}),
        provenance=Provenance(digest=digest, source_content_hashes=dict(source_hashes or {})),
    )


# --- the steps --------------------------------------------------------------------------------


def ensure_keypair(registry_root: Path) -> tuple[bytes, bytes]:
    """The registry's signing keypair, under ``<registry>/keys/`` where the Studio surface looks.

    ``cosign.key``/``cosign.pub`` are the first names ``resolve_key`` tries, so a deployment pointed
    at this registry finds them with no further configuration. Generated once and reused: a new key
    on every run would orphan every signature already in the registry.
    """
    keys = registry_root / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    private_path, public_path = keys / "cosign.key", keys / "cosign.pub"

    if private_path.is_file() and public_path.is_file():
        return private_path.read_bytes(), public_path.read_bytes()

    private_pem, public_pem = generate_keypair()
    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)
    # The private half signs everything in this registry. It is a throwaway demo key, and it is
    # still a private key: 0600 so a shared machine does not hand it around.
    private_path.chmod(0o600)
    return private_pem, public_pem


def publish_content(client: HubClient, private_pem: bytes) -> dict[str, list[str]]:
    """Publish the demo content set, skipping whatever the registry already holds.

    Every publish goes through :meth:`HubClient.publish`, which stores the bytes, signs and attests
    them, and admits them fail-closed into the catalog. That is the same gate the ``POST
    /hub/publish`` route uses, so nothing here can be indexed that the route would have refused.
    """
    registry = client.registry
    existing = set(registry.references())
    published: dict[str, list[str]] = {"assets": [], "worlds": [], "policies": []}

    def already(reference: str) -> bool:
        return reference in existing

    for asset in ASSETS:
        reference = f"{asset['id']}:0.1.0"
        published["assets"].append(reference)
        if already(reference):
            continue
        geometry_uri = f"{asset['kind']}.gltf"
        geometry = _minimal_gltf(asset["kind"])
        sadf = _sadf(asset["id"], asset["kind"], asset["name"], geometry_uri)
        client.publish(
            name=asset["id"],
            version="0.1.0",
            kind="asset",
            manifest=_manifest(
                name=asset["id"],
                version="0.1.0",
                kind=PluginKind.ASSET,
                description=f"{asset['name']} — seeded demo content, not a flight asset.",
                tags=asset["tags"],
                attributes={"asset_kind": asset["kind"], "asset_name": asset["name"]},
                digest=_sha256(sadf),
                source_hashes={geometry_uri: _sha256(geometry)},
            ),
            layers=[Blob(MEDIA_SADF_JSON, sadf), Blob(MEDIA_GEOMETRY_GLTF, geometry)],
            private_key_pem=private_pem,
            publisher="astro-mine-demo",
        )

    world_reference = f"{WORLD_ID}:0.1.0"
    published["worlds"].append(world_reference)
    if not already(world_reference):
        bundle = _world_bundle()
        client.publish(
            name=WORLD_ID,
            version="0.1.0",
            kind="world",
            manifest=_manifest(
                name=WORLD_ID,
                version="0.1.0",
                kind=PluginKind.WORLD_PROVIDER,
                description=(
                    "A synthetic stand-in for the Shackleton polar bundle: a real CRS and tileset "
                    "anchor, no terrain. Seeded demo content."
                ),
                attributes={
                    "world_id": WORLD_ID,
                    "world_name": WORLD_NAME,
                    "body": "MOON",
                    "bundle_media_type": MEDIA_WORLD_BUNDLE,
                },
                digest=_sha256(bundle),
            ),
            layers=[Blob(MEDIA_WORLD_BUNDLE, bundle)],
            private_key_pem=private_pem,
            publisher="astro-mine-demo",
        )

    for version in POLICY_VERSIONS:
        reference = f"{POLICY_ID}:{version}"
        published["policies"].append(reference)
        if already(reference):
            continue
        body = json.dumps({"policy": POLICY_ID, "version": version}).encode()
        client.publish(
            name=POLICY_ID,
            version=version,
            kind="policy",
            manifest=_manifest(
                name=POLICY_ID,
                version=version,
                kind=PluginKind.POLICY,
                description=(
                    "A lawnmower survey pattern over a prospecting region — seeded demo content, "
                    "published twice so a version range has something to resolve between."
                ),
                tags=[CapabilityTag.PROSPECTING_NEUTRON],
                interfaces=POLICY_INTERFACES,
                attributes={"entrypoint": f"{POLICY_MODULE}:survey_sweep"},
                digest=_sha256(body),
            ),
            layers=[Blob("application/json", body)],
            private_key_pem=private_pem,
            publisher="astro-mine-demo",
        )

    return published


def _sha256(data: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def seed_studio(registry_root: Path, cache_dir: Path) -> str | None:
    """Publish the Studio example campaign, through the deployment's own seam wiring.

    ``ensure_example_seeded`` is the platform's, and it is idempotent by *content*: a campaign
    pinned before a model field became required is re-authored rather than reused. Wiring it
    through :func:`astro_mine_api.studio.serve._wire_hub_seams` rather than by hand means the
    seeded campaign is published by exactly the composition the ``/studio`` surface reads it with.
    """
    from astro_mine.studio.seed import ensure_example_seeded

    from astro_mine_api.studio.serve import (
        DEFAULT_SIGNING_KEY_NAMES,
        DEFAULT_TRUSTED_KEY_NAMES,
        SIGNING_KEY_ENV,
        TRUSTED_KEY_ENV,
        _wire_hub_seams,
        resolve_key,
    )

    kwargs, _seams = _wire_hub_seams(
        registry_root,
        resolve_key(None, TRUSTED_KEY_ENV, registry_root, DEFAULT_TRUSTED_KEY_NAMES),
        resolve_key(None, SIGNING_KEY_ENV, registry_root, DEFAULT_SIGNING_KEY_NAMES),
        cache_dir,
    )
    publisher = kwargs.get("publisher")
    if publisher is None:
        return None
    return ensure_example_seeded(publisher)  # type: ignore[arg-type]


# --- the "IdP" --------------------------------------------------------------------------------


def ensure_idp(root: Path) -> tuple[bytes, dict[str, Any]]:
    """An RSA keypair and its JWKS: the deployment's identity provider, minus the provider.

    Bench verifies bearer tokens against an issuer's **public** JWKS over HTTP, and accepts only
    asymmetric signatures — so a demo needs a real key, not a shared secret. The private half stays
    in ``root`` so tokens can be re-minted on a later run; the public half is written as a JWKS for
    the caller to serve at whatever URL it configures.

    The key is generated per root and never committed (``conventions.md`` §9: no secrets in
    repositories or images). Deleting ``root`` deletes the identity, which is the intent.
    """
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_path, jwks_path = root / "oidc-signing-key.pem", root / "jwks.json"
    if private_path.is_file() and jwks_path.is_file():
        return private_path.read_bytes(), json.loads(jwks_path.read_text(encoding="utf-8"))

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KEY_ID, "use": "sig", "alg": "RS256"})
    jwks = {"keys": [jwk]}

    private_path.write_bytes(private_pem)
    private_path.chmod(0o600)
    jwks_path.write_text(json.dumps(jwks, indent=2) + "\n", encoding="utf-8")
    return private_pem, jwks


#: The key id both the JWKS and every minted token carry, so the verifier can select on it.
KEY_ID = "astro-mine-demo-key"


def mint(
    private_pem: bytes,
    *,
    issuer: str,
    audience: str,
    subject: str,
    roles: tuple[str, ...],
    ttl_seconds: int,
) -> str:
    """Mint an RS256 bearer token. Minted fresh every run — an expired token is worse than none."""
    import jwt

    now = int(time.time())
    return jwt.encode(
        {
            "sub": subject,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now - 1,
            "exp": now + ttl_seconds,
            "roles": list(roles),
            "scope": "openid profile",
            "email": f"{subject}@astro-mine.invalid",
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )


class _JwksHandler(BaseHTTPRequestHandler):
    """Serves one document at any path: the JWKS, for the seeder's own submission calls."""

    payload: bytes = b"{}"

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's spelling, not ours
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log — this server exists for a handful of requests."""


# --- Bench ------------------------------------------------------------------------------------


def point_embargo_at_repo() -> None:
    """Make the held-out seed lookup resolve against this repository's ``embargo/``.

    ``astro_mine.bench.leaderboard._eval.EMBARGO_ROOT`` is computed from the *module's* location —
    correct when the leaderboard and the seeds shared a checkout, wrong now that the library arrives
    as an installed wheel and the seeds ship here. ``load_heldout_seeds`` binds it as a **keyword
    default at import time**, so rebinding the module attribute alone changes nothing; the default
    on the function object has to move too.

    This is the same redirection ``tests/bench/conftest.py`` performs, and the same **deployment
    gap** it names: a hosted leaderboard installed from wheels has the identical broken lookup. The
    fix belongs to the platform, which owns ``_eval``; until then, anything in this repository that
    scores a submission has to do this.
    """
    from astro_mine.bench.leaderboard import _eval

    embargo = REPO / "embargo"
    _eval.EMBARGO_ROOT = embargo
    kwdefaults = _eval.load_heldout_seeds.__kwdefaults__
    if kwdefaults is None or "embargo_root" not in kwdefaults:
        raise SystemExit(
            "load_heldout_seeds no longer takes embargo_root as a keyword default — the platform "
            "changed the seam this redirection depends on; re-read "
            "astro_mine.bench.leaderboard._eval (and tests/bench/conftest.py, which asserts the "
            "same thing)"
        )
    kwdefaults["embargo_root"] = embargo


#: The demo policy module the seeder writes and the sandboxed evaluator imports.
#:
#: Written rather than referenced because the platform ships exactly one importable baseline, and a
#: leaderboard with one row is a ranking that never had to sort anything. These are thin, honest
#: variations on that baseline — different working modes, therefore different scores — and they live
#: on disk under the seed root so the sandbox reaches them through
#: ``ASTRO_MINE_BENCH_SANDBOX_PYTHONPATH``, the same seam a deployment grants a real submission.
POLICY_MODULE = "astro_mine_demo_policies"

POLICY_SOURCE = '''\
"""Demo policies for a seeded leaderboard (astro-mine-api scripts/seed_demo.py).

**Not reference implementations and not baselines to beat.** Each is `BaselinePolicy` held in a
different working mode, which is enough to produce distinct scorecards and nothing more. They exist
so a seeded leaderboard has more than one row to rank.

Written to the seed root rather than shipped in the wheel: a package that offers importable
submissions invites them to be mistaken for a starting point.
"""

from astro_mine.bench.baseline import BaselinePolicy


def survey_sweep() -> BaselinePolicy:
    """Hold every agent in the prospecting mode — the platform's own default."""
    return BaselinePolicy(mode="prospect")


def idle_hold() -> BaselinePolicy:
    """Hold every agent idle. Scores worse, which is the point: a board needs an order."""
    return BaselinePolicy(mode="idle")


def transit_hold() -> BaselinePolicy:
    """Hold every agent in transit. Left unsubmitted by the seeder, for a reader to submit."""
    return BaselinePolicy(mode="transit")
'''

#: The two the seeder submits. ``transit_hold`` is deliberately left out: the front end's submit
#: journey needs a policy that is *not* already on the board, or it drives a form and proves nothing
#: — an idempotent re-submission returns the existing entry and looks identical to a success.
SUBMISSIONS: tuple[dict[str, str], ...] = (
    {
        "policy_ref": f"{POLICY_MODULE}:survey_sweep",
        "method": "Survey sweep (demo)",
        "author": "demo-lab",
    },
    {
        "policy_ref": f"{POLICY_MODULE}:idle_hold",
        "method": "Idle hold (demo)",
        "author": "demo-lab",
    },
)

#: The one a reader — or the front end's journey — submits themselves.
UNSUBMITTED_POLICY_REF = f"{POLICY_MODULE}:transit_hold"


def write_policies(root: Path) -> Path:
    """Write the demo policy module and return the import root the sandbox must be granted."""
    policies = root / "policies"
    policies.mkdir(parents=True, exist_ok=True)
    (policies / f"{POLICY_MODULE}.py").write_text(POLICY_SOURCE, encoding="utf-8")
    return policies


def seed_bench(token: str, *, jwks: dict[str, Any]) -> list[dict[str, Any]]:
    """Submit through the real route, then attach a replay to the first entry.

    The environment must already name the store, the object store and the OIDC settings — this
    builds the deployment's own app from it and drives it in-process, so the seeded rows are
    produced by the same handler, the same authorization policy and the same sandboxed evaluator a
    live deployment would use.

    The JWKS is served on an ephemeral local port for the duration: the verifier fetches it over
    HTTP by design, and the port the *deployment* will later use is the caller's business, not the
    seeder's.
    """
    from starlette.testclient import TestClient

    from astro_mine_api._app import build_app

    _JwksHandler.payload = json.dumps(jwks).encode()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    os.environ["ASTRO_MINE_BENCH_OIDC_JWKS_URL"] = (
        f"http://127.0.0.1:{server.server_address[1]}/jwks.json"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        client = TestClient(build_app(["bench"]))
        headers = {"Authorization": f"Bearer {token}"}
        seeded: list[dict[str, Any]] = []

        existing = {
            entry["submission_id"]
            for entry in client.get(f"/bench/leaderboard/{SCENARIO_ID}").json()
        }

        for request in SUBMISSIONS:
            response = client.post(
                "/bench/submissions",
                json={"scenario_id": SCENARIO_ID, **request},
                headers=headers,
            )
            if response.status_code != 200:
                raise SystemExit(
                    f"seeding a submission failed with {response.status_code}: {response.text}"
                )
            submission = response.json()
            seeded.append(
                {
                    "submission_id": submission["submission_id"],
                    "method": submission["method"],
                    "intake": "policy_ref",
                    "integrity": submission["integrity"],
                    "already_present": submission["submission_id"] in existing,
                }
            )

        # Every seeded row gets the replay, so whichever one a reader opens has an episode to play.
        # A board where only one row does is realistic and makes a browser test depend on the
        # ranking staying put, which is a dependency worth not having.
        for entry in seeded:
            _attach_replay(entry["submission_id"])
            entry["replay"] = SAMPLE_REPLAY.name
        return seeded
    finally:
        server.shutdown()
        server.server_close()


def _attach_replay(submission_id: str) -> None:
    """Attach the committed sample MCAP to a seeded entry — the one step with no REST route.

    ``attach_replay`` is the producer seam: Bench attaches a recording during evaluation, and there
    is deliberately no endpoint for uploading one, because a leaderboard that accepts arbitrary
    episode logs from callers is a leaderboard whose replays prove nothing. Here the "producer" is
    the seeder, and it says so rather than pretending an API did it.
    """
    if not SAMPLE_REPLAY.is_file():
        raise SystemExit(f"the sample replay is missing: {SAMPLE_REPLAY}")

    from astro_mine.bench.leaderboard._objects import FileObjectStore
    from astro_mine.bench.leaderboard._service import LeaderboardService
    from astro_mine.bench.leaderboard._sql import SqlStore

    service = LeaderboardService(
        store=SqlStore(os.environ["ASTRO_MINE_BENCH_DB"]),
        object_store=FileObjectStore(os.environ["ASTRO_MINE_BENCH_OBJECTS"]),
    )
    service.attach_replay(submission_id, SAMPLE_REPLAY.read_bytes())


# --- composition ------------------------------------------------------------------------------


def environment(
    root: Path, *, cors_origins: str, jwks_url: str, issuer: str, audience: str
) -> dict[str, str]:
    """The environment a deployment must carry to read what this seeded.

    ``HUB_POSTGRES_URL`` names a **file**-backed SQLite database, because the seeder and the server
    are different processes. The deployment default is in-memory and process-lifetime — which is a
    fine default and useless here: an index written by this process would not exist in the one that
    serves it. Anything a seeder writes has to outlive the seeder.

    ``ASTRO_MINE_BENCH_SANDBOX_PYTHONPATH`` grants the evaluation worker the seed's own policy
    directory. The sandbox scrubs the environment and confines the filesystem, so an import root a
    submission needs has to be granted explicitly — that is the seam working, not a hole in it.
    """
    return {
        "ASTRO_MINE_API_CORS_ORIGINS": cors_origins,
        "HUB_POSTGRES_URL": f"sqlite+pysqlite:///{root / 'hub-catalog.sqlite'}",
        "ASTRO_MINE_HUB_REGISTRY": str(root / "registry"),
        "ASTRO_MINE_STUDIO_CACHE": str(root / "cache"),
        "ASTRO_MINE_BENCH_DB": f"sqlite+pysqlite:///{root / 'bench.sqlite'}",
        "ASTRO_MINE_BENCH_OBJECTS": str(root / "objects"),
        "ASTRO_MINE_BENCH_SANDBOX_PYTHONPATH": str(root / "policies"),
        "ASTRO_MINE_BENCH_OIDC_ISSUER": issuer,
        "ASTRO_MINE_BENCH_OIDC_AUDIENCE": audience,
        "ASTRO_MINE_BENCH_OIDC_JWKS_URL": jwks_url,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(".demo"),
        help="directory to hold the registry, catalogs, object store and keys (default: .demo)",
    )
    parser.add_argument(
        "--cors-origins",
        default="http://127.0.0.1:4174,http://localhost:4174",
        help="origins the browser tier may call from (default: %(default)s)",
    )
    parser.add_argument(
        "--jwks-url",
        default="http://127.0.0.1:8081/jwks.json",
        help="where the deployment will fetch the JWKS this writes (default: %(default)s)",
    )
    parser.add_argument("--issuer", default="http://127.0.0.1:8081/idp")
    parser.add_argument("--audience", default="astro-mine-bench")
    parser.add_argument(
        "--token-ttl",
        type=int,
        default=8 * 60 * 60,
        help="bearer-token lifetime in seconds (default: 8 hours)",
    )
    parser.add_argument("--json", action="store_true", help="print only the JSON manifest")
    args = parser.parse_args(argv)

    root: Path = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    registry_root = root / "registry"
    cache_dir = root / "cache"
    for directory in (registry_root, cache_dir, root / "objects"):
        directory.mkdir(parents=True, exist_ok=True)

    env = environment(
        root,
        cors_origins=args.cors_origins,
        jwks_url=args.jwks_url,
        issuer=args.issuer,
        audience=args.audience,
    )
    os.environ.update(env)

    def say(message: str) -> None:
        if not args.json:
            print(message)

    say(f"seeding {root}")

    private_pem, _public_pem = ensure_keypair(registry_root)
    registry = Registry(registry_root)
    catalog = sql_catalog(env["HUB_POSTGRES_URL"])
    client = HubClient(registry, catalog=catalog)

    published = publish_content(client, private_pem)
    say(
        f"  registry: {len(published['assets'])} assets, {len(published['worlds'])} world, "
        f"{len(published['policies'])} policy versions"
    )

    campaign = seed_studio(registry_root, cache_dir)
    say(f"  studio:   {campaign or 'not seeded (no publisher wired)'}")

    write_policies(root)
    oidc_private, jwks = ensure_idp(root)
    token = mint(
        oidc_private,
        issuer=args.issuer,
        audience=args.audience,
        subject="demo-steward",
        # One token carrying both roles, because the demo has one person in it. A deployment with
        # real users would mint one per person; splitting them here would only make the fixture
        # harder to drive without making it more honest.
        roles=("submitter", "admin"),
        ttl_seconds=args.token_ttl,
    )

    point_embargo_at_repo()
    submissions = seed_bench(token, jwks=jwks)
    say(f"  bench:    {len(submissions)} submissions on {SCENARIO_ID}")

    manifest = {
        "root": str(root),
        # The JWKS URL in `env` is the one the *deployment* will use; the seeder's own ephemeral
        # server is gone by now, so restore it rather than leaking a dead port into the manifest.
        "env": {**env, "ASTRO_MINE_BENCH_OIDC_JWKS_URL": args.jwks_url},
        "jwks_path": str(root / "jwks.json"),
        "hub": published,
        "studio": {"campaign": campaign},
        "bench": {
            "scenario_id": SCENARIO_ID,
            "submissions": submissions,
            "unsubmitted_policy_ref": UNSUBMITTED_POLICY_REF,
        },
        "oidc": {"issuer": args.issuer, "audience": args.audience, "token": token},
    }
    (root / SEED_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(f"\nwrote {root / SEED_MANIFEST}")
        print("\nexport the environment, then serve the JWKS and the API:\n")
        for key, value in manifest["env"].items():  # type: ignore[union-attr]
            print(f"  export {key}={value!r}")
        print(f"\n  python -m http.server 8081 --directory {root}   # serves jwks.json")
        print("  uv run uvicorn --factory astro_mine_api._app:make_app --port 8000")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
