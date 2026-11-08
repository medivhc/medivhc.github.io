#!/usr/bin/env python3
"""Unified Claude Code hook entry: reads the hook JSON on stdin, prints the decision.

Any import/dependency failure exits 1 (a non-blocking hook error in Claude Code) with a
clear message — it never reports success, and `rvd doctor` will flag the degraded gate.
"""
import sys

try:
    import _bootstrap  # noqa: F401
    from rvd_core.hooks import main_stdin
except Exception as e:  # pragma: no cover - exercised by scenario F
    sys.stderr.write(f"RVD hook unavailable ({e!r}); hook-level gate is DEGRADED, goal state unchanged.\n")
    sys.exit(1)

sys.exit(main_stdin())
