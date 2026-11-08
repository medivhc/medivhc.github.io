# RVD report — calc-demo (contract v1)

**Global state: `SATISFIED`** — all mandatory requirements verified with fresh evidence

Intent: A small calculator library whose arithmetic is correct and whose edge cases (division by zero, empty mean) follow the spec.


> This report is generated from the evidence store. Agent summaries are not used as evidence.

## Requirements

| ID | Mandatory | Status | Accepts | Evidence / gaps |
|---|---|---|---|---|
| R-CORE | yes | ✅ VERIFIED | unit_test | core: unit_test pass `ev-859cb6697b46` |
| R-EDGE | yes | ✅ VERIFIED | unit_test | edge: unit_test pass `ev-f504e589da85` |

## Open issues

None.

## Strategy proposals considered (latest per alternative)

- `core/pytest` (unit_test): feasible; full ['R-CORE'], partial -
- `edge/pytest` (unit_test): feasible; full ['R-EDGE'], partial -

## Next action

```json
{
  "action": "none",
  "reason": "goal satisfied"
}
```

## Recent events

- 2026-11-08T15:34:10.353374+00:00 `issue_resolved`
- 2026-11-08T15:34:10.352284+00:00 `verification_run`
- 2026-11-08T15:34:01.752486+00:00 `stop_blocked`
- 2026-11-08T15:33:43.105759+00:00 `issue_opened`
- 2026-11-08T15:33:43.104513+00:00 `verification_run`
