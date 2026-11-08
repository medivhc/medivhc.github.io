"""Replays hook payloads captured from a real Claude Code 2.1.319 session (RVD_HOOK_DEBUG)
against a fresh project, proving the handlers accept the real field shapes."""
import json
from pathlib import Path

from conftest import FIXTURES

REAL = FIXTURES / "claude-code-2.1.319" / "live_session_hook_events.jsonl"


def _events():
    return [json.loads(l) for l in REAL.read_text().splitlines() if l.strip()]


def test_real_payloads_have_expected_fields():
    evs = _events()
    names = {e["hook_event_name"] for e in evs}
    assert {"SessionStart", "Stop", "PreToolUse", "PostToolUse", "SessionEnd"} <= names
    stop = next(e for e in evs if e["hook_event_name"] == "Stop")
    assert {"session_id", "cwd", "stop_hook_active", "last_assistant_message"} <= stop.keys()


def test_real_payloads_replay_against_failing_project(make_project):
    p = make_project("calc-demo")
    p.rvdj("verify")
    decisions = {}
    for ev in _events():
        ev = {**ev, "cwd": str(p.root), "session_id": "replay"}
        if ev["hook_event_name"] == "PreToolUse" and ev.get("tool_input", {}).get("file_path"):
            ev["tool_input"]["file_path"] = str(p.root / Path(ev["tool_input"]["file_path"]).name)
        rc, out, err = p.hook(ev)
        assert rc in (0, 2), (ev["hook_event_name"], err)
        decisions.setdefault(ev["hook_event_name"], []).append(out)
    # first real Stop (agent finished with a failure table) must be blocked on a failing project
    assert decisions["Stop"][0]["decision"] == "block"
    ctx = decisions["SessionStart"][0]["hookSpecificOutput"]["additionalContext"]
    assert "R-EDGE=FAILED" in ctx
