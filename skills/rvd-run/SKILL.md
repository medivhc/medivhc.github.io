---
name: rvd-run
description: Drive an RVD goal to SATISFIED by working the open-issue queue — implement, verify with `rvd verify`, and keep going on failure. Use whenever a project has .rvd/goal.yaml and you are doing work toward it, or when the RVD Stop gate says the goal is not satisfied.
---

# RVD run — the loop

The goal is done only when `rvd verify` says `SATISFIED`. Your summary, a failure table,
or "tests ran" is not completion.

Loop:
1. `rvd next --json` → the next permitted action (`verify`, `recover`, `escalate`, `await_resource`).
2. `recover`: read the failing evidence log named in the issue, form a root-cause hypothesis,
   make the **minimal** fix in product code, then `rvd verify --check <id>`; finish with full
   `rvd verify` to catch regressions.
3. `verify`: run `rvd verify`.
4. Repeat. If ten tests fail with one root cause, fix the cause once.
5. `escalate` (no information gain): don't retry the same thing. Change the decomposition,
   gather new diagnostics, or try a different verification path; then
   `rvd recover <issue> --reset --strategy "<what is genuinely different>"`.
6. `await_resource`: the remaining gap needs hardware/permission/resources. Do everything
   that *is* possible (cross-compile, simulate, mock), then stop and report the gap honestly.

Hard rules:
- Never edit `.rvd/goal.yaml`, tests' acceptance logic, or evidence to make things green.
  If the contract is wrong, `rvd amend propose <new.yaml> --reason ...` (humans approve HARD changes).
- Never claim a requirement that needs `real_hardware` is done because simulation passed.
- When reporting, quote `rvd report` (generated from evidence), and list what is NOT verified.
