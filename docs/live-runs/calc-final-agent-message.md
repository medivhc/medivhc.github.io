I fixed the two edge-case bugs in `calc.py`, and the RVD goal `calc-demo` is now SATISFIED.

- **`div`:** it now raises `ValueError("division by zero")` when `b == 0`.
- **`mean`:** it now returns `None` for an empty list.

I didn't touch the tests or `.rvd/goal.yaml`. `rvd verify` ran all checks (run `run-662ea1737e0b`): `core` and `edge` both pass, so there are no regressions. I didn't re-run plain `pytest` after the fix, but `rvd verify` uses pytest for both checks.
