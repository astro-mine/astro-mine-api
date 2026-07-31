"""Test helpers: build a valid ScenarioSpec and synthetic metric traces, with overrides.

Also the hosted-tier fixtures bench#29/#30 need: a **real** RSA keypair + JWKS + minted RS256
tokens (so the OIDC verifier is exercised with real crypto and no secret is ever committed), and a
fast in-process :class:`Sandbox` double for the leaderboard wiring tests — the *real*
:class:`~astro_mine.bench.sandbox.SubprocessSandbox` is exercised by ``tests/test_sandbox.py`` and
by the end-to-end hosted test, but paying a fresh interpreter per seed in every hosted test would
dominate the suite.

Not measured for coverage (coverage source is ``src/astro_mine``); kept out of ``tests`` as a
private module so it is not collected as a test.
"""

from __future__ import annotations

import functools
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astro_mine.bench.baseline import BaselinePolicy
from astro_mine.bench.metrics import MetricValue
from astro_mine.bench.scenario import (
    ContentPins,
    ContentRef,
    EpisodeSpec,
    MetricRef,
    ScenarioSpec,
    SeedSet,
)
from astro_mine.core.messages import (
    Action,
    ActionBatch,
    CommsObservationMask,
    ModeCommand,
    Observation,
    SensorReading,
    StateSample,
)
from astro_mine.core.messages.enums import ActionKind
from astro_mine.core.messages.model import Quat, Transform, Vec3
from astro_mine.core.objective import MetricAggregation, MetricDirection
from astro_mine.core.resource import FieldDistribution
from astro_mine.core.scoring import BeliefSnapshot, EpisodeTrace, ScoringContext
from astro_mine.core.units import MOON_BODY_FIXED


def sha256_of(marker: str) -> str:
    """A well-formed ``sha256:`` digest keyed by a single hex marker char (for distinct pins)."""
    return "sha256:" + (marker * 64)


def make_scenario_spec(**overrides: Any) -> ScenarioSpec:
    """A valid anchor-shaped ScenarioSpec; pass overrides to vary a single field."""
    data: dict[str, Any] = {
        "scenario_id": "lunar-polar-ice-prospecting-v1",
        "name": "Lunar Polar Water-Ice Prospecting v1",
        "core_interface": {"env": "0.1.0", "messages": "0.1.0"},
        "content": ContentPins(
            world=ContentRef(id="shackleton-v1", content_hash=sha256_of("a")),
            fleet=(
                ContentRef(id="astro-mine.fleet.prospecting-rover", content_hash=sha256_of("b")),
            ),
            prospect=(ContentRef(id="ice-prior-v1", content_hash=sha256_of("c")),),
        ),
        "seeds": SeedSet(public=(1, 2, 3)),
        "episode": EpisodeSpec(horizon_steps=10_000),
        "metrics": (MetricRef(name="water_mass"),),
    }
    data.update(overrides)
    return ScenarioSpec(**data)


# --- synthetic metric-trace factories (RM-P0-BENCH-03) --------------------------------

_IDENTITY_POSE = Transform(
    translation_m=Vec3(x=0.0, y=0.0, z=0.0),
    rotation_quat_xyzw=Quat(x=0.0, y=0.0, z=0.0, w=1.0),
)


def make_observation(
    tick: int,
    sim_time_s: float,
    agent_id: str = "rover",
    *,
    battery_soc_j: float | None = None,
    temperature_k: float | None = None,
    water_kg: float | None = None,
    detection: float | None = None,
    detection_species: str = "water",
    earth_contact: bool | None = None,
) -> Observation:
    """A synthetic per-tick observation with only the channels a test needs populated."""
    sensors: list[SensorReading] = []
    if water_kg is not None:
        sensors.append(
            SensorReading(
                sensor="isru_tank", values=[water_kg], unit="kg", resource_species="water"
            )
        )
    if detection is not None:
        sensors.append(
            SensorReading(sensor="neutron", values=[detection], resource_species=detection_species)
        )
    comms = (
        None
        if earth_contact is None
        else CommsObservationMask(agent_id=agent_id, earth_contact=earth_contact)
    )
    state = StateSample(
        agent_id=agent_id,
        frame=MOON_BODY_FIXED,
        pose=_IDENTITY_POSE,
        battery_soc_j=battery_soc_j,
        temperature_k=temperature_k,
    )
    return Observation(
        tick=tick,
        sim_time_s=sim_time_s,
        agent_id=agent_id,
        self_state=state,
        sensors=sensors,
        comms=comms,
    )


def belief(mean: float, variance: float) -> FieldDistribution:
    """A per-cell belief distribution (uncertainty-first mean + variance)."""
    return FieldDistribution(mean=mean, variance=variance, species="water", unit="kg")


def belief_snapshot(sim_time_s: float, cells: Mapping[str, FieldDistribution]) -> BeliefSnapshot:
    """A belief snapshot at ``sim_time_s`` over the given per-cell distributions."""
    return BeliefSnapshot(sim_time_s=sim_time_s, cells=dict(cells))


def make_trace(
    observations: Iterable[Observation] = (),
    context: ScoringContext | None = None,
) -> EpisodeTrace:
    """An episode trace from an observation sequence and (optional) scoring context."""
    return EpisodeTrace(observations=tuple(observations), context=context or ScoringContext())


# --- a community metric plugin for Hub-discovery tests (RM-P1-BENCH-12) ---------------


@dataclass(frozen=True, slots=True)
class EarthContactUptime:
    """A community metric NOT in the reference set: the fraction of ticks with an Earth contact.

    A genuinely new measure of "good" (higher-better, dimensionless) authored outside Bench, used to
    prove a Hub-published metric plugin scores a trace with **no change to the built-in registry**
    (bench.md §3). Deterministic: same trace ⇒ same value.
    """

    name: str = "earth_contact_uptime"
    version: str = "0.1.0"
    unit: str = "fraction"
    direction: MetricDirection = MetricDirection.HIGHER_BETTER
    aggregation: MetricAggregation = MetricAggregation.MEAN

    def compute(self, trace: EpisodeTrace) -> MetricValue:
        contacts = [obs.comms.earth_contact for obs in trace.observations if obs.comms is not None]
        if not contacts:
            return MetricValue(value=None, unit=self.unit)  # not applicable: no comms observations
        return MetricValue(value=sum(1 for c in contacts if c) / len(contacts), unit=self.unit)


#: A module-level community-metric *instance* — the ``module:attribute`` entrypoint a metric-plugin
#: manifest points at (``tests.bench._factories:COMMUNITY_METRIC``), materialized by the
#: reference loader.
COMMUNITY_METRIC = EarthContactUptime()


# --- importable policies for leaderboard policy_ref resolution (RM-P0-BENCH-06) -------

#: A module-level Policy *instance* (the "instance" branch of ``resolve_policy``).
BASELINE_INSTANCE = BaselinePolicy()


def idle_baseline() -> BaselinePolicy:
    """A zero-arg Policy *factory* (the "factory" branch of ``resolve_policy``)."""
    return BaselinePolicy(mode="idle")


class NondeterministicPolicy:
    """A stateful policy whose action stream drifts across calls — trips the integrity check.

    Used as a Hub-submission entrypoint (``tests.bench._factories:NondeterministicPolicy``)
    to exercise
    provenance re-execution flagging a non-reproducible submission (RM-P1-BENCH-10; bench.md §9).

    Note it drifts *across process boundaries too*: the sandboxed worker re-imports it per seed, so
    the drift now comes from the per-seed tick count rather than shared in-process state — which is
    the point. A submission is no longer able to keep state between the scoring run and the
    re-execution audit, because they are different processes.
    """

    def __init__(self) -> None:
        self._calls = 0

    def decide(self, observations: object, context: object) -> ActionBatch:
        self._calls += 1
        # Wall-clock, so the action stream differs between the scoring run and the re-execution
        # audit even though each runs in a fresh process (bench.md §9: non-determinism is flagged).
        drift = f"{self._calls}-{time.perf_counter_ns()}"
        return ActionBatch(
            actions=[Action(agent_id="rover", kind=ActionKind.MODE, mode=ModeCommand(mode=drift))]
        )


class ExplodingPolicy:
    """A submission that raises on its first decision — the sandbox must hand the failure back."""

    def decide(self, observations: object, context: object) -> ActionBatch:
        raise RuntimeError("this submission is broken")


#: The entrypoints used as Hub-submission / policy_ref references across the hosted tests.
BASELINE_REF = "tests.bench._factories:BASELINE_INSTANCE"
NONDETERMINISTIC_REF = "tests.bench._factories:NondeterministicPolicy"
EXPLODING_REF = "tests.bench._factories:ExplodingPolicy"

#: The repo root — the import root a sandboxed worker needs to resolve
#: ``tests.bench._factories:…``. The
#: sandbox scrubs the environment, so this is passed explicitly rather than inherited.
REPO_ROOT = str(Path(__file__).resolve().parents[2])

#: The ``tests/`` directory as a *narrow* import root: with it on the path a hostile policy is a
#: top-level module (``_policies_hostile:…``) reachable without granting the repo root — so the
#: embargo directory, a sibling of ``tests/``, stays outside a confined worker's Landlock allowlist.
TESTS_DIR = str(Path(__file__).resolve().parent)


@functools.cache
def sandbox_enforceable() -> bool:
    """Whether this host can run the **confined** :class:`SubprocessSandbox` for real.

    That needs Linux with a seccomp egress filter *and* a filesystem the Landlock allowlist can
    actually confine. The second condition is the subtle one: on a 9p/drvfs mount — a WSL checkout
    under ``/mnt/…`` — Landlock denies even the paths it was told to grant, so a confined worker
    cannot read the package it must import and never starts. There the real-sandbox tests skip; CI
    (on a native filesystem) exercises them.

    The probe restricts a throwaway subprocess to the directory holding Bench's own installed code
    and checks it can still read a module there. That is deliberately *not* the interpreter: in a
    venv the ``python`` symlink resolves onto the native system prefix even when the package (and
    everything the worker imports) lives on a 9p mount, so probing the interpreter would report a
    false ``True``. The package's own file shares the filesystem a real submission imports from.
    """
    from astro_mine.bench.sandbox import egress_filter_supported, landlock_supported

    if not egress_filter_supported() or not landlock_supported():
        return False
    probe = (
        "import os;"
        "import astro_mine.bench.sandbox._landlock as m;"
        "from astro_mine.bench.sandbox import restrict_filesystem;"
        "f=os.path.realpath(m.__file__);"
        "restrict_filesystem([os.path.dirname(f)],[]);"
        "open(f,'rb').read(1)"
    )
    try:
        completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, timeout=30.0)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


# --- OIDC: a real keypair, a real JWKS, real RS256 tokens (bench#29) -------------------

_KEY_ID = "astro-mine-bench-test-key"


@dataclass(frozen=True, slots=True)
class TestIdp:
    """A throwaway IdP for the tests: a freshly-generated RSA key, its JWKS, and a token minter.

    The key is generated **per test run** and never committed — the OIDC verifier is exercised
    against real RS256 signatures rather than a stubbed-out verifier, and there is no fixture secret
    in the repo to leak (conventions.md §9: no secrets in images or repos).
    """

    issuer: str
    audience: str
    private_pem: bytes
    jwks: dict[str, Any]

    def token(
        self,
        *,
        subject: str = "lab-1",
        roles: Sequence[str] = ("submitter",),
        expires_in: int = 3600,
        audience: str | None = None,
        issuer: str | None = None,
        email: str | None = None,
    ) -> str:
        """Mint a signed RS256 bearer token; override a claim to test the verifier's rejections."""
        import jwt

        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": subject,
            "iss": issuer or self.issuer,
            "aud": audience or self.audience,
            "iat": now,
            "nbf": now - 1,
            "exp": now + expires_in,
            "roles": list(roles),
            "scope": "openid profile",
        }
        if email is not None:
            claims["email"] = email
        return jwt.encode(claims, self.private_pem, algorithm="RS256", headers={"kid": _KEY_ID})

    def header(self, **kwargs: Any) -> dict[str, str]:
        """The ``Authorization`` header for a freshly-minted token."""
        return {"Authorization": f"Bearer {self.token(**kwargs)}"}


def make_idp(
    *, issuer: str = "https://idp.test/realms/astro-mine", audience: str = "astro-mine-bench"
) -> TestIdp:
    """Generate a throwaway RSA key and publish it as a JWKS the OIDC verifier can consume."""
    import json

    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk: dict[str, Any] = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": _KEY_ID, "use": "sig", "alg": "RS256"})
    return TestIdp(issuer=issuer, audience=audience, private_pem=private_pem, jwks={"keys": [jwk]})


# --- a fast in-process Sandbox double (bench#30) ---------------------------------------


class InProcessSandbox:
    """A **test double** that runs the eval worker in-process — no isolation, just speed.

    The leaderboard's *wiring* (auth → verify → score → re-execute → rank) is the same whichever
    backend runs the seeds, and paying a fresh interpreter per seed in every hosted test would
    dominate the suite. So most hosted tests inject this, while the isolation properties it does
    **not** provide — no egress, rlimits, timeouts, the environment scrub — are what
    ``tests/test_sandbox.py`` asserts against the real
    :class:`~astro_mine.bench.sandbox.SubprocessSandbox`, and one hosted test drives the real one
    end-to-end.

    It goes through the *same* ``result.json`` hand-back channel as the real sandbox, so the
    evaluator-side parsing is exercised identically. It lives in ``tests/`` and is deliberately not
    shipped: the package must not offer an easy way to un-sandbox a submission.
    """

    def __init__(self, limits: Any = None) -> None:
        from astro_mine.bench.sandbox import DEFAULT_LIMITS

        self._limits = limits if limits is not None else DEFAULT_LIMITS
        #: Every invocation this double has been asked to run — so a test can assert the leaderboard
        #: really did dispatch through the sandbox seam rather than importing the policy itself.
        self.invocations: list[Any] = []

    @property
    def limits(self) -> Any:
        return self._limits

    def run(self, invocation: Any) -> Any:
        import tempfile

        from astro_mine.bench.eval import run_worker
        from astro_mine.bench.sandbox import (
            WORKER_RESULT,
            ResourceUsage,
            SandboxOutcome,
            SandboxStatus,
            read_worker_result,
        )

        self.invocations.append(invocation)
        with tempfile.TemporaryDirectory() as workdir:
            code = run_worker(
                [
                    "--scenario-id",
                    invocation.scenario_id,
                    "--policy-ref",
                    invocation.policy_ref,
                    "--seed",
                    str(invocation.seed),
                    "--output-dir",
                    workdir,
                    "--emit",
                    "json",
                ],
                env={},
            )
            result = read_worker_result(Path(workdir) / WORKER_RESULT)
        status = SandboxStatus.OK if code == 0 else SandboxStatus.FAILED
        return SandboxOutcome(
            status=status,
            invocation_seed=invocation.seed,
            result=result,
            exit_code=code,
            usage=ResourceUsage(wall_seconds=0.0),
            detail=None if result is None else result.error,
        )
