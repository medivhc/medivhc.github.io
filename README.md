# rvd-harness — RVD 2.0 goal-driven harness (Claude Code plugin + platform-agnostic Core)

Separates **"the agent finished a turn"** from **"the goal is verified"**. A persistent
controller decides, from real evidence, whether a goal may close; failures become tracked
open issues with recovery actions instead of a polite failure report that ends the work.

Implements `RVD-2.0-Agent-Harness-Design-v1.1.md`: P0 in full, the core of P1 (constraint
classes, alternative verification paths, coverage matrix, amendment review, no-information-gain
detection) and P2 (external Supervisor: cross-session recovery via `claude -p --resume`,
crash-safe checkpoints, backoff, budgets, human-only authorization).

## What you get

| Layer | Files | Role |
|---|---|---|
| Core (no Claude Code types) | `rvd_core/` | Goal contract + JSON Schema, SQLite state/evidence/issues/audit, state machine, environment probing, evidence runner & sufficiency rules, recovery planner, amendments, `AgentAdapter` + Mock adapter |
| CLI | `scripts/rvd.py`, `bin/rvd` | `init status verify next report issue recover amend cancel resume env doctor hook` (`--json`, stable exit codes) |
| Supervisor (P2) | `rvd_core/supervisor.py`, `rvd supervise …` | Human-authorized run policy, dispatch loop, resume/backoff/budgets, crash recovery, dispatch report |
| Claude Code adapter | `.claude-plugin/`, `hooks/hooks.json`, `skills/`, `agents/` | SessionStart context, Stop / TaskCompleted gates, PreToolUse contract protection, event logging |

## Quick start

```bash
# 1) in your project, write .rvd/goal.yaml (template: templates/goal.yaml, examples/)
python3 /path/to/rvd-harness/scripts/rvd.py init --confirm-hard --by <you>   # human confirms HARD items
# 2) work with Claude Code with the plugin loaded
claude --plugin-dir /path/to/rvd-harness
# 3) anytime
rvd status | rvd verify | rvd report | rvd doctor
# 4) optional, unattended across sessions (human starts it):
rvd supervise init-policy && rvd supervise accept --by <you> && rvd supervise run
```

Inside Claude Code sessions the plugin's `bin/` is on PATH, so `rvd` works directly.
Requirements: Python ≥ 3.11, PyYAML, jsonschema. Verified with Claude Code **2.1.319**.

See `docs/installation.md`, `docs/operations.md`, `docs/design-decisions.md`, and
`docs/ACCEPTANCE.md` (what was actually run, real vs mock, known limitations).
