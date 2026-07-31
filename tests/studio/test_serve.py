"""Composing a runnable Studio — the wiring that takes a clone to a served Studio (G2.3/G2.4).

These bind the same concrete Hub seams a deployment uses to a temporary local registry, so what is
exercised is the real offline wiring: with a registry + the anchor-dev keys, none of the Hub routes
503; without them, they degrade honestly; and the root URL always says something, never a 404.
No live uvicorn is started — `build_serve_app` returns the composed app for a `TestClient`.

Ported from astro-mine-studio's `tests/test_cli.py`. Its last test covered the argparse
front end, which stayed with `astro-mine-cli`; everything else is the composition, which
came here with the surface it composes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from astro_mine.core.registry import CapabilityTag
from astro_mine.hub.registry import Registry
from astro_mine.hub.supply_chain import generate_keypair
from fastapi.testclient import TestClient

from astro_mine_api.studio.serve import (
    DEFAULT_SIGNING_KEY_NAMES,
    DEFAULT_TRUSTED_KEY_NAMES,
    SIGNING_KEY_ENV,
    TRUSTED_KEY_ENV,
    build_serve_app,
    render_banner,
    resolve_key,
)

from ._assets import publish_asset


def _registry_with_anchor_keys(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A local registry holding one published asset, with the signing keypair laid down under
    ``<registry>/keys/`` under the primary default name exactly where `serve` looks for it."""
    private_pem, public_pem = generate_keypair()
    reg_path = tmp_path / "registry"
    reg = Registry(reg_path)
    publish_asset(
        reg,
        private_pem,
        asset_id="astro-mine.fleet.hopper",
        kind="hopper",
        name="Hopper Mk1",
        tags=[CapabilityTag("mobility.wheeled")],
    )
    keys = reg_path / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    trusted = keys / DEFAULT_TRUSTED_KEY_NAMES[0]
    signing = keys / DEFAULT_SIGNING_KEY_NAMES[0]
    trusted.write_bytes(public_pem)
    signing.write_bytes(private_pem)
    return reg_path, trusted, signing


def _serve(tmp_path: Path, **overrides: object):
    defaults: dict[str, object] = {
        "registry": None,
        "trusted_key": None,
        "signing_key": None,
        "cache_dir": tmp_path / "cache",
        "ui_dir": None,
        "seed": False,
        "host": "127.0.0.1",
        "port": 8000,
    }
    defaults.update(overrides)
    return build_serve_app(**defaults)  # type: ignore[arg-type]


def test_serve_wires_every_seam_from_a_local_registry(tmp_path: Path) -> None:
    reg_path, trusted, signing = _registry_with_anchor_keys(tmp_path)
    app, report = _serve(tmp_path, registry=reg_path, trusted_key=trusted, signing_key=signing)

    assert report.all_seams_wired, report.seams
    client = TestClient(app)
    # The routes the UI actually hits are live, not 503.
    assert client.get("/studio/healthz").status_code == 200
    assert client.get("/studio/catalog/assets").status_code == 200
    # A wired materializer/preview 404s an unknown reference; an *unwired* one would 503.
    assert client.get("/studio/worlds/does-not-exist").status_code == 404
    assert client.get("/studio/catalog/preview/does-not-exist").status_code == 404


def test_serve_without_registry_degrades_to_503_not_a_crash(tmp_path: Path) -> None:
    app, report = _serve(tmp_path, registry=None)

    assert not report.all_seams_wired
    client = TestClient(app)
    assert client.get("/studio/healthz").status_code == 200  # the app still starts and answers
    assert client.get("/studio/catalog/assets").status_code == 503  # honest, not a 500/traceback


def test_publish_seam_stays_503_without_a_signing_key(tmp_path: Path) -> None:
    reg_path, trusted, _ = _registry_with_anchor_keys(tmp_path)
    _app, report = _serve(tmp_path, registry=reg_path, trusted_key=trusted, signing_key=None)

    publishing = next(seam for seam in report.seams if seam.name == "publishing")
    assert not publishing.wired
    assert "signing key" in publishing.detail
    # The read seams are still live even though publish is not.
    terrain = next(seam for seam in report.seams if seam.name == "terrain")
    assert terrain.wired


def test_root_explains_how_to_build_when_ui_absent(tmp_path: Path) -> None:
    app, report = _serve(tmp_path, ui_dir=tmp_path / "no-such-dist")

    assert not report.ui_mounted
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200  # not a 404
    assert "pnpm build" in response.text


def test_serves_the_built_ui_without_shadowing_the_api(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>studio</title>hello-studio")
    app, report = _serve(tmp_path, ui_dir=dist)

    assert report.ui_mounted
    client = TestClient(app)
    root = client.get("/")
    assert root.status_code == 200
    assert "hello-studio" in root.text
    # The catch-all UI mount must not shadow the API.
    assert client.get("/studio/healthz").status_code == 200


def test_banner_names_every_seam_and_the_url(tmp_path: Path) -> None:
    reg_path, trusted, signing = _registry_with_anchor_keys(tmp_path)
    _app, report = _serve(
        tmp_path, registry=reg_path, trusted_key=trusted, signing_key=signing, port=8123
    )
    banner = render_banner(report)

    for name in ("publishing", "terrain", "catalog", "asset preview"):
        assert name in banner
    assert "http://127.0.0.1:8123" in banner
    assert report.seed_detail in banner


def test_serve_pins_the_example_campaign_idempotently(tmp_path: Path) -> None:
    reg_path, trusted, signing = _registry_with_anchor_keys(tmp_path)
    # The example swarm resolves against a real asset — publish it so the campaign can be authored,
    # signed, and admitted (the publisher inherits its capability tags, fail-closed).
    publish_asset(
        Registry(reg_path),
        signing.read_bytes(),
        asset_id="astro-mine.fleet.prospecting-rover",
        kind="rover",
        name="Prospecting Rover",
        tags=[CapabilityTag("mobility.wheeled")],
    )
    _app, report = _serve(
        tmp_path, registry=reg_path, trusted_key=trusted, signing_key=signing, seed=True
    )
    assert report.seed_detail.startswith("example campaign pinned"), report.seed_detail
    assert "example-lunar-ice:0.1.0" in report.seed_detail

    # Idempotent: a second serve resolves the existing pin and re-authors nothing.
    _app2, report2 = _serve(
        tmp_path, registry=reg_path, trusted_key=trusted, signing_key=signing, seed=True
    )
    assert report2.seed_detail == report.seed_detail


def test_seed_degrades_honestly_without_a_publisher(tmp_path: Path) -> None:
    _app, report = _serve(tmp_path, registry=None, seed=True)
    assert "not pinned" in report.seed_detail  # no publisher; the built-in UI example still opens


def test_registry_keys_are_the_default_when_no_flag_or_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(TRUSTED_KEY_ENV, raising=False)
    monkeypatch.delenv(SIGNING_KEY_ENV, raising=False)
    reg_path, trusted, signing = _registry_with_anchor_keys(tmp_path)

    assert resolve_key(None, TRUSTED_KEY_ENV, reg_path, DEFAULT_TRUSTED_KEY_NAMES) == trusted
    assert resolve_key(None, SIGNING_KEY_ENV, reg_path, DEFAULT_SIGNING_KEY_NAMES) == signing

    # The legacy anchor-dev key is still found as a fallback when it is the only key present.
    legacy = tmp_path / "legacy"
    (legacy / "keys").mkdir(parents=True)
    (legacy / "keys" / "anchor-dev.pub.pem").write_bytes(b"x")
    assert resolve_key(None, TRUSTED_KEY_ENV, legacy, DEFAULT_TRUSTED_KEY_NAMES) == (
        legacy / "keys" / "anchor-dev.pub.pem"
    )


def test_read_seams_are_wired_but_warn_when_no_trusted_key_is_present(tmp_path: Path) -> None:
    """A registry with no verify key: the routes stay live and the report says content
    verification will fail, rather than a Studio whose panes error on first click with no
    explanation of why."""
    reg_path, _trusted, signing = _registry_with_anchor_keys(tmp_path)
    _app, report = _serve(tmp_path, registry=reg_path, trusted_key=None, signing_key=signing)

    terrain = next(seam for seam in report.seams if seam.name == "terrain")
    assert terrain.wired
    assert "NO trusted key" in terrain.detail
