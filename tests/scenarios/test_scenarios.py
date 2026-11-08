"""Section 14 end-to-end scenarios A–L, driven through the real CLI and hook entry point.

Hook events are fed as JSON on stdin exactly like Claude Code does (schemas checked against
Claude Code 2.1.319); see tests/integration/test_claude_code_live.py for the live run.
"""
import json
import os
import sqlite3
import stat
import time

from conftest import BROKEN_IMPORT_CALC, FIXED_CALC


def db(p):
    return sqlite3.connect(str(p.root / ".rvd" / "state.db"))


def events(p, kind):
    return [json.loads(r[0]) for r in db(p).execute("SELECT payload_json FROM events WHERE kind=?", (kind,))]


# ---------------------------------------------------------------- A
def test_A_all_fail_and_worker_claims_done_is_blocked_then_fixed(make_project):
    p = make_project("calc-demo")
    p.write("calc.py", BROKEN_IMPORT_CALC)  # every test fails: same root cause
    code, res = p.rvdj("verify")
    assert code == 10 and res["state"] != "SATISFIED"
    assert {r["passed"] for r in res["results"]} == {False}
    s = p.status()
    assert all(r["status"] == "FAILED" for r in s["requirements"])
    # clustered: one root cause (ModuleNotFoundError) -> one issue covering both requirements
    defects = [i for i in s["open_issues"] if i["kind"] == "code_defect"]
    assert len(defects) == 1 and set(defects[0]["requirement_ids"]) == {"R-CORE", "R-EDGE"}

    # Worker writes an honest failure table and declares the round complete.
    rc, out, _ = p.hook({"hook_event_name": "Stop", "stop_hook_active": False,
                         "last_assistant_message": "| test | result |\n|---|---|\n| all 10 | FAIL |\n测试全部失败，但工作已完成。"})
    assert rc == 0 and out["decision"] == "block"
    assert "NOT satisfied" in out["reason"] and "R-EDGE=FAILED" in out["reason"]
    rc, _, err = p.hook({"hook_event_name": "TaskCompleted", "task_id": "1",
                         "task_subject": "Implement R-CORE and R-EDGE"})
    assert rc == 2 and "cannot be marked completed" in err
    # failure evidence and the claim are persisted; claim is stored as untrusted only
    assert db(p).execute("SELECT COUNT(*) FROM evidence WHERE passed=0").fetchone()[0] == 2
    assert any("工作已完成" in e["text"] for e in events(p, "agent_claim_untrusted"))
    assert p.status()["state"] != "SATISFIED"

    # Real fix -> independent verification -> SATISFIED; gates now allow.
    p.write("calc.py", FIXED_CALC)
    code, res = p.rvdj("verify")
    assert code == 0 and res["state"] == "SATISFIED"
    rc, out, _ = p.hook({"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": "fixed"})
    assert rc == 0 and (out is None or out.get("decision") != "block")
    rc, _, _ = p.hook({"hook_event_name": "TaskCompleted", "task_id": "1", "task_subject": "Implement R-CORE and R-EDGE"})
    assert rc == 0
    assert all(i["status"] == "resolved" for i in
               [dict(zip(["status"], r)) for r in db(p).execute("SELECT status FROM issues")])


# ---------------------------------------------------------------- B
def test_B_eight_pass_two_fail_only_verified_part_closes(make_project):
    p = make_project("calc-demo")
    code, res = p.rvdj("verify")
    assert code == 10
    assert p.req("R-CORE") == "VERIFIED" and p.req("R-EDGE") == "FAILED"
    issues = p.status()["open_issues"]
    assert len(issues) == 1 and issues[0]["requirement_ids"] == ["R-EDGE"]
    assert p.status()["next"]["action"] == "recover"
    rc, _, _ = p.hook({"hook_event_name": "TaskCompleted", "task_id": "2", "task_subject": "R-CORE arithmetic"})
    assert rc == 0  # the verified part may be completed
    rc, _, _ = p.hook({"hook_event_name": "TaskCompleted", "task_id": "3", "task_subject": "R-EDGE"})
    assert rc == 2


# ---------------------------------------------------------------- C
def test_C_no_arm_board_runs_alternatives_and_keeps_hw_gap(make_project):
    p = make_project("arm-demo")
    code, res = p.rvdj("verify")
    by = {r["check"]: r for r in res["results"]}
    assert by["logic"]["ran"] and by["logic"]["passed"]
    # alternatives tried in order; first runnable cross toolchain used, others recorded with reasons
    if by["xbuild"]["ran"]:
        assert by["xbuild"]["method"] == "cross_compile" and by["xbuild"]["passed"]
        assert p.req("R2") == "VERIFIED"
    assert not by["latency"]["ran"]
    assert p.req("R1") == "VERIFIED"
    assert p.req("R3") == "BLOCKED"
    s = p.status()
    assert s["state"] in ("BLOCKED", "RECOVERING") and s["state"] != "SATISFIED"
    hw = [i for i in s["open_issues"] if i["kind"] == "needs_physical_hardware_validation"]
    assert hw and hw[0]["requirement_ids"] == ["R3"] and hw[0]["status"] == "blocked"
    rows = db(p).execute("SELECT alternative_id, feasible, reason FROM strategy_proposals WHERE check_id='latency'").fetchall()
    assert {r[0] for r in rows} == {"board-ssh", "qemu-user"} and not any(r[1] for r in rows)
    # Stop is allowed (nothing autonomous left) but says plainly the goal is NOT complete
    rc, out, _ = p.hook({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"})
    assert rc == 0 and "decision" not in (out or {}) and "NOT complete" in out["systemMessage"]
    md = p.rvd("report", check=0).stdout
    assert "R3" in md and "BLOCKED" in md and "needs_physical_hardware_validation" in md


# ---------------------------------------------------------------- D
def _fake_tool(dirpath, name, script):
    f = dirpath / name
    f.write_text("#!/bin/sh\n" + script + "\n")
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


def test_D_simulation_pass_never_closes_real_hardware_requirement(make_project, tmp_path):
    p = make_project("arm-demo")
    bindir = tmp_path / "fakebin"; bindir.mkdir()
    _fake_tool(bindir, "qemu-aarch64", 'echo "simulated p99=0.8us"; exit 0')  # a passing simulator
    env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
    code, res = p.rvdj("verify", env=env)
    lat = next(r for r in res["results"] if r["check"] == "latency")
    assert lat["ran"] and lat["passed"] and lat["method"] == "simulation"
    assert p.req("R3") == "PARTIALLY_VERIFIED"
    s = p.status()
    assert s["state"] != "SATISFIED" and code == 10
    gap = next(r for r in s["requirements"] if r["id"] == "R3")["detail"]["gaps"]
    assert any("real_hardware" in g for g in gap)


# ---------------------------------------------------------------- E
def test_E_repeated_identical_failure_triggers_escalation_not_hot_loop(make_project):
    p = make_project("calc-demo")
    for _ in range(3):  # same code, same failure, no new information
        p.rvdj("verify")
    s = p.status()
    iss = next(i for i in s["open_issues"] if i["kind"] == "code_defect")
    assert iss["status"] == "exhausted" and iss["no_progress"] >= 2
    assert s["state"] == "PAUSED_INCOMPLETE"
    assert s["next"]["action"] == "escalate"
    # Stop does not keep pushing the same loop
    rc, out, _ = p.hook({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "retrying"})
    assert "decision" not in (out or {}) and "NOT complete" in out["systemMessage"]
    # a genuinely new strategy must be named, and can't be repeated verbatim
    assert p.rvd("recover", iss["issue_id"], "--reset").returncode == 4
    p.rvd("recover", iss["issue_id"], "--reset", "--strategy", "bisect failing assertion", check=0)
    assert p.rvd("recover", iss["issue_id"], "--reset", "--strategy", "bisect failing assertion").returncode == 4
    # genuine progress (code change) lifts exhaustion
    p.write("calc.py", FIXED_CALC)
    assert p.rvdj("verify")[1]["state"] == "SATISFIED"


def test_E2_stop_hook_releases_session_when_agent_makes_no_progress(make_project):
    p = make_project("calc-demo")
    p.rvdj("verify")
    decisions = []
    for i in range(4):
        _, out, _ = p.hook({"hook_event_name": "Stop", "stop_hook_active": i > 0,
                            "last_assistant_message": f"I looked at it again ({i})"})
        decisions.append((out or {}).get("decision"))
    # blocks while it could help, then releases when nothing changes between blocks
    assert decisions[0] == "block" and decisions[-1] is None
    assert decisions.count("block") <= 3
    assert p.status()["state"] in ("PAUSED_INCOMPLETE", "RECOVERING")
    assert events(p, "stop_released_no_progress")
    assert p.status()["state"] != "SATISFIED"


# ---------------------------------------------------------------- F
def test_F_hook_internal_failure_is_never_success(make_project, tmp_path):
    p = make_project("calc-demo")
    p.rvdj("verify")
    poison = tmp_path / "poison"; poison.mkdir()
    (poison / "yaml.py").write_text("raise ImportError('yaml is broken')\n")
    rc, out, err = p.hook({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"},
                          env={"PYTHONPATH": str(poison)})
    assert rc == 1 and out is None and "DEGRADED" in err
    assert p.status()["state"] != "SATISFIED"


def test_F_stop_continuation_cap_is_respected_and_reported(make_project):
    p = make_project("calc-demo")
    p.rvdj("verify")
    released = None
    for i in range(10):
        # simulate an agent that *does* change code each time (progress) but never fixes it
        p.write("calc.py", FIXED_CALC.replace("return a + b", f"return a + b  # attempt {i}").replace(
            'raise ValueError("division by zero")', "return 0"))
        p.rvdj("verify")
        _, out, _ = p.hook({"hook_event_name": "Stop", "stop_hook_active": True, "last_assistant_message": f"try {i}"})
        if "decision" not in (out or {}):
            released = i
            break
    assert released is not None and released <= 6  # our cap (6) < Claude Code's (8)
    assert p.status()["state"] != "SATISFIED"


def test_F_doctor_reports_missing_hook_gate(make_project):
    p = make_project("calc-demo")
    code, rep = p.rvdj("doctor")
    hb = next(c for c in rep["checks"] if c["check"] == "hook heartbeat")
    assert not hb["ok"] and "NOT active" in hb["detail"] and code == 10
    p.hook({"hook_event_name": "SessionStart", "source": "startup"})
    code, rep = p.rvdj("doctor")
    assert next(c for c in rep["checks"] if c["check"] == "hook heartbeat")["ok"]


def test_F_disable_all_hooks_detected(make_project):
    p = make_project("calc-demo")
    (p.root / ".claude").mkdir()
    (p.root / ".claude" / "settings.json").write_text(json.dumps({"disableAllHooks": True}))
    code, rep = p.rvdj("doctor")
    c = next(c for c in rep["checks"] if c["check"] == "claude-code plugin/hook enablement")
    assert not c["ok"] and "disableAllHooks" in c["detail"]


# ---------------------------------------------------------------- G
def test_G_api_failure_persists_non_success(make_project):
    p = make_project("calc-demo")
    p.rvdj("verify")
    rc, out, _ = p.hook({"hook_event_name": "StopFailure", "error": "rate_limit", "error_details": "429",
                         "last_assistant_message": "All tests pass now"})
    assert rc == 0
    assert events(p, "agent_api_failure")[0]["error"] == "rate_limit"
    assert p.status()["state"] not in ("SATISFIED",)
    assert db(p).execute("SELECT status FROM sessions").fetchone()[0] == "failed"
    p.hook({"hook_event_name": "SessionEnd", "reason": "other"})
    assert events(p, "session_checkpoint")


# ---------------------------------------------------------------- H
def test_H_unauthorized_high_risk_alternative_is_not_run(make_project):
    p = make_project("arm-demo", init=False)
    g = (p.root / ".rvd" / "goal.yaml").read_text().replace("risk: medium", "risk: high")
    p.write(".rvd/goal.yaml", g)
    p.rvd("init", "--confirm-hard", "--by", "xiang", check=0)
    code, res = p.rvdj("verify", env={"RVD_ARM_BOARD_SSH": "pi@10.0.0.9"})
    lat = next(r for r in res["results"] if r["check"] == "latency")
    board = next(s for s in lat["skipped"] if s["alternative"] == "board-ssh")
    assert "risk=high needs authorization" in board["reason"] or "tool:ssh" in board["reason"]
    assert p.req("R3") != "VERIFIED"


# ---------------------------------------------------------------- I
def test_I_agent_cannot_quietly_change_contract(make_project):
    p = make_project("calc-demo")
    p.rvdj("verify")
    # 1) direct edit of goal.yaml to drop R-EDGE: ignored, audited, evaluation unchanged
    g = (p.root / ".rvd" / "goal.yaml").read_text()
    p.write(".rvd/goal.yaml", g.replace("mandatory: true\n    predicate: \"tests/test_div.py",
                                        "mandatory: false\n    predicate: \"tests/test_div.py"))
    s = p.status()
    assert s["contract_drift"] and next(r for r in s["requirements"] if r["id"] == "R-EDGE")["mandatory"]
    assert events(p, "contract_drift_ignored")
    assert p.rvdj("verify")[1]["state"] != "SATISFIED"
    # 2) PreToolUse denies edits/shell writes to the contract and privileged commands
    rc, out, _ = p.hook({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                         "tool_input": {"file_path": str(p.root / ".rvd" / "goal.yaml"), "old_string": "a", "new_string": "b"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    for cmd in ["sed -i 's/true/false/' .rvd/goal.yaml", "rvd amend approve amd-1 --role owner",
                "sqlite3 .rvd/state.db 'update task_state set state=\"SATISFIED\"'", "rvd resume --by me"]:
        _, out, _ = p.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}})
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
    _, out, _ = p.hook({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                        "tool_input": {"command": "cat .rvd/goal.yaml && rvd status"}})
    assert out is None  # reading is fine
    # 3) a formal amendment that weakens acceptance needs a human owner
    import yaml
    raw = yaml.safe_load(g); raw["version"] = 2; raw["requirements"][1]["mandatory"] = False
    (p.root / "weaker.yaml").write_text(yaml.safe_dump(raw))
    code, prop = p.rvdj("amend", "propose", "weaker.yaml", "--reason", "too hard", "--by", "agent")
    assert prop["status"] == "proposed" and prop["needs_human"]
    aid = prop["amendment_id"]
    assert p.rvd("amend", "approve", aid, "--by", "agent", "--role", "owner", env={"CLAUDECODE": "1"}).returncode == 4
    assert p.rvd("amend", "approve", aid, "--by", "agent", "--role", "owner").returncode == 4  # self-approval
    assert p.rvd("amend", "approve", aid, "--by", "bob", "--role", "reviewer").returncode == 4  # not owner
    assert p.status()["contract_version"] == 1


def test_I2_verification_only_change_is_auto_reviewed_for_coverage(make_project):
    import yaml
    p = make_project("calc-demo")
    raw = yaml.safe_load((p.root / ".rvd" / "goal.yaml").read_text())
    # weakening: replace the unit test with a 'mock' method that can't prove R-EDGE -> rejected
    bad = json.loads(json.dumps(raw)); bad["version"] = 2
    bad["checks"][1]["alternatives"][0].update(method="mock", command="true")
    (p.root / "bad.yaml").write_text(yaml.safe_dump(bad))
    _, r = p.rvdj("amend", "propose", "bad.yaml", "--reason", "faster", "--by", "agent")
    assert r["status"] == "rejected"
    # legit: different runner, same method -> auto-approved, contract v2, re-verification required
    good = json.loads(json.dumps(raw)); good["version"] = 2
    good["checks"][1]["alternatives"][0]["command"] = "python3 -m pytest -q -p no:cacheprovider -x tests/test_div.py"
    (p.root / "good.yaml").write_text(yaml.safe_dump(good))
    _, r = p.rvdj("amend", "propose", "good.yaml", "--reason", "fail fast", "--by", "agent")
    assert r["status"] == "auto_approved"
    assert p.status()["contract_version"] == 2


def test_I3_owner_can_approve_acceptance_change(make_project):
    import yaml
    p = make_project("calc-demo")
    raw = yaml.safe_load((p.root / ".rvd" / "goal.yaml").read_text()); raw["version"] = 2
    raw["requirements"][0]["description"] += " (and ints only)"
    raw["requirements"][0]["predicate"] = "tests/test_core.py passes"
    (p.root / "v2.yaml").write_text(yaml.safe_dump(raw))
    _, prop = p.rvdj("amend", "propose", "v2.yaml", "--reason", "clarify", "--by", "agent")
    assert prop["status"] == "proposed"
    p.rvd("amend", "approve", prop["amendment_id"], "--by", "xiang", "--role", "owner", check=0)
    assert p.status()["contract_version"] == 2
    assert "contract_drift" not in p.status() or p.status()["contract_drift"] is None


# ---------------------------------------------------------------- J
def test_J_code_change_makes_old_pass_stale(make_project):
    p = make_project("calc-demo")
    p.write("calc.py", FIXED_CALC)
    assert p.rvdj("verify")[1]["state"] == "SATISFIED"
    p.write("calc.py", FIXED_CALC + "\n# refactor\n")
    s = p.status()
    assert {r["status"] for r in s["requirements"]} == {"STALE"}
    assert s["state"] != "SATISFIED" and s["next"]["action"] == "verify"
    _, out, _ = p.hook({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "done"})
    assert out["decision"] == "block" and "verify" in out["reason"]
    assert p.rvdj("verify")[1]["state"] == "SATISFIED"


def test_J2_tampered_evidence_log_is_invalid(make_project):
    p = make_project("calc-demo")
    p.write("calc.py", FIXED_CALC)
    p.rvdj("verify")
    log = db(p).execute("SELECT log_path FROM evidence WHERE check_id='edge'").fetchone()[0]
    with open(log, "a") as f:
        f.write("\nedited by hand\n")
    assert p.req("R-EDGE") == "STALE" and p.status()["state"] != "SATISFIED"


# ---------------------------------------------------------------- K
def test_K_duplicate_completion_events_are_idempotent(make_project):
    p = make_project("calc-demo")
    p.rvdj("verify")
    ev = {"hook_event_name": "Stop", "session_id": "dup", "stop_hook_active": False, "last_assistant_message": "done"}
    r1 = p.hook(ev); r2 = p.hook(ev)
    assert r1[1] == r2[1] and r1[1]["decision"] == "block"
    assert db(p).execute("SELECT stop_blocks FROM sessions WHERE session_key='claude-code:dup'").fetchone()[0] == 1
    assert len(events(p, "hook_replay_ignored")) == 1
    n_ev = db(p).execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    tc = {"hook_event_name": "TaskCompleted", "task_id": "9", "task_subject": "R-EDGE"}
    assert p.hook(tc)[0] == 2 and p.hook(tc)[0] == 2
    assert db(p).execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == n_ev
    assert len(events(p, "task_completion_blocked")) == 1
    # issues are not duplicated by repeated verification
    p.rvdj("verify")
    assert db(p).execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 1


# ---------------------------------------------------------------- L
def test_L_user_cancel_is_final_for_automation(make_project):
    p = make_project("calc-demo")
    p.rvdj("verify")
    p.rvd("cancel", "--by", "xiang", "--reason", "deprioritized", check=0)
    assert p.status()["state"] == "CANCELLED"
    rc, out, _ = p.hook({"hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": "continue?"})
    assert out is None or "decision" not in out
    assert p.rvd("verify").returncode == 4
    assert p.rvd("resume", "--by", "agent", env={"CLAUDECODE": "1"}).returncode == 4
    assert p.status()["next"]["action"] == "none"
    p.rvd("resume", "--by", "xiang", check=0)
    assert p.status()["state"] != "CANCELLED"
