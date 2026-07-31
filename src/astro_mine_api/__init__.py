"""Astro-Mine-API — the REST tier (architecture/api.md).

Every REST surface the platform serves, as FastAPI route modules over
``astro-mine-platform``'s ordinary Python API. Four components have a web face — :mod:`Hub
<astro_mine_api.hub>`, :mod:`Studio <astro_mine_api.studio>`, :mod:`Cloud
<astro_mine_api.cloud>` and :mod:`Bench <astro_mine_api.bench>` — and none of them lives inside
the component any more: the library half stays in the platform, the routes are here, and the
service is a *deployment* of the library rather than a second codebase (conventions.md §1
tenet 4).

**Route paths are component-prefixed.** Every route this distribution serves begins with its
owning component's name — ``/hub/…``, ``/studio/…``, ``/cloud/…``, ``/bench/…`` — so one
deployment can serve any subset of the surfaces in one process with no path collisions and no
per-deployment routing table. Each surface package exports the prefix it owns as ``PREFIX``.

**Not a Python API.** Nothing should import this distribution as a library. If code wants what
an endpoint does, it wants the platform function the endpoint calls (api.md §5).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("astro-mine-api")
except PackageNotFoundError:  # pragma: no cover - only in a source tree with no install
    __version__ = "0.0.0"

__all__ = ["__version__"]
