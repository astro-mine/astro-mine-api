# The leaderboard authorization policy (bench#29; bench.md §9).
#
# bench.md §9: "RBAC via OPA for submission quotas, embargo control, and metric/scenario authoring
# rights". This is the Rego an OPA sidecar evaluates, and it is the *same* policy
# `astro_mine.bench.leaderboard.RbacPolicyEngine` evaluates in-process — one set of rules, two
# engines, so a deployment can move policy into OPA (and evolve it there without redeploying Bench)
# without any change in behaviour.
#
# Load it into an OPA sidecar and point Bench at the decision endpoint:
#
#   opa run --server --addr :8181 policy/bench.rego
#   export ASTRO_MINE_BENCH_OPA_URL=http://opa:8181/v1/data/astromine/bench/decision
#
# The input document is `AuthorizationRequest.to_input()`:
#
#   {
#     "principal": {"subject": "...", "issuer": "...", "roles": [...], "scopes": [...]},
#     "action":    "submission:create_hub",
#     "resource":  "acme/prospector:1.0.0",
#     "context":   {"submissions_in_window": 3, "scenario_id": "lunar-polar-ice-prospecting-v1"}
#   }
#
# The result document is `{"allow": bool, "reason": string}`. Bench treats an undefined rule, an
# unreachable OPA, or any response that does not explicitly say `allow: true` as a DENIAL — the
# policy is fail-closed on both sides of the wire.

package astromine.bench

import rego.v1

# --- what each role grants (default-deny: a principal with no known role grants nothing) ----------

role_actions := {
	"submitter": {"submission:create", "submission:create_hub"},
	"maintainer": {
		"submission:create",
		"submission:create_hub",
		"scenario:author",
		"metric:author",
		"embargo:read",
	},
	"admin": {
		"submission:create",
		"submission:create_hub",
		"ranking:mutate",
		"scenario:author",
		"metric:author",
		"embargo:read",
		"audit:read",
	},
}

# Submissions allowed per rate-limit window, per role — the per-user quota (bench.md §9). An admin
# is uncapped, which is why `admin` is absent from this map rather than set to a large number.
role_quota := {
	"submitter": 20,
	"maintainer": 200,
}

# Scenarios whose held-out material is still embargoed. Override with an OPA data document
# (`data.astromine.bench.embargoed_scenarios`) rather than editing this file.
default embargoed_scenarios := []

# --- the derived facts ----------------------------------------------------------------------------

known_roles contains role if {
	some role in input.principal.roles
	role_actions[role]
}

granted contains action if {
	some role in known_roles
	some action in role_actions[role]
}

# The most generous quota the principal's roles carry; undefined (i.e. uncapped) for an admin.
quota := min([q | some role in known_roles; q := role_quota[role]]) if {
	not "admin" in known_roles
	count([q | some role in known_roles; q := role_quota[role]]) > 0
}

scenario := s if {
	s := input.context.scenario_id
} else := input.resource

quota_actions := {"submission:create", "submission:create_hub"}

# --- the denials (each one carries the reason that lands in Bench's audit log) ---------------------

deny contains reason if {
	count(known_roles) == 0
	reason := sprintf("principal %q carries no known role", [input.principal.subject])
}

deny contains reason if {
	count(known_roles) > 0
	not input.action in granted
	reason := sprintf("role(s) %v do not grant %q on %q", [known_roles, input.action, input.resource])
}

deny contains reason if {
	input.action in quota_actions
	used := object.get(input.context, "submissions_in_window", 0)
	used > quota
	reason := sprintf("submission quota exhausted: %d > %d in the current window", [used, quota])
}

deny contains reason if {
	scenario in embargoed_scenarios
	not "embargo:read" in granted
	reason := sprintf("scenario %q is under embargo; embargo:read is required", [scenario])
}

# --- the decision ---------------------------------------------------------------------------------

default allow := false

allow if count(deny) == 0

reason := concat("; ", sort(deny)) if {
	count(deny) > 0
} else := sprintf("%s granted by %v", [input.action, sort(known_roles)])

# The endpoint Bench POSTs to: a single document carrying both fields.
decision := {"allow": allow, "reason": reason}
