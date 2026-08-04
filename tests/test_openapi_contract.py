"""The OpenAPI document as a contract, not as documentation (api#3).

The front end consumes this API through a **generated** client, so the document is the thing that
breaks a downstream build — not the handlers. These assert the properties a generator depends on:
readable unique operation ids, response types that are not `unknown`, and schema names that are not
module paths.

The snapshot at the end is the one that catches everything these do not think to check. It exists so
a renamed operation or a reshaped response arrives as a **visible diff in a pull request** rather
than as a silent break of a client nobody rebuilt yet.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from astro_mine.hub.index import InMemoryCatalog

from astro_mine_api._app import SURFACES_ENV
from astro_mine_api._openapi import VERSION_PLACEHOLDER, current_document
from astro_mine_api.bench._app import create_app as bench_app
from astro_mine_api.cloud.app import create_app as cloud_app
from astro_mine_api.hub._app import create_app as hub_app
from astro_mine_api.studio.app import create_app as studio_app

#: The committed contract. `scripts/update_openapi_snapshot.py` writes it; both call
#: `current_document`, so it cannot be generated one way and checked another.
SNAPSHOT = Path(__file__).resolve().parent / "openapi_snapshot.json"

#: The routes whose whole payload is genuinely open, each with the reason it is. **Adding to this
#: list is the point of the test**: a new untyped route fails until someone writes down why it
#: deserves to be here.
#:
#: It is empty. It held the three ``/cloud/*/compile`` operations, on the grounds that a compiled
#: manifest is an execution engine's own object — and that is still true of the ``spec`` inside one,
#: which is why ``CompiledManifest.spec`` is an open object. It was never true of the envelope
#: around it: ``apiVersion``/``kind``/``metadata`` is the same for every engine and is mostly
#: Astro-Mine's own writing, so declaring it costs nothing and lets a client name what it is holding
#: (api#12).
OPEN_PAYLOAD_OPERATIONS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _no_ambient_surface_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(SURFACES_ENV, raising=False)
    yield


@pytest.fixture
def document() -> dict[str, Any]:
    return current_document()


def _operations(document: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, operations in document["paths"].items():
        for verb, operation in operations.items():
            yield path, verb, operation


# --- operation ids ------------------------------------------------------------------------------


def test_every_operation_id_is_unique_across_the_mounted_surfaces(
    document: dict[str, Any],
) -> None:
    ids = [op["operationId"] for _, _, op in _operations(document)]
    duplicates = {name for name in ids if ids.count(name) > 1}
    assert not duplicates, f"a generated client cannot express duplicate methods: {duplicates}"
    # No expected count here on purpose: the snapshot below catches a route appearing or vanishing,
    # and with a diff worth reading. A magic number would fail twice for every new route and explain
    # nothing either time.
    assert ids


def test_no_operation_id_embeds_an_http_method(document: dict[str, Any]) -> None:
    """An id ending in ``_get`` renames a client method when a route changes verb."""
    offenders = [
        op["operationId"]
        for _, _, op in _operations(document)
        if re.search(r"_(get|post|put|patch|delete|head|options)$", op["operationId"])
    ]
    assert not offenders, offenders


def test_no_operation_id_embeds_a_path(document: dict[str, Any]) -> None:
    """FastAPI's default ids carry the path, so a URL change renames a client method."""
    offenders = [
        op["operationId"] for _, _, op in _operations(document) if "__" in op["operationId"]
    ]
    assert not offenders, offenders


def test_operation_ids_are_readable(document: dict[str, Any]) -> None:
    """Lower snake case, and named for the surface they belong to."""
    surfaces = ("hub_", "studio_", "cloud_", "bench_")
    for path, _, operation in _operations(document):
        name = operation["operationId"]
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), name
        if path != "/healthz":
            assert name.startswith(surfaces), f"{name} does not name its surface"


@pytest.mark.parametrize(
    ("operation_id", "path"),
    [
        ("hub_search", "/hub/search"),
        ("hub_get_artifact", "/hub/artifacts/{name}/{version}"),
        ("hub_resolve", "/hub/resolve"),
        ("bench_leaderboard_scorecards", "/bench/leaderboard/{scenario_id}/scorecards"),
        ("cloud_compile_sweep", "/cloud/sweeps/compile"),
    ],
)
def test_the_ids_the_issue_named(document: dict[str, Any], operation_id: str, path: str) -> None:
    """The examples the issue gives, asserted so a refactor cannot quietly regress them."""
    ids = {op["operationId"] for p, _, op in _operations(document) if p == path}
    assert operation_id in ids


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(bench_app, id="bench"),
        pytest.param(cloud_app, id="cloud"),
        pytest.param(lambda: hub_app(InMemoryCatalog()), id="hub"),
        pytest.param(studio_app, id="studio"),
    ],
)
def test_a_single_surface_deployment_gets_the_same_ids(factory: object) -> None:
    """A deployment serving one surface must not answer under different method names."""
    document = factory().openapi()  # type: ignore[operator]
    for _, _, operation in _operations(document):
        assert "__" not in operation["operationId"]
        assert not re.search(r"_(get|post|delete)$", operation["operationId"])


# --- response types -----------------------------------------------------------------------------


def _json_200(operation: dict[str, Any]) -> dict[str, Any] | None:
    try:
        schema: dict[str, Any] = operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
    except KeyError:
        return None
    return schema


def test_no_route_answers_an_unjustified_open_object(document: dict[str, Any]) -> None:
    """`additionalProperties: true` generates as `unknown` — a client that cannot be used."""
    offenders = []
    for _, _, operation in _operations(document):
        schema = _json_200(operation)
        if schema is None:
            continue
        items = schema.get("items", {}) if schema.get("type") == "array" else {}
        open_object = (
            schema.get("additionalProperties") is True or items.get("additionalProperties") is True
        )
        if open_object and operation["operationId"] not in OPEN_PAYLOAD_OPERATIONS:
            offenders.append(operation["operationId"])
    assert not offenders, (
        f"{offenders} answer an untyped object. Give each a response model, or add it to "
        "OPEN_PAYLOAD_OPERATIONS with the reason its payload is genuinely open."
    )


def test_the_open_payload_allowlist_is_still_accurate(document: dict[str, Any]) -> None:
    """An allowlist that outlives its entries is a hole, not a decision."""
    ids = {op["operationId"] for _, _, op in _operations(document)}
    assert set(OPEN_PAYLOAD_OPERATIONS) <= ids, "allowlisted operations that no longer exist"


@pytest.mark.parametrize(
    ("operation_id", "schema_name"),
    [
        ("hub_search", "SearchHit"),
        ("hub_publish", "SearchHit"),
        ("hub_get_artifact", "ArtifactDetail"),
        ("hub_resolve", "ResolveResult"),
        ("hub_download", "DownloadGrant"),
        ("cloud_expand_sweep", "SweepExpansion"),
        ("cloud_compile_job", "CompiledManifest"),
        ("cloud_compile_sweep", "CompiledManifest"),
        ("cloud_compile_workflow", "CompiledManifest"),
        ("healthz", "DeploymentHealth"),
    ],
)
def test_the_routes_the_ui_calls_have_real_types(
    document: dict[str, Any], operation_id: str, schema_name: str
) -> None:
    operation = next(op for _, _, op in _operations(document) if op["operationId"] == operation_id)
    assert schema_name in json.dumps(_json_200(operation))


def test_the_compile_routes_declare_the_manifest_envelope(document: dict[str, Any]) -> None:
    """What a client may rely on from a compiled manifest, and what it may not (api#12).

    The three routes share one model because they share one shape — a Kubernetes object — so this
    asserts the four keys every engine writes, and asserts that ``spec`` and the model itself stay
    open, which is the honest half of the old "deliberately an open object" and the half that keeps
    an out-of-tree engine's fields from being filtered away on the way out.
    """
    schema = document["components"]["schemas"]["CompiledManifest"]
    assert set(schema["required"]) == {"apiVersion", "kind", "metadata", "spec"}
    assert schema["properties"]["spec"]["additionalProperties"] is True, "the engine's own schema"
    assert schema["additionalProperties"] is True, "a response model filters; this one must not"

    metadata = document["components"]["schemas"]["CompiledManifestMetadata"]
    assert {"name", "namespace", "labels", "annotations"} <= set(metadata["properties"])


def test_the_replay_route_advertises_a_binary_download(document: dict[str, Any]) -> None:
    """It returns MCAP bytes; an empty schema generates as `unknown`, like an open object."""
    operation = next(
        op for _, _, op in _operations(document) if op["operationId"] == "bench_get_replay"
    )
    assert "application/octet-stream" in operation["responses"]["200"]["content"]


# --- schema names --------------------------------------------------------------------------------


def test_no_schema_name_is_a_module_path(document: dict[str, Any]) -> None:
    """FastAPI disambiguates colliding model names by prefixing the module path.

    ``astro_mine__bench__leaderboard___jobs__JobRecord`` is a type nobody can read in a client, and
    it moves whenever the platform moves a module. Collisions are resolved by naming instead.
    """
    offenders = [name for name in document["components"]["schemas"] if "astro_mine__" in name]
    assert not offenders, offenders


def test_the_two_job_records_are_distinguishable(document: dict[str, Any]) -> None:
    schemas = document["components"]["schemas"]
    assert "BenchJobRecord" in schemas
    assert "JobRecord" in schemas, "Studio's, now unambiguous because Bench's is named"


# --- the snapshot ---------------------------------------------------------------------------------


def test_the_document_matches_the_committed_snapshot(document: dict[str, Any]) -> None:
    """The gate: a change to the contract must be a deliberate act with a visible diff."""
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert document == committed, (
        "the OpenAPI document has changed. If that was intended, regenerate the snapshot with\n"
        "    uv run python scripts/update_openapi_snapshot.py\n"
        "and commit it alongside the change. If it was not, this caught what it exists to catch."
    )


def test_the_snapshot_comparison_actually_fails_on_a_rename(document: dict[str, Any]) -> None:
    """A gate nobody has seen reject anything is a gate nobody should trust.

    Renames one operation id in a copy of the document and asserts the comparison notices — the
    exact failure the snapshot exists to produce, without mutating the committed file.
    """
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    mutated = json.loads(json.dumps(document))
    mutated["paths"]["/hub/search"]["get"]["operationId"] = "hub_do_search"
    assert mutated != committed


def test_the_snapshot_is_committed_and_normalised() -> None:
    """It must not carry the build-derived version, or it passes only where it was written."""
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert Path(SNAPSHOT).exists()
    assert committed["info"]["version"] == VERSION_PLACEHOLDER
