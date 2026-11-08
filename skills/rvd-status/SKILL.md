---
name: rvd-status
description: Show what is done, not done, why, what was tried and what's next for the RVD goal in this project, from the evidence store. Use when the user asks about progress/status of goal-driven work or wants the RVD report.
---

# RVD status

Run `rvd status` (or `rvd report` for a full markdown report, written to `.rvd/reports/latest.md`).
Present: global state, each requirement's status with its evidence or gap, open issues
(with attempts / no-progress), and the next action. Explicitly separate VERIFIED from
PARTIALLY_VERIFIED / BLOCKED / UNVERIFIED. Run `rvd doctor` if hooks may not be active —
without hooks there is no session-level completion gate, and you must say so.
