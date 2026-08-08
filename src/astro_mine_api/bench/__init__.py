"""The public leaderboard's REST surface — submit-policy-we-run + Hub-digest intake.

Only the route module is here. The leaderboard *library* — models, stores, evaluation, ranking,
Hub-digest intake, provenance bundles, auth/authz, audit and the :class:`LeaderboardService`
orchestration — is in ``astro_mine.bench.leaderboard`` in the platform, because it would still
make sense with no HTTP in the picture (api.md §2).

Ported from ``astro_mine.bench.leaderboard._app`` (astro-mine-bench) — see
:mod:`astro_mine_api.bench._app`.

Backlog: RM-P1-BENCH-10 — astro-mine-bench#18
"""

from __future__ import annotations

from astro_mine_api.bench._app import (
    DB_ENV,
    OBJECTS_ENV,
    PREFIX,
    REGISTRY_ENV,
    SANDBOX_CPU_SECONDS_ENV,
    SANDBOX_MEMORY_BYTES_ENV,
    SANDBOX_PYTHONPATH_ENV,
    SANDBOX_WALL_SECONDS_ENV,
    build_router,
    create_app,
)

__all__ = [
    "DB_ENV",
    "OBJECTS_ENV",
    "PREFIX",
    "REGISTRY_ENV",
    "SANDBOX_CPU_SECONDS_ENV",
    "SANDBOX_MEMORY_BYTES_ENV",
    "SANDBOX_PYTHONPATH_ENV",
    "SANDBOX_WALL_SECONDS_ENV",
    "build_router",
    "create_app",
]
