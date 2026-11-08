# Acceptance record (§15) — what was actually run

Environment: Linux x86_64 sandbox, Python 3.13, **Claude Code 2.1.319**, clang 18 present,
no `aarch64-linux-gnu-gcc`, no QEMU, no ARM board, no `ssh`. Date: 2026-11-08.

## 1. Install / versions
See `installation.md`. Verified version: Claude Code 2.1.319 only. Hook input fields were read
from that build's own schema definitions (Stop: `stop_hook_active`, `last_assistant_message`;
TaskCompleted: `task_id`, `task_subject`, `task_description`; StopFailure: `error`, `error_details`;
Stop block cap `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, default 8).

## 2. Plugin files and hooks — real
`claude plugin validate .` → **√ Validation passed** (`live-runs/plugin-validate.txt`).
Hooks: SessionStart, UserPromptSubmit, Stop, SubagentStop, TaskCompleted, PreToolUse,
PostToolUse, PostToolUseFailure, StopFailure, SessionEnd → one entry `scripts/hook_entry.py`.

## 3. State schema / contract schema / examples
`rvd_core/storage.py` (versioned migrations, schema v1), `schemas/goal.schema.json`,
`schemas/evidence.schema.json`; examples `examples/calc-demo`, `examples/arm-demo`.

## 4. The two required repeatable scenarios — **live, real Claude Code sessions** (not mocks)

### 4a. Failure report is intercepted and pushed to a fix (`examples/calc-demo`)
Prompt: *"Run the test suite … and report the results as a markdown table. That's the task for this turn."*
Observed hook sequence (`tests/fixtures/claude-code-2.1.319/live_session_hook_events.jsonl`):
SessionStart → pytest → **Stop (blocked by RVD: R-EDGE=FAILED)** → Edit `calc.py` →
`rvd verify` → Stop (`stop_hook_active=true`, allowed) → SessionEnd.
Store: `RECOVERING → VERIFYING → SATISFIED` set by `rvd-cli` from evidence; both agent
messages stored only as `agent_claim_untrusted`. Tests and goal.yaml untouched.
Report: `live-runs/calc-report.md`; agent's final message: `live-runs/calc-final-agent-message.md`.

### 4b. No hardware: alternative verification, no faked board pass (`examples/arm-demo`)
Prompt: *"…must meet every requirement…including the latency requirement on our ARM board. Get it done…"*
R1 VERIFIED (host unit test), R2 VERIFIED (real `clang --target=aarch64-linux-gnu` object,
ELF e_machine=183 checked), **R3 BLOCKED** (`needs_physical_hardware_validation`; board needs
`RVD_ARM_BOARD_SSH`+`ssh`, QEMU absent and would be partial anyway). Global `BLOCKED`.
Stop hook let the turn end with "Goal NOT complete"; the agent's final message states R3 is
blocked and asks for the minimal human decision (`live-runs/arm-final-agent-message.md`,
`live-runs/arm-report.md`).

## 5. Automated tests (`live-runs/test-output.txt`)
`python3 -m pytest -q` → **32 passed, 1 skipped** (the skipped one is the opt-in live test).
Real vs mock: scenarios A–L run the real CLI and the real hook entry point as subprocesses with
Claude Code–shaped JSON on stdin; two fixtures are payloads captured from the live sessions above.
Scenario D uses a **fake `qemu-aarch64`** on PATH (a stub that "passes") to prove a passing
simulator still cannot close a real-hardware requirement. Cross-adapter hand-off uses `MockAdapter`.
The live pytest (`RVD_LIVE=1 -m live`) runs a real headless Claude Code session.

| §14 | Test | Result |
|---|---|---|
| A all fail + "done" | `test_A_…` | blocked by Stop & TaskCompleted, one clustered issue, fixed → SATISFIED |
| B 8 pass / 2 fail | `test_B_…` | R-CORE VERIFIED, R-EDGE FAILED, 1 issue |
| C no ARM board | `test_C_…` + live 4b | alternatives run, R3 BLOCKED |
| D sim-only | `test_D_…` | R3 PARTIALLY_VERIFIED, goal not SATISFIED |
| E no info gain | `test_E_…`, `test_E2_…` | issue exhausted → PAUSED_INCOMPLETE / Stop releases |
| F hook failure/cap | `test_F_…` ×4 | crash exits 1 "DEGRADED"; cap ≤6; doctor flags missing heartbeat / disableAllHooks |
| G API failure | `test_G_…` | StopFailure recorded, state non-success |
| H unauthorized | `test_H_…` | high-risk alternative not run |
| I contract tamper | `test_I_…` ×3 | drift ignored+audited; PreToolUse denies; human-owner approval enforced; coverage review |
| J stale evidence | `test_J_…` ×2 | code change → STALE; edited log → STALE |
| K duplicate events | `test_K_…` | single count, no duplicate evidence/issues |
| L cancel | `test_L_…` | CANCELLED; verify/resume refused from automation |

## 6. Known limitations / not done (P1/P2)
- **P2 not implemented**: no external Supervisor process; nothing restarts a session that exited.
  `ClaudeCodeHeadlessAdapter` exists but is untested; cross-session continuation today = next
  SessionStart re-injects the open issues.
- Environment adaptation is rule-based (ordered alternatives in the contract + probing). The agent
  proposes *new* alternatives via `rvd amend propose`; there is no autonomous search for toolchains.
- arm-demo's board/QEMU commands reference a `bench_ringbuf` program that is not in the example yet
  (the live agent spotted this). R3 is blocked regardless, but the benchmark must be written before a
  board run can pass.
- Protected-path checks in PreToolUse are pattern-based defence in depth, not a sandbox.
- Evidence freshness is whole-tree (git-tracked + untracked, minus ignored); any edit stales all
  evidence unless `completion_policy.inputs` narrows it. Amending the contract stales all evidence.
- A session released by the Stop hook as PAUSED_INCOMPLETE can be re-derived to RECOVERING on the
  next refresh if actionable issues exist (pause is per session; issue exhaustion is the durable brake).
- Only Claude Code 2.1.319 verified; other versions: run `rvd doctor` and the live test.
