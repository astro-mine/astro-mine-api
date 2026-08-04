"""Point the leaderboard's embargoed seed lookup at this repository's ``embargo/``.

The sealed held-out seed sets are excluded from the platform's wheel — "the service runs from the
repo" (bench.md §9) — and this is the repository the hosted leaderboard runs from, so ``embargo/``
at its root is the set they live in.

**This used to be twelve lines of rebinding a keyword default on ``load_heldout_seeds``**, because
the platform derived the path from its own module location and offered no override, so a
wheel-installed leaderboard resolved it inside ``site-packages`` and found nothing. The docstring
here called that "a deployment gap, not just a test one" and said the fix belonged upstream; it did,
and ``astro-mine-platform#15`` made it — the lookup now reads
``$ASTRO_MINE_BENCH_EMBARGO_ROOT`` per call. So this is one environment variable, set the way a
deployment sets it (#19).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from astro_mine.bench.leaderboard import EMBARGO_ROOT_ENV

#: This repository's committed seed sets — the platform's ``embargo/``, verbatim.
EMBARGO_ROOT = Path(__file__).resolve().parents[2] / "embargo"


@pytest.fixture(autouse=True)
def _embargo_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(EMBARGO_ROOT_ENV, str(EMBARGO_ROOT))
    yield
