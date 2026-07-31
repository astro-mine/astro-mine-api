"""Point the leaderboard's embargoed seed lookup at this repository's ``embargo/``.

``astro_mine.bench.leaderboard._eval.EMBARGO_ROOT`` is ``Path(__file__).parents[4] / "embargo"``
— the repo root above ``src/`` — because the sealed held-out seed sets are deliberately excluded
from the wheel and "the service runs from the repo" (bench.md §9). That resolved in
astro-mine-bench, where the leaderboard and the seeds sat in one checkout. Here the leaderboard
library arrives as an *installed* ``astro-mine-platform``, so the same expression points inside
``site-packages`` and finds nothing.

The seeds themselves came along — ``embargo/`` at this repository's root is the set the platform
ships, because astro-mine-api is now the repository the hosted leaderboard runs from. What cannot
follow is the *path calculation*: ``load_heldout_seeds`` binds ``EMBARGO_ROOT`` as a keyword
default at import time, and ``_service`` — like the tests that call it directly — invokes it with
no override, so rebinding the module attribute alone would change nothing. Rebinding the keyword
default on the function object itself reaches every caller, because they all hold the same
function.

**This is a deployment gap, not just a test one.** A hosted leaderboard installed from wheels has
the same broken lookup, and the fix — an environment override on the platform side — is the
platform's to make (it owns ``_eval``), not something to paper over from here. The fixture is
scoped to the Bench route tests so it cannot hide the gap anywhere else.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from astro_mine.bench.leaderboard import _eval

#: This repository's committed seed sets — the platform's ``embargo/``, verbatim.
EMBARGO_ROOT = Path(__file__).resolve().parents[2] / "embargo"


@pytest.fixture(autouse=True)
def _embargo_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(_eval, "EMBARGO_ROOT", EMBARGO_ROOT)
    kwdefaults = _eval.load_heldout_seeds.__kwdefaults__
    assert kwdefaults is not None and "embargo_root" in kwdefaults, (
        "load_heldout_seeds no longer takes embargo_root as a keyword default — the platform "
        "changed the seam this fixture redirects; re-read astro_mine.bench.leaderboard._eval"
    )
    monkeypatch.setitem(kwdefaults, "embargo_root", EMBARGO_ROOT)
    yield
