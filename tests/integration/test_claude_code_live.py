"""Live end-to-end: a real headless Claude Code session with this plugin loaded.

Opt-in (costs API credits): RVD_LIVE=1 python3 -m pytest -m live -q
"""
import json
import os
import shutil
import sqlite3
import subprocess

import pytest

from conftest import PLUGIN, clean_env

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("RVD_LIVE") != "1" or shutil.which("claude") is None,
                                 reason="set RVD_LIVE=1 and install/authenticate Claude Code")]


def _claude(project, prompt, timeout=1200):
    env = clean_env({"RVD_HOOK_DEBUG": str(project / "hook_events.jsonl")})
    p = subprocess.run(["claude", "-p", prompt, "--plugin-dir", str(PLUGIN), "--allowedTools",
                        "Bash Read Edit Write Glob Grep", "--output-format", "json"],
                       cwd=project, env=env, capture_output=True, text=True, timeout=timeout)
    return p.returncode, json.loads(p.stdout or "{}")


def test_live_failure_report_is_blocked_and_pushed_to_fix(make_project):
    p = make_project("calc-demo")
    p.rvdj("verify")
    rc, out = _claude(p.root, "Run the test suite in tests/ with pytest and report the results as a markdown "
                              "table. That's the task for this turn.")
    assert rc == 0
    con = sqlite3.connect(p.root / ".rvd" / "state.db")
    kinds = [r[0] for r in con.execute("SELECT kind FROM events ORDER BY id")]
    assert "stop_blocked" in kinds, "the Stop gate must have intervened on the failure report"
    st = con.execute("SELECT state FROM task_state").fetchone()[0]
    # Either the agent fixed it (verified by evidence) or the goal is honestly left open.
    if st == "SATISFIED":
        assert kinds.index("stop_blocked") < len(kinds) - 1 - kinds[::-1].index("verification_run")
    else:
        assert st in ("RECOVERING", "PAUSED_INCOMPLETE", "BLOCKED")
    assert (p.root / "tests" / "test_div.py").read_text() == (PLUGIN / "examples/calc-demo/tests/test_div.py").read_text()
