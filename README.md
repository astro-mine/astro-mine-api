# astro-mine-api

**The Astro-Mine REST tier.** Every REST surface the platform serves, as FastAPI route modules
over [`astro-mine-platform`](https://github.com/astro-mine/astro-mine-platform)'s ordinary Python
API — one deployable for the hosted tier.

Design doc: [`architecture/api.md`](https://github.com/astro-mine/docs/blob/main/architecture/api.md) ·
Roadmap: `RM-DIST-03`.

## What is in it

| Surface | Owning component | Routes |
|---|---|---|
| **Hub registry API** | Hub | `/hub/publish` · `/hub/resolve` · `/hub/search` · `/hub/artifacts/{name}/{version}` (+ `/download`) · `/hub/healthz` |
| **Studio API** | Studio | `/studio/intent` · `/studio/studies` · `/studio/studies/comparison` · `/studio/catalog/{assets,worlds,preview/{ref}}` · `/studio/worlds/{ref}` · `/studio/campaigns/publish` · `/studio/campaigns/{ref}` · `/studio/healthz` |
| **Cloud submission edge** | Cloud | `/cloud/jobs` · `/cloud/jobs/compile` · `/cloud/sweeps/{compile,expand}` · `/cloud/workflows/compile` · `/cloud/backends` · `/cloud/healthz` |
| **Bench leaderboard** | Bench | `/bench/submissions` (+ `/hub`, `/{id}`, `/{id}/replay`) · `/bench/scenarios` · `/bench/jobs/{id}` · `/bench/audit` · `/bench/metrics` · `/bench/healthz` |

**Every route is prefixed with its owning component's name.** That is what lets one process serve
any subset of the four surfaces with no collisions: composition is inclusion, not routing. Cloud
and Bench both own a `/jobs` space, and the prefix is the whole of why that is not a problem.

**The library half stays in the platform.** The distinction matters most for Bench, whose
leaderboard is mostly *not* REST: the service layer, SQL, auth, authorization, evaluation,
provenance and audit modules are library code and live in `astro_mine.bench.leaderboard`. Only the
route module is here. The same rule applies everywhere — if it would still make sense with no HTTP
in the picture, it is library code.

**gRPC is not here.** Sim's and Prospect's gRPC services stay with their components: they serve a
component's own contract at high rate, are not a web edge, and have no cross-component conventions
to unify.

## Running it

```bash
uv sync
uv run uvicorn --factory astro_mine_api._app:make_app         # all four surfaces
ASTRO_MINE_API_SURFACES=hub,bench uv run uvicorn --factory astro_mine_api._app:make_app
```

`GET /healthz` answers for the deployment as a whole and names the surfaces it mounted; each surface
answers `GET /<surface>/healthz` with the same body minus that list. `GET /hub/health` — the one
pre-convergence spelling — still answers and is marked deprecated in the document and in its
response headers; it goes after one cycle.

Each surface can also be served alone through its own factory — `astro_mine_api.hub:create_app`,
`astro_mine_api.studio.app:create_app`, `astro_mine_api.cloud.app:create_app`,
`astro_mine_api.bench:create_app` — which is what the route tests drive.

### Configuration

Every surface is wired from the same environment variables its component repository always used:

| Variable | Surface | Meaning |
|---|---|---|
| `ASTRO_MINE_API_SURFACES` | — | comma-separated surfaces to mount (default: all four) |
| `ASTRO_MINE_API_CORS_ORIGINS` | — | comma-separated origins the browser tier may call from (default: `http://localhost:3000`, `http://127.0.0.1:3000`) |
| `HUB_POSTGRES_URL` | Hub | hosted catalog DSN (default: in-memory SQLite) |
| `ASTRO_MINE_HUB_REGISTRY` | Studio, Bench | local OCI-layout registry path |
| `ASTRO_MINE_STUDIO_TRUSTED_KEY` / `_SIGNING_KEY` / `_CACHE` | Studio | verify key, signing key, content cache |
| `ASTRO_MINE_BENCH_DB` / `_OBJECTS` / `_CATALOG_DSN` | Bench | submission store, object store, zoo catalog |
| `ASTRO_MINE_BENCH_SANDBOX_*` | Bench | evaluation-worker resource envelope |

**Cross-origin access.** The front end is a static export, so the browser fetches the bundle from
wherever it is hosted and then calls this API directly — from a different origin. A deployment
serving a browser tier MUST set `ASTRO_MINE_API_CORS_ORIGINS` to the origin the application is
served from, or every call fails at the preflight and the application is inert. The default covers
local development and nothing else; `*` is honoured as an explicit choice for a public read API, and
is safe here only because **credentials are never permitted** — nothing in the browser tier carries a
cookie. A request that sends no `Origin` header is unaffected in every case, so `curl`, the CLI and
server-to-server callers behave exactly as they did before.

A page may send `content-type` and `authorization`. The second is not a contradiction of the
sentence above: CORS *credentials* are cookies and auth state the browser attaches by itself, while
an `Authorization` header a page sets explicitly is an ordinary request header. Bench's
authenticated routes need it, and permitting it leaves `allow_credentials` off.

**The local tier does not need this distribution at all**, and that is a requirement rather than
an accident (CX-LOCAL): Hub's tier-1 client, Bench's local scoring, Cloud's local backend and
Studio's library API all work with no service running. A change that makes the API mandatory for a
local workflow is a defect.

### Seeding a demo deployment

A deployment brought up from nothing is correct and empty: no artifacts, no leaderboard rows, no
catalog. `scripts/seed_demo.py` fills it — offline, with no hosted Hub, no Docker and no IdP:

```bash
uv run python scripts/seed_demo.py --root .demo
```

It publishes and signs a small content set, indexes it, seeds the Studio example campaign, generates
an RSA keypair and writes its **public** half as a JWKS, and scores two policies through the real
`POST /bench/submissions` route — so what it leaves behind is a state the API itself can reach
rather than rows written into a store. It is **idempotent**: a second run against the same root
resolves the same content addresses and mints a fresh bearer token.

It prints the environment to export, then:

```bash
python -m http.server 8081 --directory .demo          # serves jwks.json
uv run uvicorn --factory astro_mine_api._app:make_app --port 8000
```

`.demo/seed.json` carries the same thing machine-readably — the environment block, the published
references and digests, the seeded submission ids, and the bearer token — which is how
[`astro-mine-ui`](https://github.com/astro-mine/astro-mine-ui)'s end-to-end journey suite drives a
real deployment.

**The seed root holds private keys** (the registry's signing key and the token-signing key), both
generated per root and written owner-only. It is scratch state: delete it and the identity goes with
it. Never commit one.

## Not a Python API

Nothing should import this distribution as a library. If code wants what an endpoint does, it
wants the platform function the endpoint calls.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest --cov --cov-report=term-missing --cov-fail-under=95
```

The build runs against `astro-mine-platform` at **`main`, not a released pin** (`api.md` §7,
`conventions.md` §3.1). This tier is the platform's downstream canary: a platform change that
breaks a route should break CI on the commit that makes it, not a release later.

## Provenance of the code

The route modules were **ported, not rewritten** — the same code that ran in `astro-mine-hub`,
`astro-mine-studio`, `astro-mine-cloud` and `astro-mine-bench`, with the import paths rewritten
and the component prefix applied. Request and response models, status codes, error details and
factory signatures are unchanged. The source repositories still hold their copies; nothing was
deleted there.

Two things changed beyond the mechanical rewrite, and both are noted where they live:

- `astro_mine.cloud.artifacts.store.ArtifactStore` moved to `astro_mine.core.artifacts` in the
  consolidation, so that one import points at Core now (`cloud/app.py`).
- The route bodies moved onto an `APIRouter` per surface so a deployment can mount several in one
  process; `create_app()` is the single-surface deployment of the same router.

The Studio *serve composition* (`build_serve_app`, the seam wiring, the UI mount, the startup
banner) came from `astro-mine-studio`'s `astro_mine/studio/cli.py`, where `astro-mine-cli` had
already recorded that it "belongs with the REST surface, wherever that ships". The argparse front
end stayed with the CLI.

The two conventions `api.md` §4 says converge during the move — the health-endpoint spelling and the
error shape — did, in api#4, after the port. See "The error contract" below.

## The error contract

Every refusal from every surface is one **problem document** (RFC 9457,
`application/problem+json`), carrying a stable machine-readable `code` a client branches on and a
human-readable `detail` nobody parses:

```json
{ "code": "admission_rejected", "title": "Admission rejected", "status": 422,
  "detail": "artifact sha256:… is unsigned", "errors": [] }
```

A validation failure is **one object**, not FastAPI's array: the field-level problems ride in
`errors` and the same information is one sentence in `detail`, so the naive thing a browser does
renders words.

The codes live in `astro_mine_api._errors.ErrorCode` and are enumerated in the OpenAPI document, so
a generated client gets them as a type. They are **append-only** — a code is public API the moment
something switches on it. Handlers raise `ApiError(ErrorCode.X, "…")`, never a bare
`HTTPException`; the status comes from the code, so a raise site cannot pair a reason with the wrong
number.

`add_error_handlers()` is installed by every app factory here, exactly as `add_cors()` is, so a
route test drives an app that *fails* like the deployed one.

## Tests

The ~two dozen REST tests the consolidation had to exclude from the platform wheel run here again,
under the routes they exercise (`api.md` §7) — including the halves of `test_report_view.py` and
`test_telemetry.py` that the platform still carries as skips. Coverage is gated at 95%.
