---
name: rvd-recover
description: Diagnose an RVD open issue and pick a recovery strategy with real information gain — code fix, alternative tool, alternative verification path, or honest escalation. Use when `rvd next` returns recover/escalate or a check keeps failing.
---

# RVD recover — find a better path, don't lower the bar

`rvd issue list`, then for the chosen issue:

1. **Classify the limitation**: code defect | tool/environment problem | missing hardware |
   verification method broken | agent loop | no feasible path.
2. **Which requirements does it affect?** (`rvd status`). Only those may be touched.
3. **Is the blocker a fact or an assumption?** Probe it (`rvd env`, `which`, try the command).
   "Needs a board to start testing" is an assumption — logic can often be tested on host.
4. **List alternatives and their coverage**: for each, which requirements it can fully or
   partially prove (method vs `accept.methods`), its risk, cost and authorization needs.
   Prefer the option with the most *new information* at low cost, not the easiest green.
   Examples: cross-compile (clang `--target`, zig, gcc cross), QEMU user-mode, mock HAL,
   remote host / CI runner (needs authorization), host-only unit tests for portable logic.
5. **Act**: code fixes directly; a new/changed check goes through
   `rvd amend propose` (verification-only changes are auto-reviewed for coverage; anything
   touching acceptance or HARD constraints waits for a human).
6. **Re-verify** and record what remains: requirements still needing `real_hardware` stay
   open as `needs_physical_hardware_validation` — that is correct, not a failure of yours.

Ask the human only for the minimal decision: e.g. "authorize remote ARM host X" or
"provide board at /dev/ttyUSB0".
