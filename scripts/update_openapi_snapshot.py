#!/usr/bin/env python
"""Regenerate the committed OpenAPI snapshot (api#3).

    uv run python scripts/update_openapi_snapshot.py

The snapshot is the contract the front end's generated client is built from, and
``tests/test_openapi_contract.py`` fails when the live document and the snapshot disagree. That is
the point: a renamed operation or a reshaped response becomes a **deliberate act with a visible
diff** rather than a silent break of a downstream client.

So when that test fails, read the diff before running this. If the change was intended, run it and
commit the result alongside the change that caused it. If it was not, the test just caught what it
exists to catch.

What "the document" means lives in :mod:`astro_mine_api._openapi`, which the test imports too, so
the snapshot cannot be generated one way and checked another.
"""

from __future__ import annotations

import json
from pathlib import Path

from astro_mine_api._openapi import current_document

#: Beside the test that reads it.
SNAPSHOT = Path(__file__).resolve().parents[1] / "tests" / "openapi_snapshot.json"


def main() -> None:
    SNAPSHOT.write_text(
        json.dumps(current_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {SNAPSHOT}")


if __name__ == "__main__":
    main()
