"""P2 acceptance: external Supervisor and cross-session recovery.

  1. abnormal session exit -> recovered within the authorized policy & budget, DB intact
  2. cancel never restarts; permission denial never bypassed; rate limits back off with a cap
  3. restart doesn't re-dispatch completed actions; report shows every attempt + reason
  4. a real non-interactive invocation (`claude -p`) — see tests/integration/test_claude_code_live.py
     and test_claude_adapter_* below (fake `claude` binary) for classification.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest
import yaml

from conftest import FIXED_CALC, PLUGIN, clean_env
from rvd_core.adapters import ClaudeCodeHeadlessAdapter, MockAdapter, ShellAdapter
from rvd_core.controller import Controller, PermissionDenied
from rvd_core import supervisor as sv


class FakeTime:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def policy(p, **over):
    pol = {"adapter": "shell", "shell": {"command": "true"}, "max_dispatches": 6, "cost_budget_usd": 5.0,
           "max_api_retries": 4, "backoff_base_sec": 10, "backoff_max_sec": 100, "max_crash_retries": 2,
           "cooldown_sec": 0, "execution_timeout_sec": 600}
    pol.update(over)
    (p.root / ".rvd" / "supervisor.yaml").write_text(yaml.safe_dump(pol))
    c = Controller(p.root)
    v = sv.accept_policy(c, "xiang")
    c.close()
    return v


def run_sup(p, adapter, max_iterations=30, ft=None):
    c = Controller(p.root)
    ft = ft or FakeTime()
    s = sv.Supervisor(c, adapter, clock=ft.clock, sleep=ft.sleep, poll_interval=0.01)
    try:
        return s.run(max_iterations=max_iterations), s, ft
    finally:
        c.close()


def dispatches(p):
    con = sqlite3.connect(p.root / ".rvd" / "state.db")
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("SELECT * FROM dispatches ORDER BY started_at")]


def events(p, kind):
    con = sqlite3.connect(p.root / ".rvd" / "state.db")
    return [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE kind=?", (kind,))]


def seq_worker(*steps):
    """Worker that performs steps[i] on its i-th call. A step is a callable(root, ctx) -> result."""
    calls = {"n": 0}

    def w(root, spec, ctx):
        i = calls["n"]; calls["n"] += 1
        return steps[min(i, len(steps) - 1)](root, ctx)
    w.calls = calls
    return w


def fix(root, ctx):
    (root / "calc.py").write_text(FIXED_CALC)
    return "fixed"


def crash(root, ctx):
    raise RuntimeError("session died: connection reset")


def touch_no_fix(root, ctx):  # real activity, never fixes
    f = root / "calc.py"
    f.write_text(f.read_text() + f"\n# tried {time.time_ns()}\n")
    return "I think it's done"


# ------------------------------------------------------------------ acceptance 1
def test_abnormal_exit_is_resumed_and_fixed(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p)
    a = MockAdapter("mock", seq_worker(crash, fix), resume=True)
    res, s, _ = run_sup(p, a)
    assert res["stop_reason"] == "satisfied" and res["state"] == "SATISFIED"
    d = dispatches(p)
    assert [x["status"] for x in d] == ["interrupted", "succeeded"]
    assert d[1]["mode"] == "resume" and d[1]["resumed_from"] == d[0]["session_id"]
    assert "previous dispatch interrupted" in d[1]["recovery_reason"]
    assert d[1]["state_after"] == "SATISFIED" and d[1]["verify_run_id"].startswith("run-")
    md = p.rvd("report", check=0).stdout
    assert "## Supervisor" in md and "interrupted" in md and "resume" in md


def test_adapter_without_resume_starts_fresh_session_honestly(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p)
    a = MockAdapter("mock", seq_worker(crash, fix), resume=False)
    res, _, _ = run_sup(p, a)
    d = dispatches(p)
    assert res["stop_reason"] == "satisfied"
    assert d[1]["mode"] == "new" and d[1]["resumed_from"] is None and d[0]["session_id"] != d[1]["session_id"]


def test_crash_retries_are_bounded(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p, max_crash_retries=2)
    a = MockAdapter("mock", seq_worker(crash), resume=True)
    res, _, _ = run_sup(p, a)
    assert res["stop_reason"] == "crash_budget" and len(dispatches(p)) == 3
    assert res["state"] != "SATISFIED"


# ------------------------------------------------------------------ acceptance 2: cancel / stop
def test_user_cancel_mid_execution_kills_it_and_never_restarts(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p)

    def cancel_then_wait(root, ctx):
        c = Controller(root); c.cancel("xiang", "changed my mind"); c.close()
        ctx["_cancel"].wait(10)
        return "still working"
    a = MockAdapter("mock", seq_worker(cancel_then_wait, fix), resume=True)
    res, _, _ = run_sup(p, a)
    assert res["stop_reason"] == "cancelled" and res["state"] == "CANCELLED"
    assert [x["status"] for x in dispatches(p)] == ["cancelled"]
    res2, _, _ = run_sup(p, a)                     # a later supervisor run
    assert res2["stop_reason"] == "cancelled" and len(dispatches(p)) == 1
    assert a._worker.calls["n"] == 1


def test_human_stop_flag_halts_and_only_human_clears(make_project, monkeypatch):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p)

    def stop_then_wait(root, ctx):
        c = Controller(root); sv.request_stop(c, "xiang", "pause for review"); c.close()
        ctx["_cancel"].wait(10)
        return "x"
    a = MockAdapter("mock", seq_worker(stop_then_wait, fix))
    res, _, _ = run_sup(p, a)
    assert res["stop_reason"] == "user_stop" and dispatches(p)[0]["status"] == "cancelled"
    with pytest.raises(PermissionDenied):
        run_sup(p, a)                               # paused: must not restart by itself
    c = Controller(p.root)
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(PermissionDenied):
        sv.clear_stop(c, "agent")
    monkeypatch.delenv("CLAUDECODE")
    sv.clear_stop(c, "xiang"); c.close()
    assert run_sup(p, a)[0]["stop_reason"] == "satisfied"


# ------------------------------------------------------------------ acceptance 2: permissions
def test_permission_denial_is_escalated_not_bypassed(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); v1 = policy(p)
    denied = {"status": "permission_denied",
              "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "pip install sympy"}}]}
    a = MockAdapter("mock", seq_worker(lambda r, c: denied, fix))
    res, _, _ = run_sup(p, a)
    assert res["stop_reason"] == "permission_denied"
    assert events(p, "escalate_permission")[0]["denials"][0]["tool_name"] == "Bash"
    res, _, _ = run_sup(p, a)                       # same policy: no new dispatch, no workaround
    assert res["stop_reason"] == "permission_denied" and len(dispatches(p)) == 1
    v2 = policy(p, shell={"command": "true # human widened the grant"})  # explicit human re-authorization
    assert v2["version"] == v1["version"] + 1
    assert run_sup(p, a)[0]["stop_reason"] == "satisfied"


def test_policy_must_be_accepted_by_human_and_cannot_be_silently_widened(make_project, monkeypatch):
    p = make_project("calc-demo")
    c = Controller(p.root)
    with pytest.raises(PermissionDenied, match="no accepted supervisor policy"):
        sv.Supervisor(c, MockAdapter("m", fix))
    c.close()
    policy(p, adapter="claude-code")
    raw = yaml.safe_load((p.root / ".rvd" / "supervisor.yaml").read_text())
    raw["claude"] = {"allowed_tools": "Bash Read Edit Write Glob Grep WebFetch"}   # widened without approval
    (p.root / ".rvd" / "supervisor.yaml").write_text(yaml.safe_dump(raw))
    c = Controller(p.root)
    with pytest.raises(PermissionDenied, match="differs from the accepted policy"):
        sv.Supervisor(c, MockAdapter("m", fix))
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(PermissionDenied):
        sv.accept_policy(c, "agent")
    monkeypatch.delenv("CLAUDECODE")
    raw["claude"]["permission_mode"] = "bypassPermissions"
    (p.root / ".rvd" / "supervisor.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(sv.SupervisorError):
        sv.accept_policy(c, "xiang")
    raw["claude"] = {"extra_args": ["--dangerously-skip-permissions"]}
    (p.root / ".rvd" / "supervisor.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(sv.SupervisorError):
        sv.accept_policy(c, "xiang")
    c.close()


def test_agent_cannot_start_supervisor(make_project, monkeypatch):
    p = make_project("calc-demo"); policy(p)
    assert p.rvd("supervise", "run", env={"CLAUDECODE": "1"}).returncode == 4
    for cmd in ["rvd supervise run", "python3 x/scripts/rvd.py supervise accept --by me",
                "echo 'adapter: shell' > .rvd/supervisor.yaml"]:
        _, out, _ = p.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}})
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
    _, out, _ = p.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                        "tool_input": {"command": "rvd supervise status"}})
    assert out is None


# ------------------------------------------------------------------ acceptance 2: rate limits
def test_rate_limit_exponential_backoff_then_success(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p, backoff_base_sec=10, backoff_max_sec=100)
    rl = lambda r, c: {"status": "rate_limited", "error": "429"}
    a = MockAdapter("mock", seq_worker(rl, rl, fix), resume=True)
    res, _, ft = run_sup(p, a)
    assert res["stop_reason"] == "satisfied"
    assert [e["seconds"] for e in events(p, "backoff_wait")] == [10, 20]
    assert sum(ft.sleeps) >= 30
    d = dispatches(p)
    assert [x["mode"] for x in d] == ["new", "resume", "resume"]
    # API failures without code change don't burn the issue's no-progress budget
    assert all(x["verify_run_id"] == "-" for x in d[:2])


def test_rate_limit_budget_caps_retries(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p, max_api_retries=2, backoff_max_sec=15)
    a = MockAdapter("mock", seq_worker(lambda r, c: {"status": "rate_limited"}), resume=True)
    res, _, _ = run_sup(p, a)
    assert res["stop_reason"] == "rate_limit_budget" and len(dispatches(p)) == 3
    assert [e["seconds"] for e in events(p, "backoff_wait")] == [10, 15]  # capped by backoff_max_sec


# ------------------------------------------------------------------ budgets / no information gain
def test_dispatch_budget(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p, max_dispatches=3)
    res, _, _ = run_sup(p, MockAdapter("mock", seq_worker(touch_no_fix)))
    assert res["stop_reason"] == "dispatch_budget" and len(dispatches(p)) == 3 and res["state"] != "SATISFIED"


def test_cost_budget(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p, cost_budget_usd=5.0)

    def pricey(root, ctx):
        touch_no_fix(root, ctx)
        return {"status": "succeeded", "cost_usd": 3.0}
    res, _, _ = run_sup(p, MockAdapter("mock", seq_worker(pricey)))
    assert res["stop_reason"] == "cost_budget" and len(dispatches(p)) == 2 and res["cost"] == 6.0


def test_no_information_gain_escalates_instead_of_looping(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p, max_dispatches=20)
    a = MockAdapter("mock", seq_worker(lambda r, c: "All done! everything passes"))
    res, _, _ = run_sup(p, a)
    assert res["stop_reason"] == "needs_human" and len(dispatches(p)) <= 3
    assert res["state"] == "PAUSED_INCOMPLETE"


def test_blocked_goal_dispatches_nothing(make_project):
    p = make_project("arm-demo"); p.rvdj("verify"); policy(p)
    a = MockAdapter("mock", seq_worker(fix))
    res, _, _ = run_sup(p, a)
    assert res["stop_reason"] == "blocked" and dispatches(p) == [] and a._worker.calls["n"] == 0


def test_only_one_supervisor_per_project(make_project):
    p = make_project("calc-demo"); policy(p)
    c = Controller(p.root)
    assert c.store.acquire_lease("supervisor", "supervisor:other:1", 60)
    with pytest.raises(sv.SupervisorError, match="lease"):
        sv.Supervisor(c, MockAdapter("m", fix)).run()
    c.close()


# ------------------------------------------------------------------ acceptance 3: idempotent restart
def test_restart_after_crash_before_verification_does_not_redispatch(make_project):
    p = make_project("calc-demo"); p.rvdj("verify"); policy(p)

    class CrashAfterDispatch(sv.Supervisor):
        def _verify(self, why):
            if why == "post-dispatch":
                raise KeyboardInterrupt("supervisor killed after the agent finished")
            return super()._verify(why)
    c = Controller(p.root)
    s = CrashAfterDispatch(c, MockAdapter("mock", seq_worker(fix)), poll_interval=0.01, sleep=lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        s.run()
    c.close()
    d = dispatches(p)
    assert d[0]["status"] == "succeeded" and d[0]["verify_run_id"] is None
    # restart: lost verification is completed; the finished action is NOT dispatched again
    never = MockAdapter("never", seq_worker(lambda r, c: pytest.fail("re-dispatched a completed action")))
    res, _, _ = run_sup(p, never)
    assert res["stop_reason"] == "satisfied" and len(dispatches(p)) == 1
    assert dispatches(p)[0]["state_after"] == "SATISFIED"


WORKER = textwrap.dedent('''
    import pathlib, sys, time
    state = pathlib.Path(sys.argv[1]); state.mkdir(exist_ok=True)
    n = len(list(state.glob("call-*"))); (state / f"call-{n}").write_text(str(n))
    sys.stdin.read()
    if n == 0:
        time.sleep(60)                     # first session: slow; the supervisor gets SIGKILLed meanwhile
        (state / "first-finished").write_text("1")
    else:
        pathlib.Path("calc.py").write_text(open(sys.argv[2]).read())
        print("fixed in a new session")
''')


def test_supervisor_process_killed_mid_dispatch_recovers(make_project, tmp_path):
    """Real processes: SIGKILL the supervisor while its agent runs; restart recovers."""
    p = make_project("calc-demo"); p.rvdj("verify")
    w = tmp_path / "worker.py"; w.write_text(WORKER)
    fixed = tmp_path / "fixed_calc.py"; fixed.write_text(FIXED_CALC)
    st = tmp_path / "wstate"
    policy(p, shell={"command": f"{sys.executable} {w} {st} {fixed}"}, cooldown_sec=0)
    cmd = [sys.executable, str(PLUGIN / "scripts" / "rvd.py"), "supervise", "run", "--poll-interval", "0.2"]
    sup1 = subprocess.Popen(cmd, cwd=p.root, env=clean_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + 30
    while time.time() < deadline:
        d = dispatches(p) if (p.root / ".rvd" / "state.db").exists() else []
        if d and d[0]["child_pid"] and (st / "call-0").exists():
            break
        time.sleep(0.2)
    else:
        sup1.kill(); pytest.fail("first dispatch never started")
    child = dispatches(p)[0]["child_pid"]
    os.kill(sup1.pid, signal.SIGKILL); sup1.wait()
    assert dispatches(p)[0]["status"] == "running"          # checkpoint survived the crash
    os.kill(child, 0)                                       # agent process is still alive (orphan)

    r = subprocess.run(cmd, cwd=p.root, env=clean_env(), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    d = dispatches(p)
    assert d[0]["status"] == "interrupted" and "orphaned by supervisor crash" in d[0]["recovery_reason"]
    assert "agent process terminated" in d[0]["recovery_reason"]
    assert d[1]["status"] == "succeeded" and d[1]["state_after"] == "SATISFIED"
    assert not (st / "first-finished").exists()             # the orphan was really stopped
    con = sqlite3.connect(p.root / ".rvd" / "state.db")
    runs = con.execute("SELECT stop_reason FROM supervisor_runs ORDER BY started_at").fetchall()
    assert [x[0] for x in runs] == ["crashed", "satisfied"]
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ------------------------------------------------------------------ adapters
def test_shell_adapter_exit_code_mapping(tmp_path):
    for code, want in [(0, "succeeded"), (75, "rate_limited"), (77, "permission_denied"), (3, "failed")]:
        a = ShellAdapter(f"cat >/dev/null; exit {code}")
        eid = a.start_task({"project_root": str(tmp_path), "instruction": "x"}, {})
        assert a.poll_or_wait(eid, timeout=10) == want


FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys, time
    open(os.environ["FAKE_CLAUDE_ARGV"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
    mode = os.environ["FAKE_CLAUDE_MODE"]
    sid = "sess-fake-1"
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    if mode == "hang":
        time.sleep(60)
    res = {"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
           "total_cost_usd": 0.05, "permission_denials": [], "result": "done", "api_error_status": None}
    if mode == "denied":
        res["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "curl evil"}}]
    if mode == "ratelimit":
        res.update(is_error=True, subtype="error_during_execution", api_error_status=429, result="API Error: 429")
    if mode == "maxturns":
        res.update(is_error=True, subtype="error_max_turns")
    print(json.dumps(res), flush=True)
''')


@pytest.mark.parametrize("mode,want", [("ok", "succeeded"), ("denied", "permission_denied"),
                                       ("ratelimit", "rate_limited"), ("maxturns", "interrupted")])
def test_claude_adapter_classifies_stream_json(tmp_path, monkeypatch, mode, want):
    fake = tmp_path / "claude"; fake.write_text(FAKE_CLAUDE); fake.chmod(0o755)
    argv_log = tmp_path / "argv.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGV", str(argv_log)); monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("RVD_ALLOW_AGENT_CONFIRM", "1")  # must be scrubbed from the agent's env
    a = ClaudeCodeHeadlessAdapter(PLUGIN, allowed_tools="Read Edit", claude_bin=str(fake))
    eid = a.start_task({"project_root": str(tmp_path), "instruction": "fix"}, {})
    assert a.poll_or_wait(eid, timeout=20) == want
    o = a.collect_artifacts(eid)["outcome"]
    assert o["session_id"] == "sess-fake-1" and o["resumable"]
    argv = json.loads(argv_log.read_text().splitlines()[0])
    assert argv[0] == "-p" and argv[argv.index("--allowedTools") + 1] == "Read Edit"
    assert "--dangerously-skip-permissions" not in argv and "stream-json" in argv
    # resume uses the recorded session id
    r = a.request_resume(eid, "continue")
    a.poll_or_wait(r["execution_id"], timeout=20)
    argv2 = json.loads(argv_log.read_text().splitlines()[1])
    assert argv2[argv2.index("--resume") + 1] == "sess-fake-1"


def test_claude_adapter_killed_session_is_interrupted_and_resumable(tmp_path, monkeypatch):
    fake = tmp_path / "claude"; fake.write_text(FAKE_CLAUDE); fake.chmod(0o755)
    monkeypatch.setenv("FAKE_CLAUDE_ARGV", str(tmp_path / "argv.jsonl")); monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    a = ClaudeCodeHeadlessAdapter(PLUGIN, claude_bin=str(fake))
    eid = a.start_task({"project_root": str(tmp_path), "instruction": "fix"}, {})
    for _ in range(50):
        if a.execution_info(eid)["session_id"]:
            break
        time.sleep(0.1)
    os.killpg(a.execution_info(eid)["pid"], signal.SIGKILL)   # the session dies abnormally
    assert a.poll_or_wait(eid, timeout=10) == "interrupted"
    assert a.collect_artifacts(eid)["outcome"]["session_id"] == "sess-fake-1"


def test_claude_adapter_refuses_bypass():
    with pytest.raises(ValueError):
        ClaudeCodeHeadlessAdapter(PLUGIN, permission_mode="bypassPermissions")
    with pytest.raises(ValueError):
        ClaudeCodeHeadlessAdapter(PLUGIN, extra_args=["--dangerously-skip-permissions"])
