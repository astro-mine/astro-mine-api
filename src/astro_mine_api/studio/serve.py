"""Composing a runnable Studio: the FastAPI app plus its →Hub / ←Hub seams, wired from a local
OCI-layout registry.

:func:`build_serve_app` composes :func:`astro_mine_api.studio.app.create_app` with its seams wired
from a local registry, mounts the built UI, and returns an honest report naming every seam and its
state. It adds no route (the surface still has 9) — it only *composes and mounts* (studio.md §2
principle 8: the service is a deployment of the importable library core).

The load-bearing constraint is **CX-LOCAL**: this must reach a usable Studio on one workstation,
offline, with no account and no cluster. It resolves content from a local registry path (the
``--registry`` / ``ASTRO_MINE_HUB_REGISTRY`` convention), verifies it against a trusted public key,
and signs published campaigns with a local signing key — both defaulting to the signing keypair the
registry ships under ``<registry>/keys/`` (``cosign.*``, else the legacy ``anchor-dev.*``).

Where a seam cannot be satisfied locally (no registry, no key), composition still succeeds and says
*why* in the report and — via the 503 the route already answers — in the UI, rather than failing
(view.md principle 5: degrade visibly, never blank).

Ported from ``astro_mine.studio.cli`` (astro-mine-studio), whose `serve` command was 113 statements
of app composition for an import that does not resolve in a CLI that cannot reach the REST surface
(astro-mine-cli#12, astro-mine-cli/src/astro_mine/cli/studio). The composition came here, with the
REST surface it composes; the argparse front end stayed with the CLI. The functions and their
behaviour are unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI

from astro_mine_api.studio.app import create_app

__all__ = [
    "CACHE_DIR_ENV",
    "DEFAULT_SIGNING_KEY_NAMES",
    "DEFAULT_TRUSTED_KEY_NAMES",
    "REGISTRY_ENV",
    "SIGNING_KEY_ENV",
    "TRUSTED_KEY_ENV",
    "UI_DIR_ENV",
    "SeamState",
    "ServeReport",
    "build_serve_app",
    "render_banner",
    "resolve_cache_dir",
    "resolve_key",
    "resolve_registry",
    "resolve_ui_dir",
]

#: The environment default for the local registry path, matching the workspace convention every
#: producer/consumer shares (`ASTRO_MINE_HUB_REGISTRY` → `files/hub-registry`). A `--registry` flag
#: overrides it. The Hub *package* keeps no default path by design, so the deployment supplies one.
REGISTRY_ENV = "ASTRO_MINE_HUB_REGISTRY"
TRUSTED_KEY_ENV = "ASTRO_MINE_STUDIO_TRUSTED_KEY"
SIGNING_KEY_ENV = "ASTRO_MINE_STUDIO_SIGNING_KEY"
CACHE_DIR_ENV = "ASTRO_MINE_STUDIO_CACHE"
UI_DIR_ENV = "ASTRO_MINE_STUDIO_UI_DIR"

#: Default trusted / signing key filenames tried under `<registry>/keys/`, in order. Reads verify
#: against the public key; published campaigns are signed with the private key. The registry's own
#: `cosign.*` key — the key its content is actually signed with — wins; the legacy `anchor-dev` dev
#: key is the fallback for an older mirror. A deployment overrides both with `--trusted-key` /
#: `--signing-key` (or the env vars).
DEFAULT_TRUSTED_KEY_NAMES = ("cosign.pub", "anchor-dev.pub.pem")
DEFAULT_SIGNING_KEY_NAMES = ("cosign.key", "anchor-dev.key.pem")


@dataclass(frozen=True)
class SeamState:
    """One composed seam's state, for the banner and for tests to assert against."""

    name: str
    wired: bool
    detail: str


@dataclass
class ServeReport:
    """What composition produced — surfaced in the startup banner and returned by the app builder.

    A test asserts against this rather than scraping stdout, and the banner is a pure render of it,
    so ``none of the 9 routes 503`` (or an explicit labelled reason) is a checkable property.
    """

    host: str
    port: int
    seams: list[SeamState] = field(default_factory=list)
    ui_detail: str = ""
    ui_mounted: bool = False
    seed_detail: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def all_seams_wired(self) -> bool:
        return all(seam.wired for seam in self.seams)


_UI_NOT_BUILT_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Astro-Mine Studio</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 42rem; margin: 4rem auto; padding: 0 1rem }
  pre { background: #f4f4f7; padding: 1rem; border-radius: 6px }
</style>
</head>
<body>
<h1>Astro-Mine Studio is running</h1>
<p>The backend is up. The web UI is not part of this repository — Studio's front end is the
<code>/design</code> pages of the one application, in
<a href="https://github.com/astro-mine/astro-mine-ui">astro-mine-ui</a>. Point it at this
origin.</p>
<p>If you have a built UI directory of your own, pass <code>--ui-dir</code> to mount it here.</p>
<p>The API is live regardless — try <code>/studio/healthz</code> or <code>/docs</code>.</p>
</body>
</html>
"""


def _read_key(path: Path | None) -> bytes | None:
    """Read a PEM key file, or ``None`` if the path is absent. Missing files are a degrade signal,
    not a crash: composition reports the seam as unavailable rather than refusing to start."""
    if path is None or not path.is_file():
        return None
    return path.read_bytes()


def resolve_registry(arg: str | None) -> Path | None:
    """The local registry path: explicit argument → ``$ASTRO_MINE_HUB_REGISTRY`` → nothing."""
    value = arg if arg is not None else os.environ.get(REGISTRY_ENV)
    return Path(value).expanduser() if value else None


def resolve_key(
    arg: str | None, env: str, registry: Path | None, names: tuple[str, ...]
) -> Path | None:
    """Key precedence: explicit flag → env var → the first `names` entry that exists under
    `<registry>/keys/` → nothing."""
    if arg is not None:
        return Path(arg).expanduser()
    from_env = os.environ.get(env)
    if from_env:
        return Path(from_env).expanduser()
    if registry is not None:
        for name in names:
            candidate = registry / "keys" / name
            if candidate.is_file():
                return candidate
    return None


def resolve_ui_dir(arg: str | None) -> Path | None:
    """Locate a built UI directory. Explicit flag → env → `<cwd>/ui/dist-harness`.

    No repository carries a UI tree any more: Studio's front end is the `/design` pages of the one
    application in astro-mine-ui (RM-DIST-04/05), and the `Surface` contract this docstring used to
    describe is retired (ui.md §11). The default path is kept so an existing build stays mountable,
    and absent is fine — composition mounts a 'not built' page instead of 404-ing the root."""
    value = arg if arg is not None else os.environ.get(UI_DIR_ENV)
    if value:
        return Path(value).expanduser()
    return Path.cwd() / "ui" / "dist-harness"


def resolve_cache_dir(arg: str | None) -> Path:
    """The materialized-content cache root: explicit flag → env → ``~/.cache/astro-mine-studio``."""
    value = arg if arg is not None else os.environ.get(CACHE_DIR_ENV)
    root = Path(value).expanduser() if value else Path.home() / ".cache" / "astro-mine-studio"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _wire_hub_seams(
    registry: Path,
    trusted_key: Path | None,
    signing_key: Path | None,
    cache_root: Path,
) -> tuple[dict[str, object], list[SeamState]]:
    """Compose the concrete Hub seams against a local registry. Returns the ``create_app`` kwargs
    and the per-seam states for the banner.

    This is composition, not new machinery: ``HubArtifactPublisher``, ``HubWorldMaterializer``,
    ``HubAssetCatalog``, and ``HubAssetPreviewMaterializer`` already resolve content from an
    OCI-layout :class:`~astro_mine.hub.registry.Registry` and re-verify it client-side before
    trusting a byte (hub.md §2.3). This just points them at a local path and hands them the keys.
    """
    from astro_mine.hub.client import HubClient
    from astro_mine.hub.registry import Registry
    from astro_mine.studio.hub import (
        HubArtifactPublisher,
        HubAssetCatalog,
        HubAssetPreviewMaterializer,
        HubCapabilityResolver,
        HubWorldMaterializer,
    )

    trusted_pem = _read_key(trusted_key)
    signing_pem = _read_key(signing_key)

    reg = Registry(registry)
    client = HubClient(reg, trusted_public_key_pem=trusted_pem)
    worlds_cache = cache_root / "worlds"
    assets_cache = cache_root / "assets"
    worlds_cache.mkdir(parents=True, exist_ok=True)
    assets_cache.mkdir(parents=True, exist_ok=True)

    # The read seams verify pulled bytes against the trusted key. Without one, the route is still
    # live (not 503) but every materialize/preview would fail verification — so we wire it and warn,
    # rather than silently serving a Studio whose panes error on first click.
    if trusted_pem is not None and trusted_key is not None:
        verify_note = f"verifying against {trusted_key.name}"
    else:
        verify_note = "NO trusted key — content verification will fail (pass --trusted-key)"

    kwargs: dict[str, object] = {
        "materializer": HubWorldMaterializer(client, cache_dir=worlds_cache),
        "world_cache_dir": str(worlds_cache),
        "catalog": HubAssetCatalog(reg),
        "preview_materializer": HubAssetPreviewMaterializer(client, cache_dir=assets_cache),
        "asset_cache_dir": str(assets_cache),
    }
    seams = [
        SeamState("terrain", True, verify_note),
        SeamState("catalog", True, f"listing assets from {registry}"),
        SeamState("asset preview", True, verify_note),
    ]

    # Publishing is the one seam that needs a *signing* key, not just a verify key. Without one it
    # genuinely cannot be satisfied locally, so the route stays 503 and the banner says why — the
    # explicit escape hatch in the acceptance criteria.
    if signing_pem is not None:
        kwargs["publisher"] = HubArtifactPublisher(
            client,
            capability_resolver=HubCapabilityResolver(reg),
            private_key_pem=signing_pem,
        )
        assert signing_key is not None
        seams.insert(0, SeamState("publishing", True, f"signing with {signing_key.name}"))
    else:
        seams.insert(
            0,
            SeamState(
                "publishing", False, "no signing key — publish disabled (pass --signing-key)"
            ),
        )

    return kwargs, seams


def build_serve_app(
    *,
    registry: Path | None,
    trusted_key: Path | None,
    signing_key: Path | None,
    cache_dir: Path,
    ui_dir: Path | None,
    seed: bool,
    host: str,
    port: int,
) -> tuple[FastAPI, ServeReport]:
    """Compose the Studio app and a report of what got wired.

    Separated from the uvicorn run so it is testable with a ``TestClient`` and no live server:
    everything the acceptance criteria assert (no 503 when wired, the 'not built' root, the banner
    naming each seam) is a property of the returned ``(app, report)``.
    """
    report = ServeReport(host=host, port=port)
    publisher: object | None = None

    if registry is None:
        # No registry → the read/publish seams have nowhere to resolve from. The app still captures
        # intent and runs studies; the 5 Hub routes 503, honestly (studio.md §6).
        app = create_app()
        reason = f"no registry (pass --registry or set ${REGISTRY_ENV})"
        report.seams = [
            SeamState(name, False, reason)
            for name in ("publishing", "terrain", "catalog", "asset preview")
        ]
    else:
        try:
            kwargs, seams = _wire_hub_seams(registry, trusted_key, signing_key, cache_dir)
        except ImportError:
            # The Hub client is not importable. Start anyway; the Hub routes 503 with a detail the
            # UI shows — never an ImportError traceback (CX-LOCAL).
            app = create_app()
            reason = "the Hub client is not installed (pip install astro-mine-api)"
            report.seams = [
                SeamState(name, False, reason)
                for name in ("publishing", "terrain", "catalog", "asset preview")
            ]
        else:
            app = create_app(**kwargs)  # type: ignore[arg-type]
            report.seams = seams
            publisher = kwargs.get("publisher")

    _mount_ui(app, ui_dir, report)
    _attach_seed(report, seed, publisher)
    return app, report


def _mount_ui(app: FastAPI, ui_dir: Path | None, report: ServeReport) -> None:
    """Serve the built UI at ``/`` when present; otherwise mount a labelled 'not built' page.

    The static mount is appended *after* ``create_app`` registered the API routes, so the API wins
    for its own paths and the catch-all only handles the browser. When ``ui_dir`` is absent the root
    explains how to build it — it does not 404 and does not crash (acceptance criterion)."""
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles

    if ui_dir is not None and ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
        report.ui_mounted = True
        report.ui_detail = f"mounted from {ui_dir}"
        return

    @app.get("/", include_in_schema=False)
    def _root() -> HTMLResponse:
        return HTMLResponse(_UI_NOT_BUILT_HTML)

    report.ui_mounted = False
    report.ui_detail = (
        "UI not found — Studio's front end is the /design pages of astro-mine-ui"
        if ui_dir is not None
        else "UI mount disabled"
    )


def _attach_seed(report: ServeReport, seed: bool, publisher: object | None) -> None:
    """Pin the example campaign so a fresh serve is never an empty screen (#32), and record what
    happened. Idempotent — a re-run resolves the existing pin. Without a publisher (no registry /
    no signing key) the campaign cannot be pinned, but the built-in UI example still opens the
    workspace on something labelled, so this degrades honestly rather than failing to start."""
    if not seed:
        report.seed_detail = "seeding disabled (--no-seed)"
        return
    if publisher is None:
        report.seed_detail = (
            "example not pinned (no publisher); the UI still opens on the built-in example"
        )
        return
    try:
        from astro_mine.studio.seed import ensure_example_seeded

        report.seed_detail = f"example campaign pinned: {ensure_example_seeded(publisher)}"  # type: ignore[arg-type]
    except Exception as exc:
        report.seed_detail = f"could not pin the example campaign: {exc}"


def render_banner(report: ServeReport) -> str:
    """The honest startup banner: the bound URL, the UI state, and every seam and why. A user must
    never have to read ``_require`` to understand a 503."""
    lines = [
        "",
        "  Astro-Mine Studio",
        f"  → {report.url}",
        f"  UI:   {report.ui_detail}",
        "  Seams:",
    ]
    for seam in report.seams:
        mark = "✓" if seam.wired else "○"
        lines.append(f"    {mark} {seam.name}: {seam.detail}")
    lines.append(f"  Seed: {report.seed_detail}")
    lines.append("")
    return "\n".join(lines)
