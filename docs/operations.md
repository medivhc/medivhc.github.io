# Operations

## Daily loop
| Command | Purpose | Exit |
|---|---|---|
| `rvd status [--json]` | state, per-requirement status + gaps, open issues, next action | 0 |
| `rvd verify [--check ID]` | run checks (first feasible alternative per check), record evidence, decide | 0 SATISFIED / 10 not |
| `rvd next --json` | the next permitted action (`verify`, `recover`, `escalate`, `await_resource`, `none`) | 0 |
| `rvd report [--out F]` | markdown report from the evidence store (default `.rvd/reports/latest.md`) | 0 |
| `rvd issue list` | open/blocked/exhausted issues | 0 |
| `rvd recover ID [--reset --strategy "..."]` | inspect an issue; after exhaustion, record a *new* strategy to re-open it | 0 / 4 |
| `rvd amend propose FILE --reason R` | propose contract v+1; verification-only changes are auto-reviewed for coverage | 0 |
| `rvd amend approve ID --by NAME --role owner` | human approval (refused inside agent sessions, refused for the proposer) | 0 / 4 |
| `rvd cancel / resume --by NAME` | user controls; automation never resumes a cancelled task | 0 / 4 |
| `rvd env` | environment capability snapshot + fingerprint | 0 |
| `rvd doctor` | deps, hooks.json, enablement (`disableAllHooks`), DB integrity, hook heartbeat | 0 / 10 |

Exit codes: 0 ok · 1 internal error · 2 usage · 3 not initialized · 4 permission denied ·
5 invalid contract · 10 goal not satisfied / doctor found problems.

## Reading the states
- **Requirement**: `UNVERIFIED`, `IN_PROGRESS`, `VERIFIED`, `PARTIALLY_VERIFIED` (only partial-method
  evidence such as simulation), `FAILED`, `BLOCKED` (no runnable way to get accepted evidence here),
  `STALE` (code, contract or the evidence log changed since the evidence was taken).
- **Global**: `NEW → READY → (RUNNING) → VERIFYING → SATISFIED | RECOVERING | BLOCKED | PAUSED_INCOMPLETE`,
  plus `CANCELLED` (user) and `ERROR` (harness fault — not a verdict on the project).

## Budgets (goal.yaml `budget`)
- `max_consecutive_no_progress` (default 2): verifications of an issue with the same code + outcome
  before it is `exhausted`; and Stop-hook continuations with no change in between before the session
  is released as `PAUSED_INCOMPLETE`.
- `max_attempts_per_issue` (6), `max_stop_blocks_per_session` (6 — below Claude Code's
  `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` default of 8, so our accounting, not Claude Code's override, ends loops),
  `check_timeout_sec` (300).

## When the gate degrades
Hooks can be disabled (`disableAllHooks`), not loaded (plugin not enabled), time out or crash.
Then there is **no session-level completion gate**: the agent can end a turn freely. The
authoritative state is unaffected (nothing but `rvd verify` can make it SATISFIED) and the next
`SessionStart` re-injects open issues, but you must not assume interception. `rvd doctor` reports
"hook heartbeat … gate is NOT active" when no hook has ever run for the project. A hook that
crashes exits 1 (Claude Code's non-blocking error), prints "DEGRADED", and records `ERROR`.

## Audit
Every transition, claim, block, denial, amendment and drift is an `events` row in `state.db`
(actor, source, payload, contract version). Agent texts are stored as `agent_claim_untrusted`.
