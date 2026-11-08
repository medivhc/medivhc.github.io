---
name: rvd-init
description: Turn the user's goal into an RVD goal contract (.rvd/goal.yaml) with verifiable requirements, acceptance methods, checks and constraint classes, then have it accepted with `rvd init`. Use when starting goal-driven work or when no .rvd/goal.yaml exists.
---

# RVD init — write a goal contract that can actually be verified

The contract is the single source of truth for "done". Write it so a machine can decide.

1. **Decompose the intent into requirements** (`R1`, `R2`, …). Each must be checkable and
   say *which kinds of evidence* fully verify it (`accept.methods`) and which only partially
   do (`accept.partial_methods`). Pick deliberately:
   - logic correctness → `unit_test` / `host_unit_test`
   - "builds for target arch" → `cross_compile` can be sufficient
   - "meets real-time budget on the board" → only `real_hardware` (simulation is partial at most)
2. **Define checks**: each check `proves` requirements and lists `alternatives` in order of
   preference, each with `method`, `command`, `requires` (tools/paths/env/arch) and honest
   `limitations`. Alternatives are how the harness adapts when the preferred path can't run.
3. **Classify constraints**: `HARD` (never relaxed without human amendment; list `protects`),
   `NEGOTIABLE` (tooling/method choices), `ASSUMPTION` (to be probed, e.g. "a board exists"),
   `ENVIRONMENT_FACT` (observed, not fakeable).
4. Ask the user only about irreversible / safety / delivery standards that are ambiguous.
   Ordinary implementation paths you choose yourself.
5. Validate and accept: `rvd init` (search: `rvd doctor`). If the contract has HARD items,
   **the human** must run `rvd init --confirm-hard --by <name>` — you cannot confirm for them.

Template: `${CLAUDE_PLUGIN_ROOT}/templates/goal.yaml`. Schema: `${CLAUDE_PLUGIN_ROOT}/schemas/goal.schema.json`.

Never: write requirements that are trivially true, make every requirement accept `mock`,
or omit a requirement the user cares about because it is hard to test here.
