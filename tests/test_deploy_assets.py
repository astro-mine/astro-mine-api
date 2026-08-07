"""The deployment artifacts, checked against the code they configure (RM-DIST-03; api.md §3, §6).

`deploy/` is the one part of this distribution no test can drive end-to-end here: bringing the
stack up needs a Docker daemon, and installing the chart needs a cluster. That is exactly why it
rots. Every defect this workstream was opened for was of the same kind -- a string in a file nobody
executes, naming something that no longer exists, found months later by a human reading it.

So these check the properties that *are* checkable without a daemon: that every environment
variable the deployment sets is one the code reads, that every path it mounts is present, and that
the compose file and the chart agree with each other about how the tier is wired. A typo'd variable
name is silently ignored by both Docker and Kubernetes -- the container starts, the setting simply
never applies, and the surface falls back to a local default in production.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from astro_mine_api._app import HUB_REGISTRY_ENV, SURFACES_ENV
from astro_mine_api._cors import CORS_ORIGINS_ENV
from astro_mine_api.bench._app import DB_ENV
from astro_mine_api.studio.serve import CACHE_DIR_ENV

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"

#: Variables the platform library reads rather than this package -- Bench's OIDC/OPA/signer wiring
#: and Hub's stores. They have no importable constant here, so they are spelled out, and
#: `test_every_env_var_is_read_by_something` proves each is read by *some* installed module.
PLATFORM_ENV = {
    "ASTRO_MINE_BENCH_CATALOG_DSN",
    "ASTRO_MINE_BENCH_OIDC_ISSUER",
    "ASTRO_MINE_BENCH_OIDC_AUDIENCE",
    "ASTRO_MINE_BENCH_OPA_URL",
    "ASTRO_MINE_BENCH_TRUSTED_KEY",
    "HUB_POSTGRES_URL",
    "HUB_OPA_URL",
}

KNOWN_ENV = PLATFORM_ENV | {
    SURFACES_ENV,
    CORS_ORIGINS_ENV,
    HUB_REGISTRY_ENV,
    DB_ENV,
    CACHE_DIR_ENV,
}


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))


def test_the_api_service_sets_only_variables_something_reads(compose: dict) -> None:
    """A misspelled variable errors nowhere -- it is a setting that silently never applies."""
    declared = set(compose["services"]["api"]["environment"])
    unknown = declared - KNOWN_ENV
    assert unknown == set(), f"compose sets variables nothing reads: {sorted(unknown)}"


def test_every_env_var_is_read_by_something(compose: dict) -> None:
    """The other direction: each name must appear in installed source, not just in this test.

    Catches the case where a variable is renamed in the platform and the deployment keeps setting
    the old one -- which reads as "configured" in every review and applies nothing at runtime.
    """
    import astro_mine

    import astro_mine_api

    roots = [Path(astro_mine_api.__file__).parent, *(Path(p) for p in astro_mine.__path__)]
    sources = " ".join(
        path.read_text(encoding="utf-8", errors="replace")
        for root in roots
        for path in root.rglob("*.py")
    )
    for name in set(compose["services"]["api"]["environment"]):
        assert f'"{name}"' in sources, f"{name} is set by compose but read by no installed module"


def test_every_mounted_path_exists(compose: dict) -> None:
    """A bind mount of a missing host path is not a startup error -- Docker creates a directory.

    OPA would then serve an empty bundle and allow nothing; Grafana would show no dashboards. Both
    look like a policy or telemetry problem rather than a missing file.
    """
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            if not isinstance(volume, str) or not volume.startswith("./"):
                continue
            host = (DEPLOY / volume.split(":")[0][2:]).resolve()
            assert host.exists(), f"service {name!r} mounts {host}, which is not in the repository"


def test_the_image_the_compose_builds_is_the_one_in_this_directory(compose: dict) -> None:
    """`context` is relative to the compose file; `dockerfile` is relative to the context."""
    build = compose["services"]["api"]["build"]
    assert (DEPLOY / build["context"] / build["dockerfile"]).resolve().is_file()


def test_the_chart_and_the_compose_agree_on_the_port() -> None:
    """Both describe the same server; a disagreement means one of them was edited alone."""
    compose_doc = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    values = yaml.safe_load(
        (DEPLOY / "helm" / "astro-mine-api" / "values.yaml").read_text(encoding="utf-8")
    )
    published = compose_doc["services"]["api"]["ports"][0]
    assert published.endswith(":8000")
    assert values["service"]["port"] == 8000


def test_the_chart_defaults_are_fail_closed() -> None:
    """Bench's write routes must not become open by default through a chart value (bench#29).

    `oidcIssuer`/`oidcAudience` empty is what keeps them answering 503. A default that filled either
    in -- even a placeholder -- would turn a fail-closed tier into one that merely looks configured.
    """
    values = yaml.safe_load(
        (DEPLOY / "helm" / "astro-mine-api" / "values.yaml").read_text(encoding="utf-8")
    )
    assert values["bench"]["oidcIssuer"] == ""
    assert values["bench"]["oidcAudience"] == ""
    # No wildcard CORS default either: an unset origin list must be a closed door.
    assert values["corsOrigins"] == []
    assert values["securityContext"]["readOnlyRootFilesystem"] is True
    assert values["podSecurityContext"]["runAsNonRoot"] is True


def test_the_readonly_root_filesystem_has_a_volume_for_every_write_path() -> None:
    """`readOnlyRootFilesystem: true` turns a forgotten write path into a runtime crash loop."""
    template = (DEPLOY / "helm" / "astro-mine-api" / "templates" / "deployment.yaml").read_text(
        encoding="utf-8"
    )
    for written in ("/var/cache/astro-mine-studio", "/tmp"):
        assert f"mountPath: {written}" in template, f"no volume mounted at {written}"
