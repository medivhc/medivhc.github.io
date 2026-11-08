# RVD report — calc-demo (contract v1)

**Global state: `SATISFIED`** — all mandatory requirements verified with fresh evidence

Intent: A small calculator library whose arithmetic is correct and whose edge cases (division by zero, empty mean) follow the spec.


> This report is generated from the evidence store. Agent summaries are not used as evidence.

## Requirements

| ID | Mandatory | Status | Accepts | Evidence / gaps |
|---|---|---|---|---|
| R-CORE | yes | ✅ VERIFIED | unit_test | core: unit_test pass `ev-88655b497ed9` |
| R-EDGE | yes | ✅ VERIFIED | unit_test | edge: unit_test pass `ev-e46ba41649fc` |

## Open issues

None.

## Strategy proposals considered (latest per alternative)

- `core/pytest` (unit_test): feasible; full ['R-CORE'], partial -
- `edge/pytest` (unit_test): feasible; full ['R-EDGE'], partial -

## Supervisor

| run | started | ended | stop reason | policy |
|---|---|---|---|---|
| sup-f16dbe62f857 | 2026-11-08T17:42:21 | 2026-11-08T17:42:36 | satisfied | v1 |

| # | dispatch | issue | mode | session | outcome | cost $ | recovery reason | verification → state |
|---|---|---|---|---|---|---|---|---|
| 1 | dsp-9918afee9e0d | iss-7f847e7d7634 | new | 6bb73a5d-d48 | interrupted: no result line; exit -9;  | 0.000 | new issue work | - → RECOVERING |
| 2 | dsp-8096b712b67a | iss-7f847e7d7634 | resume ←6bb73a5d | 6bb73a5d-d48 | succeeded | 0.041 | previous dispatch interrupted | run-9075ffd641c7 → SATISFIED |


## Next action

```json
{
  "action": "none",
  "reason": "goal satisfied"
}
```

## Recent events

- 2026-11-08T17:42:36.410067+00:00 `verification_run`
- 2026-11-08T17:42:33.002448+00:00 `issue_resolved`
- 2026-11-08T17:42:33.001310+00:00 `verification_run`
- 2026-11-08T17:42:21.137261+00:00 `issue_opened`
- 2026-11-08T17:42:21.135911+00:00 `verification_run`
