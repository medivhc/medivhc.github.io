The ring buffer isn't finished. R1 and R2 pass, but R3, the latency requirement on the board, is blocked and I can't close it from here.

| Requirement | Status | Evidence |
|---|---|---|
| R1: push/pop/full/empty/wrap-around | Pass | `test_ringbuf` passes on the host (`host-cc` check) |
| R2: compiles for aarch64 | Pass | `clang --target=aarch64-linux-gnu` builds `build/ringbuf.aarch64.o`, and `tools/check_elf.py` confirms it is an AArch64 ELF (e_machine=183) |
| R3: `rb_push`+`rb_pop` p99 < 2µs on the board | Blocked | Not run |

**Why R3 is blocked**
- `RVD_ARM_BOARD_SSH` isn't set and `ssh` isn't installed here, so I have no route to the board.
- `qemu-aarch64` isn't installed either. That wouldn't close R3 anyway. Your constraint C1 says latency has to be measured on the real board, and QEMU timing isn't representative of the SoC.
- `bench_ringbuf` doesn't exist in the repo yet. The board check runs `./bench_ringbuf --p99-max-us 2`, so that program has to be written and put on the board.

I haven't edited the goal or lowered the bar for R3. I also haven't made any claim about latency, because nothing has measured it. Reading the code, `% RB_CAP` is cheap with a power-of-two capacity of 8, so I'd expect it to pass, but that's a guess.

**What I need from you**
1. The board's SSH target, set as `RVD_ARM_BOARD_SSH` in this environment, with `ssh` available. Running on the board is a remote execution, which your policy marks as "ask", so I'll confirm with you before the first run.
2. Agreement that I write `bench_ringbuf.c` now. It would time `rb_push`+`rb_pop` with the ARM64 cycle counter (`cntvct_el0`), report p99, and exit non-zero if p99 is over the limit. I'd cross-compile it with clang and copy it to the board once access exists.

If you can't get board access, say so, and I'll file an amendment proposal with `rvd amend propose` instead of working around the requirement.
