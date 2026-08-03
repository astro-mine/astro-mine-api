"""A Core plugin-manifest factory for the Hub route tests.

`make_manifest` is copied verbatim from astro-mine-hub's `tests/conftest.py`; the policy
conformance table that shared that file is library-side and stayed with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from astro_mine.core.registry import PluginKind, PluginManifest, Provenance
from astro_mine.hub._content import content_hash

__all__ = ["make_manifest"]


def make_manifest(
    name: str = "pol",
    version: str = "1.0.0",
    *,
    kind: PluginKind = PluginKind.POLICY,
    interfaces: Mapping[str, str] | None = None,
    tags: Sequence[str] = (),
    license: str | None = "Apache-2.0",
    description: str = "",
    inputs: Sequence[str] = (),
    outputs: Sequence[str] = (),
    attributes: Mapping[str, Any] | None = None,
) -> PluginManifest:
    """A valid Core :class:`PluginManifest` with a content-addressed provenance digest."""
    return PluginManifest(
        name=name,
        version=version,
        kind=kind,
        core_interfaces=dict(interfaces or {"policy": "0.1.0"}),
        capability_tags=list(tags),
        license=license,
        description=description or None,
        inputs=list(inputs),
        outputs=list(outputs),
        attributes=dict(attributes or {}),
        provenance=Provenance(
            input_hashes=[],
            source_content_hashes={},
            digest=content_hash(f"{name}:{version}".encode()),
        ),
    )
