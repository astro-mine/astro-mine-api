"""Stable operation ids for the OpenAPI document (api.md §4; api#3).

The front end consumes this API through a **generated** client, which turns every operation id into
a method name verbatim. FastAPI's default ids embed the function name, the path and the method —
``do_search_hub_search_get``, ``artifact_hub_artifacts__name___version__get`` — so the generated
client reads like a stack trace and changes shape whenever a path does.

The rule here is ``<surface>_<endpoint>``: the first static path segment, then the endpoint
function's name. Two properties matter and both are asserted in ``tests/test_openapi_contract.py``:

* **Unique across all four surfaces mounted together.** Uniqueness comes from the endpoint name
  rather than from the HTTP method, because an id that embeds the method (``..._get``) is exactly
  what this replaces. Where one path serves two methods the endpoint names already differ —
  ``list_scenarios`` and ``author_scenario`` on ``/bench/scenarios``.
* **Free of paths and methods**, so renaming a route's URL does not rename a client's method.

An endpoint whose function name reads badly in an id — ``do_search``, ``artifact``,
``compile_sweep_endpoint`` — carries an explicit ``operation_id=`` on its decorator instead. That
is deliberate: renaming the function itself would be a wider change than the document needs, and in
one case (``resolve``) the good name is already taken by an imported symbol in the same module.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

__all__ = ["unique_operation_id"]


def unique_operation_id(route: APIRoute) -> str:
    """The operation id for *route*: ``<surface>_<endpoint>``.

    A route directly under the root — the deployment's own ``/healthz`` — has no surface to name, so
    it is just the endpoint name rather than ``healthz_healthz``.
    """
    static = [segment for segment in route.path.split("/") if segment and "{" not in segment]
    surface = static[0] if static else ""
    if not surface or surface == route.name:
        return route.name
    return f"{surface}_{route.name}"
