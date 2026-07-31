# `embargo/` — sealed held-out benchmark seeds (do **not** ship, do **not** disclose)

This directory holds the **embargoed held-out seed sets** for zoo scenarios — the anti-gaming
reserve (`docs/architecture/bench.md §9`). One subdirectory per scenario id, each containing a sealed
`heldout_seeds.json` of the form:

```json
{ "salt": "<hex>", "seeds": [ <ints> ] }
```

## Why it lives outside `src/`

The packaged wheel/zoo artifact ships **only** `src/astro_mine`
(`pyproject.toml [tool.hatch.build.targets.wheel]`), so nothing here reaches a `pip install` or a
published zoo OCI artifact. The public `ScenarioSpec` in the zoo carries only a **commitment** —
`seeds.heldout_commit`, a `sha256:` over the sealed `{salt, seeds}` payload — which binds the held-out
seeds into the scenario's content hash **without disclosing them**. Evaluation discloses the seeds
only at scoring time (`bench.md §5, §9`).

## Verify a commitment

```bash
python scripts/seal_heldout_seeds.py <scenario-id>
python scripts/seal_heldout_seeds.py <scenario-id> --verify sha256:<hex>
```

`tests/test_zoo_anchor.py` also asserts the anchor's sealed payload matches its published
`heldout_commit`.

## SECURITY — rotate before the repo flips public

These seeds are committed to the **private** incubation repo for CI verifiability only. Phase-0 has no
encrypted, eval-time-only disclosure yet (a CX-SEC follow-up). **Before the repo is made public,
rotate every held-out set here** and re-publish the affected scenarios as new versions (a new
`heldout_commit` ⇒ a new scenario version; old leaderboards remain valid for their pinned spec).
