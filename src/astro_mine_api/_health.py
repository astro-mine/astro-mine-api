"""One health endpoint spelling, one health shape (api.md §4; conventions.md §10; api#4).

The four surfaces were ported carrying the spellings they were written with — ``/hub/health``
against ``/studio/healthz``, ``/cloud/healthz``, ``/bench/healthz`` — and three different bodies
between them. ``api.md`` §4 called it *"a visible, low-cost example of what one home for the
decision is for"*, and promised it would converge during the move. This is that convergence:

* **``/healthz`` everywhere.** ``/hub/health`` still answers, byte-identically, and is marked
  deprecated in the OpenAPI document and in its response headers. One cycle, then it goes.
* **One :class:`Health` body**, so a probe, a status page or a generated client reads every surface
  the same way. The deployment's own ``/healthz`` extends it by one field rather than differing.

**Liveness, not readiness.** ``conventions.md`` §10 asks for both; every endpoint here answers the
first — the process is up and this surface is mounted. None of them checks a backend, and none of
them claims to: a readiness probe that reports ``ok`` without asking anything is worse than no
readiness probe, because a deployment will route traffic at it.
"""

from __future__ import annotations

from pydantic import BaseModel

from astro_mine_api import __version__

__all__ = ["Health", "health"]


class Health(BaseModel):
    """Liveness for one surface. The same three fields from every surface, always."""

    #: ``"ok"`` when the surface answers at all. A field rather than a bare 200 so a body that gets
    #: logged, cached or proxied still says what it meant.
    status: str = "ok"
    #: Which surface answered: ``hub``, ``studio``, ``cloud``, ``bench`` — or ``api`` for the
    #: deployment as a whole. What makes one probe result distinguishable from another behind one
    #: address.
    component: str
    #: The version of **this distribution** — the thing actually serving, and the thing that changes
    #: when the tier is deployed. Deliberately not the owning component's ``__version__``: all four
    #: come from one platform wheel and each pins its own interface version, so reporting those
    #: would answer a question nobody asked with a number that never moves.
    version: str


def health(component: str) -> Health:
    """The liveness body for *component*."""
    return Health(component=component, version=__version__)
