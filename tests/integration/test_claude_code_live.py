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


def test_live_supervisor_resumes_killed_claude_session(make_project):
    """P2 acceptance: a real Claude Code session exits abnormally (SIGKILL); the external
    supervisor, within its accepted policy, resumes that same session with `claude -p --resume`."""
    import signal
    import time
    import yaml
    p = make_project("calc-demo")
    p.rvdj("verify")
    (p.root / ".rvd" / "supervisor.yaml").write_text(yaml.safe_dump({
        "adapter": "claude-code", "claude": {"allowed_tools": "Bash Read Edit Write Glob Grep", "max_turns": 40},
        "max_dispatches": 4, "cost_budget_usd": 3.0, "execution_timeout_sec": 900, "cooldown_sec": 0}))
    p.rvd("supervise", "accept", "--by", "xiang", check=0)
    sup = subprocess.Popen([os.sys.executable, str(PLUGIN / "scripts" / "rvd.py"), "supervise", "run",
                            "--poll-interval", "1"], cwd=p.root, env=clean_env(),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    db = p.root / ".rvd" / "state.db"
    killed = None
    deadline = time.time() + 120
    while time.time() < deadline and killed is None:
        con = sqlite3.connect(db)
        row = con.execute("SELECT child_pid, session_id FROM dispatches ORDER BY started_at LIMIT 1").fetchone()
        con.close()
        if row and row[0] and row[1]:
            time.sleep(2)
            os.killpg(row[0], signal.SIGKILL)  # the Claude Code session dies abnormally
            killed = row
        time.sleep(0.5)
    assert killed, "supervisor never started a Claude session"
    out, err = sup.communicate(timeout=1200)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    d = [dict(r) for r in con.execute("SELECT * FROM dispatches ORDER BY started_at")]
    assert d[0]["status"] == "interrupted" and d[0]["session_id"] == killed[1]
    assert d[1]["mode"] == "resume" and d[1]["resumed_from"] == killed[1], (out, err, d)
    assert con.execute("SELECT state FROM task_state").fetchone()[0] == "SATISFIED", (out, err)
    assert sup.returncode == 0
