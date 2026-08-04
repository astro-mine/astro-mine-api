"""The deployment's own wiring, driven the way it actually runs (api#15, api#16, api#17).

Every other test in this repository builds an app the way a *test* wants one — ``create_app(catalog,
registry=…)``, or a fully-injected ``LeaderboardService``. That is right for testing a route, and it
means the code that composes a real deployment from environment variables was exercised by nothing
at all. Three defects lived in that gap at once, and none of them was subtle:

- **api#15** — the Hub catalog's ``:memory:`` fallback gave every *thread* its own empty database,
  so ``GET /hub/search`` answered 500 on any deployment that set no ``HUB_POSTGRES_URL``.
- **api#16** — the Hub router was mounted with no registry, so every artifact reported
  ``attestations: []`` and ``POST /hub/publish`` refused with 503 unconditionally.
- **api#17** — the audit trail was never given the durable backing that already existed, so it was
  lost on every restart while the submissions it described persisted.

What they have in common is the thing this file exists to hold: **a route test cannot see a wiring
defect, because it supplies the wiring itself.** So everything here goes through
:func:`astro_mine_api._app.build_app` with an environment and nothing else, and — where the defect
was about persistence — through a *second* app over the same stores, because "it survived the
process" is not a property one process can assert.

These are cheap. Nothing here scores a submission or publishes real content; ``test_seed_demo.py``
does that, slowly, once.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from astro_mine.core.registry import PluginKind, PluginManifest, Provenance
from astro_mine.hub.client import HubClient
from astro_mine.hub.index import sql_catalog
from astro_mine.hub.registry import Registry
from astro_mine.hub.supply_chain import generate_keypair
from fastapi.testclient import TestClient

from astro_mine_api._app import IN_MEMORY_CATALOG_URL, build_app, hub_catalog_url


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A configured deployment's environment: a registry, a catalog and a submission store."""
    registry = tmp_path / "registry"
    registry.mkdir()
    monkeypatch.setenv("ASTRO_MINE_HUB_REGISTRY", str(registry))
    monkeypatch.setenv("HUB_POSTGRES_URL", f"sqlite+pysqlite:///{tmp_path / 'catalog.sqlite'}")
    monkeypatch.setenv("ASTRO_MINE_BENCH_DB", f"sqlite+pysqlite:///{tmp_path / 'bench.sqlite'}")
    monkeypatch.setenv("ASTRO_MINE_BENCH_OBJECTS", str(tmp_path / "objects"))
    yield tmp_path


def _publish_signed(registry_root: Path, catalog_url: str) -> tuple[str, str]:
    """Publish one signed artifact the way the platform publishes, and return its reference/digest.

    Through :class:`HubClient`, not by hand: the point of the attestation assertion below is that a
    deployment surfaces the evidence a *real* publish attaches, so a fixture that attached its own
    would be asserting against itself.
    """
    private_pem, _ = generate_keypair()
    manifest = PluginManifest(
        name="demo.policy",
        version="1.0.0",
        kind=PluginKind.POLICY,
        core_interfaces={"policy": "0.1.0"},
        license="Apache-2.0",
        provenance=Provenance(digest="sha256:" + "a" * 64),
    )
    client = HubClient(Registry(registry_root), catalog=sql_catalog(catalog_url))
    artifact = client.publish(
        name="demo.policy",
        version="1.0.0",
        kind="policy",
        manifest=manifest,
        private_key_pem=private_pem,
        publisher="tests",
    )
    return "demo.policy:1.0.0", artifact.digest


# --- api#15: the catalog fallback ----------------------------------------------------------------


def test_the_catalog_fallback_is_one_database_for_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that configures nothing can still be searched.

    ``TestClient`` serves sync routes on a worker thread, which is precisely what broke: the catalog
    was constructed on the main thread and queried on another. So this asserts a 200 rather than an
    absence of exceptions — the 500 was the symptom a reader met.
    """
    monkeypatch.delenv("HUB_POSTGRES_URL", raising=False)
    assert hub_catalog_url() == IN_MEMORY_CATALOG_URL

    client = TestClient(build_app(["hub"]))
    response = client.get("/hub/search", params={"text": "anything"})
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_an_explicit_catalog_url_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fallback is a fallback. A deployment that names a database gets that database."""
    url = f"sqlite+pysqlite:///{tmp_path / 'catalog.sqlite'}"
    monkeypatch.setenv("HUB_POSTGRES_URL", url)
    assert hub_catalog_url() == url


# --- api#16: the Hub registry --------------------------------------------------------------------


def test_a_configured_deployment_surfaces_an_artifact_s_attestations(deployment: Path) -> None:
    """The evidence is read off the registry, so with no registry there is no evidence to read.

    Attestations are OCI referrers, not index fields — which is why this failed while every other
    field on the same response was correct, and why nothing about the artifact detail looked wrong
    except that its supply chain appeared empty (`ui.md` §7 rule 6).
    """
    reference, _digest = _publish_signed(deployment / "registry", hub_catalog_url())

    client = TestClient(build_app(["hub"]))
    detail = client.get(f"/hub/artifacts/{reference.replace(':', '/')}").json()
    # The **OCI artifactType** of each referrer, spelled exactly as it is stored — not a short name.
    # Written out rather than matched loosely because these three strings are what a client renders,
    # and a client that recognises a different spelling of them shows an artifact with evidence as
    # an artifact with none.
    assert set(detail["attestations"]) == {
        "application/vnd.astro-mine.signature.v1",
        "application/vnd.astro-mine.provenance.slsa.v1",
        "application/vnd.astro-mine.sbom.cyclonedx.v1",
    }


def test_a_configured_deployment_can_publish(deployment: Path) -> None:
    """``POST /hub/publish`` refused with 503 on every deployment, because admission had nothing to
    verify against and correctly declined to index on a caller's word."""
    registry_root = deployment / "registry"
    reference, digest = _publish_signed(registry_root, hub_catalog_url())
    manifest = json.loads(Registry(registry_root).read_config(digest))

    client = TestClient(build_app(["hub"]))
    response = client.post(
        "/hub/publish",
        json={"digest": digest, "manifest": manifest, "publisher": "tests", "namespace": "open"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["reference"] == reference


def test_publishing_still_refuses_when_no_registry_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix must not become a fallback that indexes on trust.

    With no registry the refusal is the *correct* answer, and it has to stay correct: admission
    re-derives every claim from stored bytes, so with no bytes there is nothing to re-derive.
    """
    monkeypatch.delenv("ASTRO_MINE_HUB_REGISTRY", raising=False)
    monkeypatch.delenv("HUB_POSTGRES_URL", raising=False)

    client = TestClient(build_app(["hub"]))
    response = client.post(
        "/hub/publish",
        json={
            "digest": "sha256:" + "a" * 64,
            "manifest": {},
            "publisher": "tests",
            "namespace": "open",
        },
    )
    assert response.status_code == 503
    assert response.json()["code"] == "publish_unconfigured"


# --- api#17: the durable audit trail -------------------------------------------------------------


def _refused_write(client: TestClient) -> None:
    """Make the deployment record something: an unauthenticated write is an audited denial."""
    response = client.delete(f"/bench/submissions/{'sha256:' + 'b' * 64}")
    assert response.status_code in {401, 503}


def test_the_audit_trail_outlives_the_process(deployment: Path) -> None:
    """Written by one app, read by another, over the same database.

    Two apps rather than two requests, because that is the property: an ``InMemoryAuditLog`` passes
    any assertion made inside the process that wrote it. It fails this one.

    The trail is read through the library rather than through ``GET /bench/audit``, which needs an
    OIDC deployment and a JWKS server to answer — that path is covered end to end in
    ``test_seed_demo.py``. What is under test here is persistence, not authentication.
    """
    from astro_mine.bench.leaderboard._sql import SqlAuditLog

    _refused_write(TestClient(build_app(["bench"])))

    written = SqlAuditLog(f"sqlite+pysqlite:///{deployment / 'bench.sqlite'}").query()
    assert written, "the refusal should have been recorded durably"

    # ...and a second app reads the same trail rather than starting an empty one.
    _refused_write(TestClient(build_app(["bench"])))
    assert len(SqlAuditLog(f"sqlite+pysqlite:///{deployment / 'bench.sqlite'}").query()) > len(
        written
    )


def test_the_audit_trail_stays_process_local_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No database configured ⇒ no database invented.

    The rule every backend in ``_default_service`` follows: durable if configured, process-local
    otherwise. A deployment that asked for neither must not find a SQLite file appearing beside it.
    """
    from astro_mine.bench.leaderboard._audit import InMemoryAuditLog

    monkeypatch.delenv("ASTRO_MINE_BENCH_DB", raising=False)

    from astro_mine_api.bench._app import _default_audit

    assert isinstance(_default_audit(), InMemoryAuditLog)


# --- the composition as a whole ------------------------------------------------------------------


def test_every_surface_answers_on_a_deployment_that_configures_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor: an unconfigured deployment is degraded, never broken.

    Each surface may have nothing to serve — an empty catalog, no registry, no IdP — and each must
    still answer. A 500 from an unconfigured deployment is a defect; an empty list, or a 503 that
    names what is missing, is the design (`api.md` §6).
    """
    for variable in (
        "HUB_POSTGRES_URL",
        "ASTRO_MINE_HUB_REGISTRY",
        "ASTRO_MINE_BENCH_DB",
        "ASTRO_MINE_BENCH_OBJECTS",
        "ASTRO_MINE_BENCH_OIDC_ISSUER",
        "ASTRO_MINE_BENCH_OIDC_AUDIENCE",
    ):
        monkeypatch.delenv(variable, raising=False)

    client = TestClient(build_app())
    health = client.get("/healthz").json()
    assert set(health["surfaces"]) == {"hub", "studio", "cloud", "bench"}

    reads: list[tuple[str, dict[str, Any]]] = [
        ("/hub/search", {"text": "x"}),
        ("/bench/scenarios", {}),
        ("/cloud/backends", {}),
    ]
    for path, params in reads:
        response = client.get(path, params=params)
        assert response.status_code == 200, f"{path}: {response.status_code} {response.text}"

    # Studio's Hub-backed routes are the honest exception: with no registry they 503 by design,
    # naming the seam that is unwired rather than pretending an empty catalog.
    assert client.get("/studio/catalog/assets").status_code == 503


def test_a_single_surface_deployment_imports_only_that_surface() -> None:
    """"Every surface is imported *only* if it is enabled" — asserted, because it is easy to lose.

    ``_mount`` reads environment-variable *names* as well as backends, and the obvious way to reach
    one another surface already spells is to import its module — which quietly drags that surface's
    whole package into a deployment that disabled it. That nearly happened while api#16 was being
    fixed: the Hub mount wanted ``ASTRO_MINE_HUB_REGISTRY``, and ``studio.serve`` had it.

    Run in a subprocess because the property is about what a **fresh** interpreter imports, and this
    test session has already imported all four.
    """
    import subprocess
    import sys

    probe = (
        "import sys;"
        "from astro_mine_api._app import build_app;"
        "build_app(['hub']);"
        "print([m for m in ('astro_mine_api.studio','astro_mine_api.bench','astro_mine_api.cloud')"
        " if m in sys.modules])"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=300
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip() == "[]", (
        f"a hub-only deployment imported {completed.stdout.strip()}"
    )
