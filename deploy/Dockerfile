# The REST tier — one image serving every surface the platform has (api.md §6; RM-DIST-03).
#
#   CORE_REPO_TOKEN=<gh token> docker compose up --build
#
# Descended from `astro-mine-bench/Dockerfile.leaderboard`, which built one deployable for one
# surface. Four such images was the shape the distribution track removed: the surfaces share every
# convention that matters at the edge — the problem document, the health spelling, CORS, operation
# ids — and shipping them separately meant deciding each of those four times. This builds one, and
# `ASTRO_MINE_API_SURFACES` chooses what it mounts at runtime, so one image covers a Bench-only
# leaderboard, a Hub registry, and everything-at-once without a rebuild.
#
# **The local tier needs none of this** (CX-LOCAL, api.md §6): Hub's tier-1 client, Bench's local
# scoring, Cloud's local backend and Studio's library API all work with no service running. That is
# a requirement, not an accident — a change that makes this image mandatory for a local workflow is
# a defect.
#
# Pin the base image by digest before the repositories flip public (CX-REPRO); the tag is pinned
# here during private incubation, which is also why the private git dependency needs a token.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# git resolves the private `astro-mine-platform` git dependency; drop it after the public/PyPI flip.
# `build-essential` is not needed: the platform publishes a cp312 wheel built by maturin, so the
# Rust safety core arrives compiled rather than being built here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# No extras: unlike the per-component images, every dependency the hosted tier needs is a base
# dependency of this distribution (`pyproject.toml` §dependencies) — fastapi/uvicorn for the edge,
# sqlalchemy/psycopg/pgvector for the stores, pyjwt/httpx for the OIDC and supply-chain verification
# path, and the platform's `bench-leaderboard` + `bench-observability` extras. There is nothing to
# select, because the tier is one deployable.
#
# The held-out seed sets under embargo/ are COPYed in above (they are excluded from the wheel, not
# from the repository) so the leaderboard can disclose them at evaluation time.
RUN --mount=type=secret,id=core_token \
    sh -c 'if [ -f /run/secrets/core_token ]; then \
      git config --global \
        url."https://x-access-token:$(cat /run/secrets/core_token)@github.com/".insteadOf \
        "https://github.com/"; \
    fi' \
    && uv sync --locked --no-dev

EXPOSE 8000

# `make_app()` reads ASTRO_MINE_API_SURFACES to decide what to mount; unset means all of them.
# Bench's store comes from ASTRO_MINE_BENCH_DB, Hub's catalog from its own URL — both set by compose.
#
# NOTE (bench#30): submitted policies execute out-of-process in a seccomp-filtered, resource-capped
# child of this container (SubprocessSandbox), which denies network egress but does NOT isolate the
# filesystem. For a public, internet-facing deployment, mount a container runtime and wire
# ContainerSandbox against the signed, digest-pinned evaluation-runner image instead — see the
# platform's TRUST_BOUNDARY.md §3.
CMD ["uv", "run", "--no-dev", \
     "uvicorn", "--factory", "astro_mine_api._app:make_app", \
     "--host", "0.0.0.0", "--port", "8000"]
