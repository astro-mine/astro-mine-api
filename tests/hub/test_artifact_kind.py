"""Hub's container vocabulary — what it is, and that it survives to the index (hub#33).

``ARTIFACT_KINDS`` was a second artifact vocabulary that *asserted in code* it was not one, and was
then discarded at ingest: it reached the OCI media type and died there, so no read path above the
registry could see it. Hub paid for a parallel closed set — a governance claim it could not honour,
a junk-drawer member — and captured none of the benefit.

These pin the resolution: the vocabulary is Hub's own **container** axis (payload shape), the Core
manifest kind is the **interface** axis, an entry carries both, and the two never collapse into one
field. The docstring guard exists because the previous docstrings were false *when written* — a
claim about a vocabulary is worth exactly as much as a test that re-reads the vocabulary.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from astro_mine.core.registry import PluginKind
from astro_mine.hub.client import HubClient
from astro_mine.hub.index import InMemoryCatalog
from astro_mine.hub.index._sql import SqlCatalog
from astro_mine.hub.registry import ARTIFACT_KINDS, Registry, artifact_kind_of, artifact_media_type
from astro_mine.hub.search import SearchQuery, search
from astro_mine.hub.supply_chain import generate_keypair, stored_artifact_kind

from .conftest import make_manifest

# The four names the two vocabularies share; everything else is one or the other.
_SHARED = {"asset", "campaign", "design", "policy"}
_HUB_ONLY = {"plugin", "schema", "surrogate", "world"}


def _published(tmp_path: Path, *, kind: str = "world", core_kind: PluginKind | None = None):
    """A genuinely admitted artifact whose container kind and interface kind differ."""
    registry = Registry(tmp_path / "reg")
    client = HubClient(registry)
    private_pem, _ = generate_keypair()
    manifest = make_manifest("art", "1.0.0")
    if core_kind is not None:
        manifest = manifest.model_copy(update={"kind": core_kind})
    client.publish(
        name="art", version="1.0.0", kind=kind, manifest=manifest, private_key_pem=private_pem
    )
    return registry, client.catalog


# --- the vocabulary is Hub's own, and says so ----------------------------------------------


def test_the_two_vocabularies_genuinely_diverge() -> None:
    """The premise. If these ever coincided, the container axis would be redundant."""
    core = {k.value for k in PluginKind}
    hub = set(ARTIFACT_KINDS)
    assert hub & core == _SHARED
    assert hub - core == _HUB_ONLY
    assert core - hub  # a dozen Core interfaces describe no distinct container


def test_no_docstring_claims_the_tuple_tracks_core() -> None:
    """The guard. The previous docstrings said Hub "stores only the kinds Core describes" and
    "a new kind is a Core RFC, not a Hub extension" — both false for half the tuple, and false when
    they were written. A vocabulary claim needs a test that re-reads the vocabulary."""
    from astro_mine.hub.registry import _oci

    source = Path(_oci.__file__).read_text(encoding="utf-8")
    for claim in (
        "stores only the kinds Core describes",
        "a new kind is a Core RFC",
        "grows only when Core's does",
    ):
        assert claim not in source, f"docstring still claims Core-tracking: {claim!r}"


def test_media_types_round_trip_through_the_kind() -> None:
    for kind in ARTIFACT_KINDS:
        assert artifact_kind_of(artifact_media_type(kind)) == kind


def test_a_foreign_media_type_yields_no_container_kind() -> None:
    """An artifact pushed by another OCI tool is still storable — it just has no Hub kind."""
    assert artifact_kind_of(None) is None
    assert artifact_kind_of("application/vnd.oci.image.manifest.v1+json") is None
    assert artifact_kind_of("application/vnd.astro-mine.not-a-kind.v1") is None


# --- it survives to the index --------------------------------------------------------------


def test_an_entry_carries_both_axes_distinctly(tmp_path: Path) -> None:
    """The round trip hub#33 asks for: publish a `world` container whose manifest declares
    `world_provider`, and the catalog reports **both**, in separate fields."""
    _, catalog = _published(tmp_path, kind="world", core_kind=PluginKind.WORLD_PROVIDER)
    entry = catalog.get("art:1.0.0")
    assert entry is not None
    assert entry.artifact_kind == "world"  # the container
    assert entry.kind == "world_provider"  # the Core interface
    assert entry.artifact_kind != entry.kind  # never one field carrying two vocabularies


def test_the_container_kind_is_read_from_the_stored_bytes(tmp_path: Path) -> None:
    """Derived at admission, not accepted from a caller — it cannot drift from the artifact."""
    registry, catalog = _published(tmp_path, kind="surrogate")
    entry = catalog.get("art:1.0.0")
    assert entry is not None
    assert stored_artifact_kind(registry, entry.digest) == "surrogate"
    assert entry.artifact_kind == "surrogate"


def test_the_publish_endpoint_cannot_be_told_a_container_kind(tmp_path: Path) -> None:
    """`POST /publish` has no artifact-kind field to supply — the value is re-derived."""
    from astro_mine_api.hub._app import PublishBody

    assert "artifact_kind" not in PublishBody.model_fields


def test_an_artifact_with_no_container_kind_still_indexes(tmp_path: Path) -> None:
    """Absence is not an error: the entry is indexed by its Core manifest kind regardless."""
    from astro_mine.hub.index import ingest

    catalog = InMemoryCatalog()
    ingest(catalog, make_manifest("art", "1.0.0"), digest="sha256:" + "a" * 64, publisher="p")
    entry = catalog.get("art:1.0.0")
    assert entry is not None
    assert entry.artifact_kind is None
    assert entry.kind  # the interface axis is unaffected


# --- and it is queryable, which is the whole point ------------------------------------------


def test_search_filters_on_the_container_axis(tmp_path: Path) -> None:
    _, catalog = _published(tmp_path, kind="world", core_kind=PluginKind.WORLD_PROVIDER)
    assert search(catalog, SearchQuery(artifact_kind="world"))
    assert search(catalog, SearchQuery(artifact_kind="policy")) == []


def test_the_two_filters_are_independent(tmp_path: Path) -> None:
    """Filtering on one axis must never imply the other — that is what having two axes means."""
    _, catalog = _published(tmp_path, kind="world", core_kind=PluginKind.WORLD_PROVIDER)
    assert search(catalog, SearchQuery(kind="world_provider", artifact_kind="world"))
    # The container is `world`, so a `world` *interface* filter matches nothing.
    assert search(catalog, SearchQuery(kind="world")) == []
    # And the interface is `world_provider`, which is not a container kind at all.
    assert search(catalog, SearchQuery(artifact_kind="world_provider")) == []


def test_the_facet_survives_the_sql_backend(tmp_path: Path) -> None:
    """It is a column, not just a JSON field — so it is indexable rather than write-only."""
    registry, _ = _published(tmp_path, kind="world", core_kind=PluginKind.WORLD_PROVIDER)
    sql = SqlCatalog(f"sqlite+pysqlite:///{tmp_path / 'catalog.db'}")
    try:
        client = HubClient(registry, catalog=sql)
        rebuilt = client.catalog.get("art:1.0.0")
        if rebuilt is None:  # the entry was indexed into the in-memory catalog of `_published`
            from astro_mine.hub.index import ingest

            ingest(
                sql,
                make_manifest("art", "1.0.0"),
                digest=registry.resolve("art:1.0.0").digest,
                publisher="p",
                artifact_kind="world",
            )
        entry = sql.get("art:1.0.0")
        assert entry is not None and entry.artifact_kind == "world"
        assert "artifact_kind" in {c.name for c in sql._catalog.columns}
    finally:
        sql.dispose()


def test_the_api_reports_both_axes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from astro_mine_api.hub import create_app

    registry, catalog = _published(tmp_path, kind="world", core_kind=PluginKind.WORLD_PROVIDER)
    client = TestClient(create_app(catalog, registry=registry))
    body = client.get("/hub/artifacts/art/1.0.0").json()
    assert body["artifact_kind"] == "world"
    assert body["kind"] == "world_provider"
    hits = client.get("/hub/search", params={"artifact_kind": "world"}).json()
    assert hits and hits[0]["artifact_kind"] == "world"


# --- the junk drawer is a decision, not an accident -----------------------------------------


@pytest.mark.parametrize("kind", sorted(_HUB_ONLY))
def test_the_hub_only_kinds_are_container_shapes_not_interfaces(kind: str) -> None:
    """`plugin` in particular: the deliberate generic container Link and Prospect both target,
    for payloads with no more specific shape. Documented as a choice rather than left as a
    fallback nobody made."""
    assert kind not in {k.value for k in PluginKind}
    assert artifact_media_type(kind).startswith("application/vnd.astro-mine.")
