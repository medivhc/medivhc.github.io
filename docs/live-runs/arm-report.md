# RVD report — arm-ringbuf (contract v1)

**Global state: `BLOCKED`** — 1 unmet; remaining gaps need external resources or authorization

Intent: Deliver a ring buffer for the sensor ISR of our ARM64 SoC: logically correct, builds for aarch64, and meets the ISR latency budget on the real board.


> This report is generated from the evidence store. Agent summaries are not used as evidence.

## Requirements

| ID | Mandatory | Status | Accepts | Evidence / gaps |
|---|---|---|---|---|
| R1 | yes | ✅ VERIFIED | host_unit_test, simulation, real_hardware | logic: host_unit_test pass `ev-89db9650118f` |
| R2 | yes | ✅ VERIFIED | cross_compile, real_hardware | xbuild: cross_compile pass `ev-8873046163d0` |
| R3 | yes | ⛔ BLOCKED | real_hardware | latency: blocked<br>**gap:** no runnable way to obtain ['real_hardware'] evidence in this environment |

## Open issues

- **iss-81e0a5ce7ebd** [blocked/needs_physical_hardware_validation] R3 needs real_hardware evidence that this environment cannot produce — reqs ['R3'], attempts 0, no-progress 0
  - hypothesis: environment fact, not a code failure: env:RVD_ARM_BOARD_SSH, tool:qemu-aarch64, tool:ssh
  - next: Provide the missing resource (env:RVD_ARM_BOARD_SSH, tool:qemu-aarch64, tool:ssh) or authorize an alternative environment; then `rvd verify`. Lower-tier evidence already gathered is kept but cannot close this requirement.
  - blocked on: env:RVD_ARM_BOARD_SSH, tool:qemu-aarch64, tool:ssh

## Strategy proposals considered (latest per alternative)

- `latency/board-ssh` (real_hardware): not feasible — missing: tool:ssh, env:RVD_ARM_BOARD_SSH; full ['R3'], partial -
- `latency/qemu-user` (simulation): not feasible — missing: tool:qemu-aarch64; full -, partial ['R3']
- `logic/host-cc` (host_unit_test): feasible; full ['R1'], partial -
- `xbuild/gcc-cross` (cross_compile): not feasible — missing: tool:aarch64-linux-gnu-gcc; full ['R2'], partial -
- `xbuild/zig-cc` (cross_compile): not feasible — missing: tool:zig; full ['R2'], partial -
- `xbuild/clang-target` (cross_compile): feasible; full ['R2'], partial -

## Next action

```json
{
  "action": "await_resource",
  "issues": [
    {
      "issue_id": "iss-81e0a5ce7ebd",
      "requirements": [
        "R3"
      ],
      "needs": "env:RVD_ARM_BOARD_SSH, tool:qemu-aarch64, tool:ssh",
      "next_condition": "when env:RVD_ARM_BOARD_SSH, tool:qemu-aarch64, tool:ssh becomes available"
    }
  ]
}
```

## Recent events

- 2026-11-08T15:34:56.763270+00:00 `issue_opened`
- 2026-11-08T15:34:56.763160+00:00 `issue_resolved`
- 2026-11-08T15:34:56.763152+00:00 `issue_resolved`
- 2026-11-08T15:34:56.763139+00:00 `issue_resolved`
- 2026-11-08T15:34:56.761938+00:00 `verification_run`
- 2026-11-08T15:34:43.282374+00:00 `issue_opened`
- 2026-11-08T15:34:43.282351+00:00 `issue_opened`
- 2026-11-08T15:34:43.282297+00:00 `issue_opened`
