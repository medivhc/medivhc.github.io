"""Claude Code hook handlers (session gate layer).

Each handler is fast: reads a snapshot, does cheap recomputation (hashing, SQL), and
returns a decision. It never runs the project's build/tests. A hook can *influence*
one session; it is not the authority — the Controller state is.

Return value: (exit_code, stdout_json_or_None, stderr_text).
Claude Code semantics (verified against the installed version, see docs/design-decisions.md):
  * Stop / SubagentStop: {"decision": "block", "reason": ...} keeps the turn going.
    Claude Code itself overrides after CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (default 8)
    consecutive blocks, so we keep our own lower budget + no-progress detection.
  * TaskCompleted: exit 2 + stderr prevents marking the task completed.
  * PreToolUse: hookSpecificOutput.permissionDecision = "deny".
  * SessionStart: hookSpecificOutput.additionalContext.
  * StopFailure / SessionEnd: side effects only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .contracts import canonical_json
from .controller import Controller, find_project_root
from .state_machine import GState
from .storage import now_iso

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RVD_CMD = f'python3 "{PLUGIN_ROOT / "scripts" / "rvd.py"}"'
REPLAY_WINDOW_SEC = 5.0

Result = tuple[int, dict | None, str]


def _ok(out: dict | None = None) -> Result:
    return 0, out, ""


def _dedupe_key(event: dict) -> str:
    return hashlib.sha256(canonical_json(event).encode()).hexdigest()


def _replayed(ctrl: Controller, key: str) -> Result | None:
    row = ctrl.store.q1("SELECT ts, decision_json FROM hook_events WHERE dedupe_key=?", (key,))
    if row is None:
        return None
    from datetime import datetime
    age = time.time() - datetime.fromisoformat(row["ts"]).timestamp()
    if age > REPLAY_WINDOW_SEC:
        return None
    d = json.loads(row["decision_json"])
    return d["code"], d["out"], d["err"]


def _remember(ctrl: Controller, key: str, event_name: str, session_id: str | None, res: Result) -> None:
    with ctrl.store.tx():
        ctrl.store.x("INSERT OR REPLACE INTO hook_events(dedupe_key,event,session_id,ts,decision_json) VALUES(?,?,?,?,?)",
                     (key, event_name, session_id, now_iso(),
                      json.dumps({"code": res[0], "out": res[1], "err": res[2]})))
        ctrl.store.meta_set("hook_heartbeat", now_iso())
        ctrl.store.meta_set(f"hook_heartbeat:{event_name}", now_iso())


def _summary(ctrl: Controller, limit: int = 8) -> str:
    rows = ctrl.requirement_rows()
    unmet = [f"{r['id']}={r['status']}" for r in rows if r["mandatory"] and r["status"] != "VERIFIED"]
    return ", ".join(unmet[:limit]) + (" …" if len(unmet) > limit else "")


def _open_controller(event: dict) -> Controller | None:
    cwd = event.get("cwd")
    root = find_project_root(Path(cwd) if cwd else None)
    if root is None:
        return None
    ctrl = Controller(root)
    if not ctrl.initialized:
        ctrl.close()
        return None
    return ctrl


# --------------------------------------------------------------------------- handlers
def on_session_start(ctrl: Controller, ev: dict) -> Result:
    ctrl.register_session("claude-code", ev.get("session_id", "unknown"), resumable=True)
    with ctrl.store.tx():
        ctrl.store.x("UPDATE sessions SET stop_blocks=0, consecutive_no_progress=0 WHERE session_key=?",
                     (f"claude-code:{ev.get('session_id', 'unknown')}",))
    ctrl.refresh("hook:SessionStart")
    st = ctrl.status()
    nxt = st["next"]
    lines = [f"[RVD] Goal `{st['goal_id']}` (contract v{st['contract_version']}) — state {st['state']}.",
             f"Intent: {st['intent']}"]
    unmet = _summary(ctrl)
    if unmet:
        lines.append(f"Unmet mandatory requirements: {unmet}.")
    if st["open_issues"]:
        lines.append("Open issues: " + "; ".join(f"{i['issue_id']} [{i['status']}] {i['title']}"
                                                  for i in st["open_issues"][:5]))
    lines.append("Next action: " + json.dumps(nxt, ensure_ascii=False))
    lines.append(f"Completion is decided only by evidence (`{RVD_CMD} verify`), not by your summary. "
                 "Do not edit .rvd/goal.yaml; propose changes with `rvd amend propose`.")
    if st["contract_drift"]:
        lines.append("WARNING: " + st["contract_drift"]["drift"])
    return _ok({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}})


def on_user_prompt_submit(ctrl: Controller, ev: dict) -> Result:
    key = ctrl.register_session("claude-code", ev.get("session_id", "unknown"), resumable=True)
    with ctrl.store.tx():  # a new human turn resets the per-turn continuation budget
        ctrl.store.x("UPDATE sessions SET stop_blocks=0, consecutive_no_progress=0, last_block_fingerprint='' "
                     "WHERE session_key=?", (key,))
    return _ok()


def on_stop(ctrl: Controller, ev: dict, subagent: bool = False) -> Result:
    sid = ev.get("session_id", "unknown")
    key = ctrl.register_session("claude-code", sid, resumable=True)
    ctrl.record_claim("hook:SubagentStop" if subagent else "hook:Stop", ev.get("agent_type") or "claude-code-worker",
                      ev.get("last_assistant_message", ""), key)
    if subagent:
        # Sub-agent results are recorded for review; the global gate lives on the main Stop.
        return _ok()
    decision = ctrl.refresh("hook:Stop")
    gs = GState(decision["state"])
    if gs in (GState.SATISFIED, GState.CANCELLED):
        return _ok({"systemMessage": f"[RVD] goal state: {gs.value}"} if gs == GState.SATISFIED else None)
    nxt = ctrl.next()
    budget = ctrl.contract.budget
    sess = ctrl.store.q1("SELECT * FROM sessions WHERE session_key=?", (key,))
    unmet = _summary(ctrl)
    if nxt["action"] not in ("verify", "recover"):
        # Nothing the agent can do autonomously right now: let it stop, but say plainly it's NOT done.
        msg = (f"[RVD] Goal NOT complete — state {gs.value}. Unmet: {unmet}. "
               f"Next: {nxt['action']}. See `{RVD_CMD} report`.")
        with ctrl.store.tx():
            ctrl.store.event("hook:Stop", "hook", "stop_allowed_incomplete", {"session": key, "next": nxt})
        return _ok({"systemMessage": msg})
    fp = ctrl.progress_fingerprint()
    no_prog = sess["consecutive_no_progress"]
    if sess["stop_blocks"] > 0:
        no_prog = no_prog + 1 if fp == sess["last_block_fingerprint"] else 0
    if no_prog >= budget["max_consecutive_no_progress"] or sess["stop_blocks"] >= budget["max_stop_blocks_per_session"]:
        why = ("no new evidence or code change since the last continuation request"
               if no_prog >= budget["max_consecutive_no_progress"] else "per-session continuation budget used up")
        with ctrl.store.tx():
            ctrl._transition(GState.PAUSED_INCOMPLETE, "hook:Stop", f"session released: {why}", "hook:Stop")
            ctrl.store.x("UPDATE sessions SET consecutive_no_progress=? WHERE session_key=?", (no_prog, key))
            ctrl.store.event("hook:Stop", "hook", "stop_released_no_progress",
                             {"session": key, "why": why, "stop_blocks": sess["stop_blocks"]})
        return _ok({"systemMessage": f"[RVD] Goal NOT complete ({unmet}). Paused: {why}. "
                                     f"Open issues remain in the store; resume later or change strategy."})
    with ctrl.store.tx():
        ctrl.store.x("UPDATE sessions SET stop_blocks=stop_blocks+1, last_block_fingerprint=?, "
                     "consecutive_no_progress=? WHERE session_key=?", (fp, no_prog, key))
        ctrl.store.event("hook:Stop", "hook", "stop_blocked",
                         {"session": key, "next": nxt, "stop_hook_active": ev.get("stop_hook_active"),
                          "block_no": sess["stop_blocks"] + 1})
    reason = (f"RVD gate: goal `{ctrl.contract.goal_id}` is NOT satisfied (state {gs.value}). "
              f"Unmet mandatory requirements: {unmet}. Your own summary or a failure table is not completion. "
              f"Next action ({nxt['action']}, issue {nxt.get('issue_id', '-')}): {nxt.get('instructions', '')} "
              f"Then run `{RVD_CMD} verify`. If you are genuinely blocked by a missing resource or permission, "
              f"run `{RVD_CMD} status`, explain the blocker, and do not weaken tests or edit .rvd/goal.yaml.")
    return _ok({"decision": "block", "reason": reason})


REQ_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def on_task_completed(ctrl: Controller, ev: dict) -> Result:
    text = f"{ev.get('task_subject', '')}\n{ev.get('task_description', '')}"
    ids = {r.id for r in ctrl.contract.requirements}
    referenced = sorted(set(REQ_TOKEN.findall(text)) & ids)
    ctrl.refresh("hook:TaskCompleted")
    rows = {r["id"]: r["status"] for r in ctrl.requirement_rows()}
    if referenced:
        unmet = [f"{r}={rows[r]}" for r in referenced if rows.get(r) != "VERIFIED"]
    elif ctrl.contract.completion_policy["unreferenced_tasks"] == "block" and ctrl.global_state() != GState.SATISFIED:
        unmet = [f"goal={ctrl.global_state().value}"]
    else:
        return _ok()
    if not unmet:
        return _ok()
    with ctrl.store.tx():
        ctrl.store.event("hook:TaskCompleted", "hook", "task_completion_blocked",
                         {"task_id": ev.get("task_id"), "subject": ev.get("task_subject"), "unmet": unmet})
    return 2, None, (f"RVD: task '{ev.get('task_subject', '')}' cannot be marked completed — required evidence is "
                     f"missing or failing: {', '.join(unmet)}. Fix the cause and run `{RVD_CMD} verify`; "
                     f"the task may be completed only after the requirement is VERIFIED.")


PROTECTED_REL = (".rvd/goal.yaml", ".rvd/policy.yaml", ".rvd/supervisor.yaml", ".rvd/state.db", ".rvd/evidence")
_WRITE_OPS = re.compile(r"(>|\btee\b|\bsed\s+-i|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bperl\s+-p?i|"
                        r"python[0-9.]*\s+-c|\bchmod\b|\bln\b|\btouch\b|\bdd\b)")
BASH_DENY = [
    (re.compile(r"\.rvd/(goal|policy)\.ya?ml"), _WRITE_OPS),
    (re.compile(r"\.rvd/evidence"), _WRITE_OPS),          # reading logs is fine, rewriting them is not
    (re.compile(r"\.rvd/state\.db|\bsqlite3\b[^|;&]*\.rvd"), re.compile(r".")),
    (re.compile(r"\brvd(\.py)?\b[^|;&]*\bamend\s+approve\b"), re.compile(r".")),
    (re.compile(r"\brvd(\.py)?\b[^|;&]*\b(resume|cancel)\b"), re.compile(r".")),
    (re.compile(r"\brvd(\.py)?\b[^|;&]*--confirm-hard\b"), re.compile(r".")),
    (re.compile(r"\brvd(\.py)?\b[^|;&]*\bsupervise\s+(run|accept|clear-stop)\b"), re.compile(r".")),
    (re.compile(r"\.rvd/supervisor\.ya?ml"), _WRITE_OPS),
    (re.compile(r"RVD_ALLOW_AGENT_CONFIRM|RVD_STATE_DIR="), re.compile(r".")),
]


def on_pre_tool_use(ctrl: Controller, ev: dict) -> Result:
    tool = ev.get("tool_name", "")
    ti = ev.get("tool_input") or {}
    reason = None
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = ti.get("file_path") or ti.get("notebook_path") or ""
        try:
            rel = Path(fp).resolve().relative_to(ctrl.root).as_posix()
        except ValueError:
            rel = ""
        if any(rel == p or rel.startswith(p + "/") for p in PROTECTED_REL):
            reason = (f"RVD: {rel} is the authoritative goal contract/evidence store and cannot be edited by the agent. "
                      f"Propose contract changes with `{RVD_CMD} amend propose <file> --reason ...`.")
    elif tool == "Bash":
        cmd = ti.get("command", "")
        for target, op in BASH_DENY:
            if target.search(cmd) and op.search(cmd):
                reason = ("RVD: this command would modify the goal contract/evidence store or self-approve a "
                          "privileged RVD action (approve/resume/cancel/confirm-hard). Those require a human. "
                          f"Use `{RVD_CMD} amend propose` instead.")
                break
    if reason is None:
        return _ok()
    with ctrl.store.tx():
        ctrl.store.event("hook:PreToolUse", "hook", "protected_change_denied", {"tool": tool, "input": ti})
    return _ok({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}})


def on_post_tool(ctrl: Controller, ev: dict, failed: bool) -> Result:
    if ev.get("tool_name") != "Bash":
        return _ok()
    ti = ev.get("tool_input") or {}
    with ctrl.store.tx():
        ctrl.store.event("hook:PostToolUse", "hook", "tool_failure" if failed else "tool_run",
                         {"command": (ti.get("command") or "")[:300],
                          "error": (ev.get("error") or "")[:300] if failed else None,
                          "interrupted": ev.get("is_interrupt")})
    return _ok()


def on_stop_failure(ctrl: Controller, ev: dict) -> Result:
    key = ctrl.register_session("claude-code", ev.get("session_id", "unknown"), resumable=True)
    with ctrl.store.tx():
        ctrl.store.event("hook:StopFailure", "hook", "agent_api_failure",
                         {"session": key, "error": ev.get("error"), "details": (ev.get("error_details") or "")[:500]})
        ctrl.store.x("UPDATE sessions SET status='failed' WHERE session_key=?", (key,))
    ctrl.refresh("hook:StopFailure")  # state stays non-satisfied; nothing here can mark success
    return _ok()


def on_session_end(ctrl: Controller, ev: dict) -> Result:
    key = f"claude-code:{ev.get('session_id', 'unknown')}"
    ctrl.end_session(key, f"ended:{ev.get('reason', 'other')}")
    ctrl.store.release_leases_of(f"hook:{ev.get('session_id', '')}")
    with ctrl.store.tx():
        ctrl.store.event("hook:SessionEnd", "hook", "session_checkpoint",
                         {"session": key, "reason": ev.get("reason"), "state": ctrl.global_state().value})
    ctrl.store.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return _ok()


HANDLERS = {
    "SessionStart": on_session_start,
    "UserPromptSubmit": on_user_prompt_submit,
    "Stop": on_stop,
    "SubagentStop": lambda c, e: on_stop(c, e, subagent=True),
    "TaskCompleted": on_task_completed,
    "PreToolUse": on_pre_tool_use,
    "PostToolUse": lambda c, e: on_post_tool(c, e, False),
    "PostToolUseFailure": lambda c, e: on_post_tool(c, e, True),
    "StopFailure": on_stop_failure,
    "SessionEnd": on_session_end,
}
DEDUPE_EVENTS = {"Stop", "SubagentStop", "TaskCompleted"}


def handle(event: dict) -> Result:
    name = event.get("hook_event_name", "")
    handler = HANDLERS.get(name)
    if handler is None:
        return _ok()
    ctrl = None
    try:
        ctrl = _open_controller(event)
        if ctrl is None:
            return _ok()  # not an RVD project: stay out of the way
        key = _dedupe_key(event)
        if name in DEDUPE_EVENTS:
            # Idempotency is "same event against the same state". If evidence/code/state changed
            # since, the event must be re-evaluated rather than answered from the replay cache.
            key = hashlib.sha256(f"{key}|{ctrl.global_state().value}|{ctrl.progress_fingerprint()}"
                                 .encode()).hexdigest()
            prev = _replayed(ctrl, key)
            if prev is not None:
                with ctrl.store.tx():
                    ctrl.store.event(f"hook:{name}", "hook", "hook_replay_ignored", {"event": name})
                return prev
        res = handler(ctrl, event)
        _remember(ctrl, key, name, event.get("session_id"), res)
        return res
    except Exception as e:  # never produce a "pass" on internal failure
        msg = f"RVD hook {name} internal error: {e!r}. Goal state was NOT changed to satisfied."
        if ctrl is not None:
            ctrl._record_error(f"hook:{name}", msg)
        return 1, None, msg
    finally:
        if ctrl is not None:
            ctrl.close()


def main_stdin() -> int:
    import sys
    raw = sys.stdin.read()
    try:
        event = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        sys.stderr.write(f"RVD hook: invalid JSON on stdin: {e}\n")
        return 1
    if os.environ.get("RVD_HOOK_DEBUG"):
        Path(os.environ["RVD_HOOK_DEBUG"]).open("a").write(json.dumps(event) + "\n")
    code, out, err = handle(event)
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    if err:
        sys.stderr.write(err + "\n")
    return code
