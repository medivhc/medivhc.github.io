import json
import shutil
import sqlite3

import pytest
import yaml

from conftest import EXAMPLES, FIXED_CALC
from rvd_core.adapters import MockAdapter, build_context
from rvd_core.contracts import (GoalContract, diff_contracts, load_contract_file, review_verification_change,
                                validate_raw)
from rvd_core.controller import Controller, PermissionDenied
from rvd_core.state_machine import GState, RStatus, TransitionError, check_transition, decide_global_state
from rvd_core.storage import SCHEMA_VERSION, Store


# ------------------------------------------------------------ state machine invariants
def test_satisfied_only_via_controller_decision():
    for src in GState:
        if GState.SATISFIED in __import__("rvd_core.state_machine", fromlist=["ALLOWED"]).ALLOWED[src]:
            for source in ("hook:Stop", "worker", "user", "controller"):
                with pytest.raises(TransitionError):
                    check_transition(src, GState.SATISFIED, source)
            check_transition(src, GState.SATISFIED, "controller.decide")


def test_illegal_and_user_only_transitions():
    with pytest.raises(TransitionError):
        check_transition(GState.NEW, GState.SATISFIED, "controller.decide")
    with pytest.raises(TransitionError):
        check_transition(GState.CANCELLED, GState.READY, "controller")
    check_transition(GState.CANCELLED, GState.READY, "user")
    with pytest.raises(TransitionError):
        check_transition(GState.CANCELLED, GState.RECOVERING, "user")


def test_decide_requires_all_mandatory_verified():
    st = {"A": RStatus.VERIFIED, "B": RStatus.PARTIALLY_VERIFIED}
    assert decide_global_state(st, {"A", "B"}, open_actionable=0, open_blocked=1, exhausted=False)[0] == GState.BLOCKED
    assert decide_global_state(st, {"A"}, open_actionable=0, open_blocked=1, exhausted=False)[0] == GState.SATISFIED
    st["B"] = RStatus.FAILED
    assert decide_global_state(st, {"A", "B"}, open_actionable=1, open_blocked=0, exhausted=False)[0] == GState.RECOVERING
    assert decide_global_state(st, {"A", "B"}, open_actionable=0, open_blocked=0, exhausted=True)[0] == GState.PAUSED_INCOMPLETE


# ------------------------------------------------------------ contracts
def test_examples_validate_and_schema_rejects_garbage():
    for ex in ("calc-demo", "arm-demo"):
        load_contract_file(EXAMPLES / ex / ".rvd" / "goal.yaml")
    assert validate_raw({"goal_id": "x"})
    raw = yaml.safe_load((EXAMPLES / "calc-demo" / ".rvd" / "goal.yaml").read_text())
    raw["checks"][0]["proves"] = ["NOPE"]
    assert any("unknown requirement" in e for e in validate_raw(raw))


def test_diff_classifies_hard_and_acceptance_changes():
    raw = yaml.safe_load((EXAMPLES / "arm-demo" / ".rvd" / "goal.yaml").read_text())
    old = GoalContract(raw)
    new = json.loads(json.dumps(raw)); new["requirements"][2]["accept"]["methods"] = ["simulation"]
    d = diff_contracts(old, GoalContract(new))
    assert d["needs_human"] and not d["verification_only"]
    new2 = json.loads(json.dumps(raw)); new2["constraints"][0]["kind"] = "NEGOTIABLE"
    assert diff_contracts(old, GoalContract(new2))["needs_human"]
    new3 = json.loads(json.dumps(raw)); new3["checks"][1]["alternatives"].reverse()
    d3 = diff_contracts(old, GoalContract(new3))
    assert d3["verification_only"] and not d3["needs_human"]
    assert review_verification_change(old, GoalContract(new3))[0]
    new4 = json.loads(json.dumps(raw)); new4["checks"][2]["alternatives"] = [new4["checks"][2]["alternatives"][1]]
    ok, problems = review_verification_change(old, GoalContract(new4))
    assert not ok and "R3" in problems[0]  # dropping the only real-hardware path loses coverage


# ------------------------------------------------------------ storage
def test_migrations_idempotent_and_version_guard(tmp_path):
    s = Store(tmp_path / "s.db"); s.close()
    s = Store(tmp_path / "s.db")
    assert s.meta_get("schema_version") == str(SCHEMA_VERSION); s.close()
    con = sqlite3.connect(tmp_path / "s.db")
    con.execute("UPDATE meta SET value='999' WHERE key='schema_version'"); con.commit(); con.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store(tmp_path / "s.db")


def test_lease_excludes_concurrent_owner(tmp_path):
    a, b = Store(tmp_path / "s.db"), Store(tmp_path / "s.db")
    assert a.acquire_lease("verify", "A", 60)
    assert not b.acquire_lease("verify", "B", 60)
    a.release_lease("verify", "A")
    assert b.acquire_lease("verify", "B", 60)


def test_state_dir_override_keeps_projects_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("RVD_STATE_DIR", str(tmp_path / "state"))
    roots = []
    for n in ("p1", "p2"):
        d = tmp_path / n; shutil.copytree(EXAMPLES / "calc-demo", d); roots.append(d)
        c = Controller(d); c.init("xiang", confirm_hard=True); c.close()
    dbs = list((tmp_path / "state").glob("*/state.db"))
    assert len(dbs) == 2
    assert not (roots[0] / ".rvd" / "state.db").exists()


def test_hard_confirmation_not_possible_from_agent(tmp_path, monkeypatch):
    d = tmp_path / "p"; shutil.copytree(EXAMPLES / "calc-demo", d)
    monkeypatch.setenv("CLAUDECODE", "1")
    c = Controller(d)
    with pytest.raises(PermissionDenied):
        c.init("agent", confirm_hard=True)


# ------------------------------------------------------------ cross-adapter hand-off (§7.5.4)
def test_goal_continues_across_adapters_after_session_exit(tmp_path):
    d = tmp_path / "calc"; shutil.copytree(EXAMPLES / "calc-demo", d)
    c = Controller(d); c.init("xiang", confirm_hard=True); c.verify("bootstrap")

    def worker_a(root, spec, ctx):  # fixes only div(), then its session "exits"
        src = (root / "calc.py").read_text().replace(
            "return a / b  # BUG", "if b == 0:\n        raise ValueError('division by zero')\n    return a / b  # was BUG")
        (root / "calc.py").write_text(src)
        return "All done, everything passes!"  # an over-claim

    a = MockAdapter("vendor-a", worker_a, resume=True)
    key_a = c.register_session(a.name, "a-1", resumable=a.capabilities.resume)
    eid = a.start_task({"project_root": str(d), "instruction": "fix R-EDGE"}, build_context(c))
    c.record_claim("adapter", a.name, a.collect_artifacts(eid)["claim"], key_a)
    c.end_session(key_a, "exited")
    r = c.verify("independent-verifier")
    assert r["state"] != "SATISFIED" and "R-EDGE" in c.unmet_mandatory()  # claim did not count

    # vendor-b cannot resume vendor-a's session: it starts fresh with persisted context
    b = MockAdapter("vendor-b", lambda root, spec, ctx: (root / "calc.py").write_text(FIXED_CALC) or
                    f"continuing {ctx['next'].get('issue_id')}", resume=False)
    assert b.request_resume("a-1", "continue")["resumed"] is False
    ctx = build_context(c)
    assert ctx["unmet"] == ["R-EDGE"] and ctx["open_issues"]
    eid = b.start_task({"project_root": str(d), "instruction": "continue"}, ctx)
    assert b.poll_or_wait(eid) == "finished"
    assert c.verify("independent-verifier")["state"] == "SATISFIED"
    sessions = {r["session_key"]: r["status"] for r in c.store.q("SELECT * FROM sessions")}
    assert sessions["vendor-a:a-1"] == "exited"
    c.close()


def test_crashing_worker_is_failed_execution_not_goal_failure(tmp_path):
    def boom(root, spec, ctx):
        raise RuntimeError("api 529")
    m = MockAdapter("x", boom)
    eid = m.start_task({"project_root": str(tmp_path)}, {})
    assert m.poll_or_wait(eid) == "failed" and "529" in m.collect_artifacts(eid)["claim"]
