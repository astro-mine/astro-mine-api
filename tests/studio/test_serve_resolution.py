"""How a deployment's configuration reaches the Studio composition — flag → env → default.

The resolvers are the whole of "configure a Studio without editing code", and each has three arms
that a deployment actually hits: an explicit argument, an environment variable, and the fallback
that makes an unconfigured workstation work anyway (CX-LOCAL). `tests/studio/test_serve.py` covers
composing the app; this covers deciding *what* to compose it from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astro_mine_api.studio.serve import (
    CACHE_DIR_ENV,
    DEFAULT_TRUSTED_KEY_NAMES,
    REGISTRY_ENV,
    TRUSTED_KEY_ENV,
    UI_DIR_ENV,
    build_serve_app,
    resolve_cache_dir,
    resolve_key,
    resolve_registry,
    resolve_ui_dir,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (REGISTRY_ENV, TRUSTED_KEY_ENV, CACHE_DIR_ENV, UI_DIR_ENV):
        monkeypatch.delenv(name, raising=False)


# --- the registry path ----------------------------------------------------------------------


def test_registry_prefers_the_explicit_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REGISTRY_ENV, "/from/env")
    assert resolve_registry("/from/flag") == Path("/from/flag")


def test_registry_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REGISTRY_ENV, "~/registries/local")
    assert resolve_registry(None) == Path.home() / "registries" / "local"


def test_registry_is_none_when_nothing_is_configured() -> None:
    """Not an error: the Hub-backed routes 503 and the rest of Studio still works."""
    assert resolve_registry(None) is None


# --- key material ---------------------------------------------------------------------------


def test_key_prefers_the_flag_then_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TRUSTED_KEY_ENV, str(tmp_path / "from-env.pem"))
    assert resolve_key("~/flag.pem", TRUSTED_KEY_ENV, None, DEFAULT_TRUSTED_KEY_NAMES) == (
        Path.home() / "flag.pem"
    )
    assert resolve_key(None, TRUSTED_KEY_ENV, None, DEFAULT_TRUSTED_KEY_NAMES) == (
        tmp_path / "from-env.pem"
    )


def test_key_is_none_when_the_registry_ships_none(tmp_path: Path) -> None:
    """A registry with no `keys/` directory: publishing degrades, composition still succeeds."""
    assert resolve_key(None, TRUSTED_KEY_ENV, tmp_path, DEFAULT_TRUSTED_KEY_NAMES) is None


# --- the UI build and the content cache -------------------------------------------------------


def test_ui_dir_prefers_flag_then_env_then_the_conventional_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert resolve_ui_dir("~/built") == Path.home() / "built"
    monkeypatch.setenv(UI_DIR_ENV, str(tmp_path / "from-env"))
    assert resolve_ui_dir(None) == tmp_path / "from-env"
    monkeypatch.delenv(UI_DIR_ENV)
    monkeypatch.chdir(tmp_path)
    assert resolve_ui_dir(None) == tmp_path / "ui" / "dist-harness"


def test_cache_dir_is_created_not_merely_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The materializers write into it on first request, so composition must not leave it absent."""
    explicit = resolve_cache_dir(str(tmp_path / "explicit"))
    assert explicit.is_dir()

    monkeypatch.setenv(CACHE_DIR_ENV, str(tmp_path / "from-env"))
    assert resolve_cache_dir(None) == tmp_path / "from-env"
    assert (tmp_path / "from-env").is_dir()


def test_cache_dir_defaults_under_the_home_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_cache_dir(None) == tmp_path / ".cache" / "astro-mine-studio"


# --- composition degrades rather than failing --------------------------------------------------


def test_a_missing_hub_client_degrades_to_503_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CX-LOCAL: an unimportable Hub client must not become an ImportError traceback at startup.

    The client is a base dependency of this distribution, so the only way to reach the branch is
    to make the import fail — which is exactly what a broken or partial install looks like.
    """
    import builtins

    real_import = builtins.__import__

    def _explode(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("astro_mine.hub"):
            raise ImportError(f"no module named {name!r}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _explode)
    _app, report = build_serve_app(
        registry=tmp_path / "registry",
        trusted_key=None,
        signing_key=None,
        cache_dir=tmp_path / "cache",
        ui_dir=None,
        seed=False,
        host="127.0.0.1",
        port=8000,
    )
    assert not report.all_seams_wired
    assert all("not installed" in seam.detail for seam in report.seams)


def test_seeding_reports_a_failure_rather_than_refusing_to_start(tmp_path: Path) -> None:
    """A publisher that cannot pin the example is a degraded workspace, not a dead process."""

    class _BrokenPublisher:
        def publish_campaign(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("registry is read-only")

    from astro_mine_api.studio.serve import ServeReport, _attach_seed

    report = ServeReport(host="127.0.0.1", port=8000)
    _attach_seed(report, seed=True, publisher=_BrokenPublisher())
    assert report.seed_detail.startswith("could not pin the example campaign")
