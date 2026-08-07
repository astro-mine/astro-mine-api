"""Nothing this tier serves may name a distribution, package or build script that no longer exists.

Consolidation retired seventeen `astro-mine-<component>` distributions (RM-DIST-01/02) and the
rebuild retired four `@astro-mine/*` front-end packages along with the `ui/` trees they were built
from (RM-DIST-04). A name that resolves to nothing is not a cosmetic defect: the reader follows it,
their package manager reports that no such thing exists, and they conclude their environment is
broken rather than the message.

`astro-mine-cli` learned this twice (`astro-mine-cli#18`, `#19`) and wrote a gate. This tier had
none, and it is the tier that serves **HTML to a browser** — so the same bug lived here longer and
was found the same way, by a human reading the output months later:

    <pre>cd ui &amp;&amp; pnpm install &amp;&amp; pnpm build:harness</pre>

`astro-mine-studio` had already fixed that exact string in `b33e03c`, the commit that deleted its
`ui/` tree. The fix landed after the route modules were copied here, so this repository kept the
pre-fix text — and a test pinned it in place by asserting `"pnpm build" in response.text`.

**What is checked, and why by emission rather than by grep.** Prose that *quotes* a retired name
while explaining that it is retired is correct and must survive — this module's own docstring does
it three times. So the checks drive the tier and read what comes *out*: the HTML the root serves,
the composition report's details, and every description the OpenAPI document publishes. Those are
the strings a user can actually reach.

**`dist-harness` is deliberately not retired.** It is the default *path* `--ui-dir` falls back to,
kept so an existing build stays mountable (`resolve_ui_dir`). What is retired is `build:harness` —
the pnpm script that would produce one, which no repository ships any more.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from astro_mine_api._openapi import current_document
from astro_mine_api.studio.serve import build_serve_app

#: The seventeen distributions consolidation retired. `astro-mine-{platform,cli,api,ui}` are the
#: four that exist and must stay sayable, so this is a list rather than an `astro-mine-\w+` pattern.
RETIRED_DISTRIBUTIONS = [
    f"astro-mine-{component}"
    for component in [
        "allocate",
        "bench",
        "cloud",
        "core",
        "fleet",
        "guard",
        "hub",
        "learn",
        "link",
        "mind",
        "prospect",
        "seal",
        "sim",
        "spice",
        "studio",
        "surrogate",
        "worlds",
    ]
]

#: The front-end packages the rebuild retired outright (ui.md §8.2). `@astro-mine/ui` and
#: `@astro-mine/view` are *not* here: those names live on in `astro-mine-ui`, and only the versions
#: the old repositories cut are history.
RETIRED_PACKAGES = [
    "@astro-mine/surface",
    "@astro-mine/hub-ui",
    "@astro-mine/studio-ui",
    "@astro-mine/bench-ui",
]

#: Build steps that named a tree no repository carries any more.
RETIRED_BUILD_STEPS = ["build:harness", "cd ui", "in ui/"]

RETIRED = RETIRED_DISTRIBUTIONS + RETIRED_PACKAGES + RETIRED_BUILD_STEPS


def _offenders(text: str) -> list[str]:
    """Every retired name the given user-visible string contains."""
    return [name for name in RETIRED if name in text]


@pytest.fixture
def report_and_client(tmp_path: Path):  # type: ignore[no-untyped-def]
    """`studio serve` with no UI directory — the composition that renders the 'not found' root."""
    app, report = build_serve_app(
        registry=None,
        trusted_key=None,
        signing_key=None,
        cache_dir=tmp_path / "cache",
        ui_dir=tmp_path / "absent",
        seed=False,
        host="127.0.0.1",
        port=8000,
    )
    return report, TestClient(app)


def test_the_served_root_names_nothing_retired(report_and_client) -> None:  # type: ignore[no-untyped-def]
    """The page a browser lands on when no UI is mounted. This is where the bug was."""
    _, client = report_and_client
    body = client.get("/").text
    assert _offenders(body) == [], f"the 'no UI' page names retired things: {_offenders(body)}"
    # Not merely free of the old name -- it has to say where the front end actually is.
    assert "astro-mine-ui" in body


def test_the_composition_report_names_nothing_retired(report_and_client) -> None:  # type: ignore[no-untyped-def]
    """`ui_detail` reaches the operator through the startup banner, which renders this report."""
    report, _ = report_and_client
    details = [report.ui_detail, report.seed_detail, *(seam.detail for seam in report.seams)]
    for detail in details:
        assert _offenders(detail) == [], f"the serve report names retired things: {detail!r}"


def test_no_published_description_names_anything_retired() -> None:
    """Every summary and description in the OpenAPI document.

    A description is reachable without running anything -- `/docs` renders them, and the generated
    client copies them into the front end's own source -- so no request-driven test would see one.
    """
    document = current_document()
    offences: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"description", "summary", "title"} and isinstance(value, str):
                    for name in _offenders(value):
                        offences.append(f"{path}.{key}: {name}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(document, "openapi")
    assert offences == [], "retired names published in the OpenAPI document: " + "; ".join(offences)


def test_the_gate_would_have_caught_the_bug_it_was_written_for() -> None:
    """The regression this module exists for, asserted directly.

    Without this, a refactor that narrowed `_offenders` to the empty list would leave every test
    above passing and prove nothing. The exact string `astro-mine-studio` fixed in `b33e03c` must
    still be one this module rejects.
    """
    assert _offenders("cd ui &amp;&amp; pnpm install &amp;&amp; pnpm build:harness")
    assert _offenders("run `pnpm build:harness` in ui/")
    assert _offenders("pip install astro-mine-studio[serve]")
    assert _offenders("@astro-mine/bench-ui")
    # ...and the four live distributions must not be rejected, or the gate blocks correct prose.
    for live in ("astro-mine-platform", "astro-mine-cli", "astro-mine-api", "astro-mine-ui"):
        assert _offenders(live) == [], f"{live} is a live distribution and must stay sayable"


def test_the_retired_vocabulary_covers_every_component() -> None:
    """Seventeen, not sixteen — a component missing from the list is a name that can rot unseen."""
    assert len(RETIRED_DISTRIBUTIONS) == 17
    # The list is spelled out above; this pins it against the import path the platform actually
    # ships, so a component that is renamed or added shows up here rather than silently missing.
    import astro_mine

    # `astro_mine` is a namespace package, so `__file__` is None -- walk `__path__` instead.
    # `cli` is excluded because it is `astro-mine-cli`'s, not the platform's: it shares the
    # namespace and shows up here whenever both are installed in one environment.
    packaged = {
        entry.name
        for root in astro_mine.__path__
        for entry in Path(root).iterdir()
        if entry.is_dir() and not entry.name.startswith(("_", "."))
    } - {"cli"}
    named = {name.removeprefix("astro-mine-") for name in RETIRED_DISTRIBUTIONS}
    assert named <= packaged, f"named but not packaged: {sorted(named - packaged)}"
    assert packaged <= named, f"packaged but unnamed: {sorted(packaged - named)}"


def test_no_route_module_ships_a_retired_default() -> None:
    """`resolve_ui_dir`'s fallback is a path, not a build step -- and must stay a path.

    `dist-harness` is intentionally still here (an existing build stays mountable). This pins that
    distinction: the default may name the directory, but nothing may tell a user to *create* it.
    """
    from astro_mine_api.studio.serve import resolve_ui_dir

    default = str(resolve_ui_dir(None))
    assert default.endswith("dist-harness")
    assert not re.search(r"pnpm|build:harness", default)
