"""Publishing signed asset artifacts the way Fleet does — the fixture the `/studio/catalog`
route tests build their registry from.

Copied verbatim from astro-mine-studio's `tests/test_hub_catalog.py`, which stayed with the
library half in the platform. The route tests imported `_publish_asset` from it across files;
here it is its own module, because the test that owned it did not come along.

The SADF-JSON layer is opaque to Studio (it copies, never parses it), so any JSON suffices; the
geometry blobs are mapped ``uri -> digest`` through ``provenance.source_content_hashes``, exactly
as Fleet stamps them.
"""

from __future__ import annotations

import hashlib
import json

from astro_mine.core.registry import CapabilityTag, PluginKind, PluginManifest, Provenance
from astro_mine.hub.client import HubClient
from astro_mine.hub.registry import Blob, Registry
from astro_mine.studio.hub.catalog import (
    MEDIA_GEOMETRY_GLTF,
    MEDIA_GEOMETRY_USD,
    MEDIA_SADF_JSON,
)

__all__ = ["HOPPER", "ORBITER", "publish_asset"]

ORBITER = "astro-mine.fleet.relay-orbiter:0.1.0"
HOPPER = "astro-mine.fleet.hopper:0.1.0"


def _sha(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def publish_asset(
    registry: Registry,
    private_pem: bytes,
    *,
    asset_id: str,
    kind: str,
    name: str,
    tags: list[CapabilityTag],
    geometry: dict[str, bytes] | None = None,
    with_sadf: bool = True,
) -> str:
    """Publish a signed asset artifact and return its ``name:version`` reference.

    The SADF-JSON layer is opaque to Studio (it copies, never parses it), so any JSON suffices; the
    geometry blobs are mapped ``uri → digest`` through ``provenance.source_content_hashes``, exactly
    as Fleet stamps them.
    """
    geometry = geometry or {}
    sadf = json.dumps(
        {"asset": {"identity": {"id": asset_id, "kind": kind, "name": name}}}
    ).encode()
    layers = [Blob(MEDIA_SADF_JSON, sadf)] if with_sadf else []
    source_hashes: dict[str, str] = {}
    for uri, data in geometry.items():
        layers.append(
            Blob(MEDIA_GEOMETRY_USD if uri.endswith(".usda") else MEDIA_GEOMETRY_GLTF, data)
        )
        source_hashes[uri] = _sha(data)
    manifest = PluginManifest(
        name=asset_id,
        version="0.1.0",
        kind=PluginKind.ASSET,
        capability_tags=list(tags),
        attributes={"asset_kind": kind, "asset_name": name},
        provenance=Provenance(digest=_sha(sadf), source_content_hashes=source_hashes),
    )
    HubClient(registry).publish(
        name=asset_id,
        version="0.1.0",
        kind="asset",
        manifest=manifest,
        layers=layers,
        private_key_pem=private_pem,
    )
    return f"{asset_id}:0.1.0"
