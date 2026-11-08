# Design decisions

1. **Accepted contract lives in the DB, not the file.** `goal.yaml` is the human-editable proposal;
   the version accepted by `rvd init` / approved amendments is stored in `contract_versions`. A goal.yaml
   that differs is reported as drift and ignored (scenario I). Approved amendments write the file back.

2. **Sufficiency is per requirement.** Each requirement lists `accept.methods` (fully proves) and
   `accept.partial_methods`. Evidence closes a requirement only if it passed, its method is accepted,
   its artifact hash equals the current tree, its contract version is current, and its log still matches
   its recorded SHA-256. There is no global score.

3. **Checks carry ordered alternatives.** The runner probes each alternative's `requires`
   (tools/paths/env/arch) and risk, records a strategy proposal for each (feasible or not, coverage,
   gaps), and runs the first feasible one. This is the "controlled workaround": the method may change,
   the requirement may not. Risk `high` needs `--allow-high-risk` (human) to run.

4. **Changing *how* to verify vs *what* must hold.** Contract diffs touching intent, policy, budgets,
   requirements' acceptance/mandatory/predicate, or any HARD constraint need a human owner. Checks-only
   diffs are auto-reviewed: the new checks must still be *able* to fully verify every requirement the
   old checks could (no downgrade to `mock`, no dropping the only real-hardware path).

5. **Issues are derived, not appended.** Each round, open issues are recomputed from requirement
   statuses with a root-cause signature (normalized last exception line, else check id). Same
   signature ⇒ same row (UNIQUE), so replays are idempotent and ten failures with one cause become one
   issue. Vanished signatures are resolved; regressions re-open with a fresh budget.

6. **Information gain, not retry counts.** Each issue keeps a fingerprint of (artifact hash, outcome,
   method, alternative) of its checks. Re-running with identical fingerprint increments `no_progress`;
   at the budget it becomes `exhausted` and `next` says `escalate`. Any code change or new path resets it.
   `rvd recover --reset` requires naming a strategy not tried before.

7. **Stop hook = bounded nudge, not a daemon.** Verified in Claude Code 2.1.319: Stop accepts
   `{"decision":"block","reason":…}`; Claude Code overrides after `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`
   (default 8) consecutive blocks and asks hooks to honour `stop_hook_active`. We deliberately *do* block
   while `stop_hook_active` is true, but only while (a) there is an autonomous next action and (b) the
   session made progress since the previous block, capped at 6 per human turn (reset on
   `UserPromptSubmit`). When nothing actionable remains (`await_resource`/`escalate`) the hook lets the
   turn end with a `systemMessage` stating the goal is NOT complete. Cross-session continuation is P2.

8. **Hooks are fast.** They hash the tree and query SQLite; they never run builds/tests. `rvd verify`
   is the only place checks run, under a DB lease.

9. **Replay protection = same event against same state.** Stop/TaskCompleted payloads are de-duplicated
   within 5 s keyed on (payload, global state, progress fingerprint), so a double delivery doesn't double
   count, but a genuinely changed state is always re-evaluated.

10. **Human-only actions.** `init --confirm-hard`, `amend approve`, `cancel`, `resume` refuse to run when
    `CLAUDECODE=1` (set by Claude Code in tool subprocesses) or `RVD_AGENT_CONTEXT=1`; PreToolUse also
    denies them and any write to `.rvd/goal.yaml|policy.yaml|evidence`, and any access to `state.db`.
    This is defence in depth against an agent *quietly* lowering the bar — not a security boundary
    against a determined local user.

11. **TaskCompleted scoping.** A Claude Code task that mentions requirement IDs may complete only when
    those are VERIFIED (exit 2 otherwise). Tasks naming no requirement follow
    `completion_policy.unreferenced_tasks` (`allow` default, or `block` until the goal is SATISFIED).

12. **Adapters never write authority.** `AgentAdapter` (start/poll/collect/resume/cancel + declared
    capabilities). `MockAdapter` proves hand-off across vendors with `resume=false`;
    `ClaudeCodeHeadlessAdapter` (`claude -p`, `--resume`) is P2 groundwork and untested in CI.

13. **Supervisor authority comes from a separately accepted run policy.** The goal contract says
    what must be true; `.rvd/supervisor.yaml` says what the supervisor may *spend and grant*
    (tools, permission mode, budgets). Only a human can accept it, its hash is pinned, and any
    on-disk change (e.g. a widened tool list) stops the supervisor until re-accepted. Child agent
    processes get a scrubbed environment (no `RVD_ALLOW_AGENT_CONFIRM` / `RVD_STATE_DIR`).

14. **Outcome classes, not prose.** Adapters classify each execution (succeeded / failed /
    interrupted / rate_limited / permission_denied / cancelled / timeout). For Claude Code this uses
    `--output-format stream-json --verbose`: `session_id` is on the first line, so even a SIGKILLed
    session is resumable; the final `result` line gives `is_error`, `subtype`, `api_error_status`,
    `permission_denials`, `total_cost_usd`.

15. **Permission denials end automation for that policy version.** The supervisor never retries a
    denied action with more permissions and never switches adapters to get around it.

16. **Rate limits don't count as failed attempts.** If an execution was rate limited/interrupted
    and the tree is unchanged, the supervisor only refreshes (no verification), so API trouble
    doesn't exhaust the issue's no-progress budget; backoff and `max_api_retries` bound it instead.

17. **Lease takeover is host-local.** A dead holder is detected by PID on the same machine. Running
    two supervisors for one project on different hosts against a shared state dir is unsupported.
