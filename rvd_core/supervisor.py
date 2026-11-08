"""P2 external Supervisor: cross-session continuation under a human-authorized run policy.

What it is: a separate, long-running process (`rvd supervise run`) that repeatedly asks the
Controller for the next permitted action and, when that action needs an agent, dispatches
it through an AgentAdapter (`claude -p`, a generic CLI, or a mock). After each execution it
runs the Controller's independent verification. It is NOT a Stop-hook loop.

What it may never do:
  * restart a CANCELLED task or one a human stopped (stop flag), or keep going past BLOCKED /
    escalate (no information gain) — those need a human;
  * widen permissions: the agent gets exactly the tools/permission mode of the *accepted*
    policy; a permission denial stops dispatching for that policy version;
  * run without budgets: dispatches, cost, wall time, API-retry and crash-retry limits;
  * re-dispatch an action whose effect was already recorded (crash-safe checkpoints + action keys).
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .adapters import AgentAdapter, ClaudeCodeHeadlessAdapter, ShellAdapter, _kill_tree, build_context
from .contracts import canonical_json, sha256_text
from .controller import Controller, PermissionDenied, _is_agent_context
from .evidence import artifact_hash
from .state_machine import GState
from .storage import new_id, now_iso

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
POLICY_FILE = "supervisor.yaml"

DEFAULT_POLICY = {
    "adapter": "claude-code",
    "claude": {"allowed_tools": "Bash Read Edit Write Glob Grep", "permission_mode": "default",
               "max_turns": 60, "model": None, "extra_args": []},
    "shell": {"command": ""},
    "max_dispatches": 6,
    "cost_budget_usd": 5.0,
    "time_budget_sec": 4 * 3600,
    "execution_timeout_sec": 1800,
    "max_api_retries": 4,
    "backoff_base_sec": 30,
    "backoff_max_sec": 900,
    "max_crash_retries": 2,
    "cooldown_sec": 5,
}

POLICY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "adapter": {"enum": ["claude-code", "shell"]},
        "claude": {"type": "object", "additionalProperties": False, "properties": {
            "allowed_tools": {"type": "string"},
            "permission_mode": {"enum": ["default", "acceptEdits", "plan", "dontAsk"]},
            "max_turns": {"type": ["integer", "null"], "minimum": 1},
            "model": {"type": ["string", "null"]},
            "extra_args": {"type": "array", "items": {"type": "string"}}}},
        "shell": {"type": "object", "additionalProperties": False, "properties": {"command": {"type": "string"}}},
        "max_dispatches": {"type": "integer", "minimum": 1, "maximum": 1000},
        "cost_budget_usd": {"type": "number", "exclusiveMinimum": 0},
        "time_budget_sec": {"type": "integer", "minimum": 1},
        "execution_timeout_sec": {"type": "integer", "minimum": 1},
        "max_api_retries": {"type": "integer", "minimum": 0},
        "backoff_base_sec": {"type": "number", "minimum": 0},
        "backoff_max_sec": {"type": "number", "minimum": 0},
        "max_crash_retries": {"type": "integer", "minimum": 0},
        "cooldown_sec": {"type": "number", "minimum": 0},
    },
}

TERMINAL_STOPS = {"satisfied", "cancelled", "user_stop", "blocked", "needs_human", "permission_denied",
                  "dispatch_budget", "cost_budget", "time_budget", "rate_limit_budget", "crash_budget",
                  "harness_error", "lease_lost", "max_iterations", "no_action"}


class SupervisorError(RuntimeError):
    pass


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_policy_file(ctrl: Controller) -> dict:
    p = ctrl.rvd / POLICY_FILE
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    errs = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
            for e in Draft202012Validator(POLICY_SCHEMA).iter_errors(raw or {})]
    if errs:
        raise SupervisorError("supervisor policy invalid:\n  - " + "\n  - ".join(errs))
    pol = _merge(DEFAULT_POLICY, raw or {})
    if any(a in ClaudeCodeHeadlessAdapter.FORBIDDEN_ARGS for a in pol["claude"]["extra_args"]):
        raise SupervisorError("supervisor policy may not contain permission-bypass flags")
    if pol["adapter"] == "shell" and not pol["shell"]["command"]:
        raise SupervisorError("adapter 'shell' requires shell.command")
    return pol


def accept_policy(ctrl: Controller, by: str) -> dict:
    """Human authorization of the run policy (what the supervisor may spend and grant)."""
    if _is_agent_context() and not os.environ.get("RVD_ALLOW_AGENT_CONFIRM"):
        raise PermissionDenied("the supervisor run policy must be accepted by a human, not from an agent session")
    pol = load_policy_file(ctrl)
    h = sha256_text(canonical_json(pol))
    with ctrl.store.tx():
        cur = ctrl.store.q1("SELECT version, policy_hash FROM supervisor_policy ORDER BY version DESC LIMIT 1")
        if cur and cur["policy_hash"] == h:
            return {"version": cur["version"], "unchanged": True, "policy": pol}
        v = (cur["version"] + 1) if cur else 1
        ctrl.store.x("INSERT INTO supervisor_policy(version,policy_json,policy_hash,accepted_by,accepted_at) "
                     "VALUES(?,?,?,?,?)", (v, canonical_json(pol), h, by, now_iso()))
        ctrl.store.event(by, "supervisor", "supervisor_policy_accepted", {"version": v, "policy": pol})
    return {"version": v, "policy": pol}


def accepted_policy(ctrl: Controller) -> tuple[int, dict]:
    row = ctrl.store.q1("SELECT version, policy_json, policy_hash FROM supervisor_policy ORDER BY version DESC LIMIT 1")
    if row is None:
        raise PermissionDenied("no accepted supervisor policy; a human must run `rvd supervise accept --by <name>`")
    pol = json.loads(row["policy_json"])
    try:
        on_disk = sha256_text(canonical_json(load_policy_file(ctrl)))
    except (SupervisorError, OSError, yaml.YAMLError):
        on_disk = None
    if on_disk != row["policy_hash"]:
        raise PermissionDenied(".rvd/supervisor.yaml differs from the accepted policy (possible privilege change); "
                               "a human must re-run `rvd supervise accept --by <name>`")
    return row["version"], pol


def make_adapter(pol: dict, plugin_root: Path = PLUGIN_ROOT) -> AgentAdapter:
    if pol["adapter"] == "claude-code":
        c = pol["claude"]
        return ClaudeCodeHeadlessAdapter(plugin_root, allowed_tools=c["allowed_tools"],
                                         permission_mode=c["permission_mode"], max_turns=c["max_turns"],
                                         model=c["model"], extra_args=c["extra_args"],
                                         timeout=pol["execution_timeout_sec"])
    return ShellAdapter(pol["shell"]["command"], timeout=pol["execution_timeout_sec"])


def stop_flag(ctrl: Controller) -> dict | None:
    v = ctrl.store.meta_get("supervisor_stop")
    return json.loads(v) if v else None


def request_stop(ctrl: Controller, by: str, reason: str = "") -> dict:
    """Anyone may ask the supervisor to stop (the safe direction)."""
    with ctrl.store.tx():
        ctrl.store.meta_set("supervisor_stop", json.dumps({"by": by, "reason": reason, "ts": now_iso()}))
        ctrl.store.event(by, "supervisor", "supervisor_stop_requested", {"reason": reason})
    return {"stop_requested": True}


def clear_stop(ctrl: Controller, by: str) -> dict:
    if _is_agent_context() and not os.environ.get("RVD_ALLOW_AGENT_CONFIRM"):
        raise PermissionDenied("only a human may allow the supervisor to run again after a stop")
    with ctrl.store.tx():
        ctrl.store.x("DELETE FROM meta WHERE key='supervisor_stop'")
        ctrl.store.event(by, "supervisor", "supervisor_stop_cleared", {})
    return {"stop_cleared": True}


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Supervisor:
    def __init__(self, ctrl: Controller, adapter: AgentAdapter | None = None, *, clock=time.time,
                 sleep=time.sleep, poll_interval: float = 2.0, lease_ttl: float = 60.0, enforce_human: bool = True):
        if enforce_human and _is_agent_context() and not os.environ.get("RVD_ALLOW_AGENT_CONFIRM"):
            raise PermissionDenied("the supervisor must be started by a human/operator, not from inside an agent "
                                   "session (a hook or agent must not spawn its own supervisor)")
        self.ctrl = ctrl
        self.policy_version, self.policy = accepted_policy(ctrl)
        self.adapter = adapter or make_adapter(self.policy)
        self.clock, self._sleep, self.poll_interval, self.lease_ttl = clock, sleep, poll_interval, lease_ttl
        self.run_id = new_id("sup")
        self.owner = f"supervisor:{self.run_id}:{os.getpid()}"
        self._signal_stop: str | None = None
        self.log: list[str] = []

    # ------------------------------------------------------------ infrastructure
    def _event(self, kind: str, payload: dict) -> None:
        with self.ctrl.store.tx():
            self.ctrl.store.event(f"supervisor:{self.run_id}", "supervisor", kind, payload,
                                  self.ctrl.contract.version)
        self.log.append(f"{kind} {json.dumps(payload, ensure_ascii=False)[:200]}")

    def _renew(self) -> bool:
        return self.ctrl.store.acquire_lease("supervisor", self.owner, self.lease_ttl)

    def _should_halt(self) -> str | None:
        if self._signal_stop:
            return "user_stop"
        if stop_flag(self.ctrl):
            return "user_stop"
        if self.ctrl.global_state() == GState.CANCELLED:
            return "cancelled"
        return None

    def _wait(self, seconds: float, why: str) -> str | None:
        """Interruptible sleep; returns a halt reason if one appears while waiting."""
        if seconds <= 0:
            return self._should_halt()
        self._event("backoff_wait" if why.startswith("backoff") else "wait", {"seconds": seconds, "why": why})
        end = self.clock() + seconds
        while True:
            h = self._should_halt()
            if h:
                return h
            if not self._renew():
                return "lease_lost"
            left = end - self.clock()
            if left <= 0:
                return None
            self._sleep(min(self.poll_interval, left))

    def _take_over_dead_lease(self) -> None:
        """A supervisor killed with SIGKILL can't release its lease. If the holder's PID (same host)
        no longer exists, the lease is stale and may be taken over; a live holder is never displaced."""
        row = self.ctrl.store.q1("SELECT owner FROM leases WHERE name='supervisor'")
        if row is None or not row["owner"].startswith("supervisor:"):
            return
        try:
            pid = int(row["owner"].rsplit(":", 1)[1])
        except ValueError:
            return
        if pid != os.getpid() and not _pid_alive(pid):
            with self.ctrl.store.tx():
                self.ctrl.store.x("DELETE FROM leases WHERE name='supervisor' AND owner=?", (row["owner"],))
            self._event("stale_lease_taken_over", {"previous_owner": row["owner"]})

    def _install_signals(self) -> None:
        def handler(signum, frame):
            self._signal_stop = f"signal {signum}"
        for s in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(s, handler)
            except ValueError:  # not in main thread (tests)
                pass

    # ------------------------------------------------------------ crash recovery
    def recover_orphans(self) -> list[dict]:
        """Dispatches left 'running' by a supervisor that died: kill any surviving agent process,
        mark interrupted (resumable if a session id was checkpointed), and finish lost post-steps."""
        out = []
        for d in self.ctrl.store.q("SELECT * FROM dispatches WHERE status='running' AND sup_run_id != ?",
                                   (self.run_id,)):
            killed = False
            if _pid_alive(d["child_pid"]):
                _kill_tree(d["child_pid"], signal.SIGTERM)
                killed = True
            with self.ctrl.store.tx():
                self.ctrl.store.x("UPDATE dispatches SET status='interrupted', ended_at=?, error=?, "
                                  "recovery_reason=? WHERE dispatch_id=?",
                                  (now_iso(), "supervisor process died during this dispatch",
                                   "orphaned by supervisor crash" + ("; agent process terminated" if killed else ""),
                                   d["dispatch_id"]))
            out.append({"dispatch_id": d["dispatch_id"], "session_id": d["session_id"], "killed_child": killed})
        for r in self.ctrl.store.q("SELECT run_id FROM supervisor_runs WHERE ended_at IS NULL AND run_id != ?",
                                   (self.run_id,)):
            with self.ctrl.store.tx():
                self.ctrl.store.x("UPDATE supervisor_runs SET ended_at=?, stop_reason='crashed' WHERE run_id=?",
                                  (now_iso(), r["run_id"]))
        # finished dispatches whose verification step was lost: verify now, never re-dispatch
        pending = self.ctrl.store.q("SELECT dispatch_id FROM dispatches WHERE verify_run_id IS NULL AND "
                                    "status NOT IN ('running','cancelled')")
        if pending:
            res = self._verify("post-dispatch verification lost in crash")
            with self.ctrl.store.tx():
                for d in pending:
                    self.ctrl.store.x("UPDATE dispatches SET verify_run_id=?, state_after=? WHERE dispatch_id=?",
                                      (res.get("run_id"), res.get("state"), d["dispatch_id"]))
        if out:
            self._event("orphans_recovered", {"dispatches": out})
        return out

    # ------------------------------------------------------------ accounting
    def _totals(self) -> dict:
        r = self.ctrl.store.q1("SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) c FROM dispatches WHERE policy_version=?",
                               (self.policy_version,))
        return {"dispatches": r["n"], "cost": r["c"]}

    def _tail_streak(self, rows: list, statuses: set[str]) -> int:
        n = 0
        for d in rows:
            if d["status"] in statuses:
                n += 1
            else:
                break
        return n

    def _verify(self, why: str) -> dict:
        try:
            return self.ctrl.verify(actor=f"supervisor:{self.run_id}")
        except PermissionDenied as e:
            return {"state": self.ctrl.global_state().value, "error": str(e)}

    # ------------------------------------------------------------ main loop
    def run(self, max_iterations: int | None = None) -> dict:
        if stop_flag(self.ctrl):
            raise PermissionDenied("supervisor was stopped by a human; run `rvd supervise clear-stop --by <name>` first")
        self._take_over_dead_lease()
        if not self._renew():
            raise SupervisorError("another supervisor holds the lease for this project")
        self._install_signals()
        start = self.clock()
        with self.ctrl.store.tx():
            self.ctrl.store.x("INSERT INTO supervisor_runs(run_id,pid,started_at,policy_version) VALUES(?,?,?,?)",
                              (self.run_id, os.getpid(), now_iso(), self.policy_version))
        self._event("supervisor_started", {"policy_version": self.policy_version, "adapter": self.adapter.name})
        reason = None
        try:
            self.recover_orphans()
            it = 0
            while reason is None:
                it += 1
                if max_iterations is not None and it > max_iterations:
                    reason = "max_iterations"
                    break
                reason = self.step(start)
        except Exception as e:
            reason = "harness_error"
            self._event("supervisor_error", {"error": repr(e)})
            raise
        finally:
            with self.ctrl.store.tx():
                self.ctrl.store.x("UPDATE supervisor_runs SET ended_at=?, stop_reason=? WHERE run_id=?",
                                  (now_iso(), reason, self.run_id))
            self._event("supervisor_stopped", {"reason": reason, **self._totals()})
            self.ctrl.store.release_lease("supervisor", self.owner)
        return {"run_id": self.run_id, "stop_reason": reason, "state": self.ctrl.global_state().value,
                **self._totals()}

    def step(self, start: float) -> str | None:
        if not self._renew():
            return "lease_lost"
        h = self._should_halt()
        if h:
            return h
        dec = self.ctrl.refresh(f"supervisor:{self.run_id}")
        st = GState(dec["state"])
        if st == GState.SATISFIED:
            return "satisfied"
        if st == GState.ERROR:
            return "harness_error"
        pol, tot = self.policy, self._totals()
        if self.clock() - start >= pol["time_budget_sec"]:
            return "time_budget"
        if tot["cost"] >= pol["cost_budget_usd"]:
            return "cost_budget"
        nxt = self.ctrl.next()
        a = nxt["action"]
        if a == "verify":
            self._verify("next action is verify")
            return None
        if a == "await_resource":
            return "blocked"
        if a == "escalate":
            return "needs_human"
        if a != "recover":
            return "no_action"
        issue_id = nxt["issue_id"]

        hist = [dict(r) for r in self.ctrl.store.q(
            "SELECT * FROM dispatches WHERE policy_version=? ORDER BY started_at DESC", (self.policy_version,))]
        ihist = [d for d in hist if d["issue_id"] == issue_id]
        if any(d["status"] == "permission_denied" for d in ihist):
            self._event("escalate_permission", {"issue_id": issue_id, "denials": json.loads(
                next(d for d in ihist if d["status"] == "permission_denied")["permission_denials"])})
            return "permission_denied"
        if tot["dispatches"] >= pol["max_dispatches"]:
            return "dispatch_budget"
        rl = self._tail_streak(hist, {"rate_limited"})
        if rl:
            if rl > pol["max_api_retries"]:
                return "rate_limit_budget"
            delay = min(pol["backoff_base_sec"] * (2 ** (rl - 1)), pol["backoff_max_sec"])
            h = self._wait(delay, f"backoff after {rl} rate-limited execution(s)")
            if h:
                return h
        crashes = self._tail_streak(ihist, {"interrupted", "timeout", "failed"})
        if crashes > pol["max_crash_retries"]:
            return "crash_budget"

        last = ihist[0] if ihist else None
        resume_sid = None
        recovery = "new issue work" if last is None else f"previous dispatch {last['status']}"
        if (last and last["status"] in ("interrupted", "rate_limited", "timeout") and last["session_id"]
                and self.adapter.capabilities.resume):
            resume_sid = last["session_id"]
        return self._dispatch(nxt, resume_sid, last, recovery)

    def _action_key(self, issue_id: str) -> str:
        row = self.ctrl.store.q1("SELECT attempts, last_fingerprint FROM issues WHERE issue_id=?", (issue_id,))
        return hashlib.sha256(
            f"{self.ctrl.contract.version}|{issue_id}|{row['attempts'] if row else 0}|"
            f"{row['last_fingerprint'] if row else ''}|{self.ctrl.progress_fingerprint()}".encode()).hexdigest()[:24]

    def _instruction(self, nxt: dict, resumed: bool, last: dict | None) -> str:
        g = self.ctrl.contract
        head = (f"Your previous run on this task ended abnormally ({last['status']}: {last['error'] or 'no detail'}). "
                f"Continue where you left off.\n\n") if resumed else ""
        return (f"{head}You are an RVD worker continuing goal `{g.goal_id}`: {g.intent.strip()}\n"
                f"Work ONLY on issue {nxt['issue_id']} ({nxt['kind']}) affecting {nxt['requirements']}:\n"
                f"{nxt['instructions']}\n\n"
                "When you believe it is fixed, run `rvd verify` and report what the evidence says. "
                "Completion is decided by RVD evidence, not by your summary. Do not edit anything under .rvd/, "
                "do not weaken tests or acceptance criteria. If you are blocked by a missing permission, tool "
                "or resource, stop and state exactly what is needed — do not work around permissions.")

    def _dispatch(self, nxt: dict, resume_sid: str | None, last: dict | None, recovery: str) -> str | None:
        issue_id = nxt["issue_id"]
        key = self._action_key(issue_id)
        dup = self.ctrl.store.q1("SELECT * FROM dispatches WHERE action_key=? AND status IN "
                                 "('succeeded','permission_denied','failed')", (key,))
        if dup is not None:
            # The same action against the same state already ran to completion: don't repeat it.
            self._event("dispatch_skipped_duplicate", {"action_key": key, "dispatch_id": dup["dispatch_id"]})
            if dup["verify_run_id"] is None:
                self._verify("finish duplicate's verification")
                return None
            return "needs_human"
        resumed = resume_sid is not None
        instruction = self._instruction(nxt, resumed, last)
        context = build_context(self.ctrl)
        art = artifact_hash(self.ctrl.root, self.ctrl.contract.completion_policy.get("inputs") or None)
        did = new_id("dsp")
        with self.ctrl.store.tx():
            self.ctrl.store.x(
                "INSERT INTO dispatches(dispatch_id,sup_run_id,action_key,issue_id,kind,adapter,mode,resumed_from,"
                "status,started_at,recovery_reason,checkpoint_json,policy_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, self.run_id, key, issue_id, nxt["kind"], self.adapter.name, "resume" if resumed else "new",
                 resume_sid, "running", now_iso(), recovery,
                 json.dumps({"artifact_hash": art, "next": nxt, "instruction": instruction}, ensure_ascii=False),
                 self.policy_version))
        self._event("dispatch_started", {"dispatch_id": did, "issue_id": issue_id, "mode": "resume" if resumed
                                         else "new", "resume_session": resume_sid, "reason": recovery})
        spec = {"project_root": str(self.ctrl.root), "instruction": instruction, "issue_id": issue_id}
        try:
            if resumed:
                if hasattr(self.adapter, "resume_session"):
                    r = self.adapter.resume_session(resume_sid, self.ctrl.root, instruction)
                else:
                    r = self.adapter.request_resume(last["execution_id"], instruction)
                if not r.get("resumed"):
                    eid = self.adapter.start_task(spec, context)
                    with self.ctrl.store.tx():
                        self.ctrl.store.x("UPDATE dispatches SET mode='new', recovery_reason=? WHERE dispatch_id=?",
                                          (recovery + f"; resume unavailable ({r.get('reason')}), new session",
                                           did))
                else:
                    eid = r["execution_id"]
            else:
                eid = self.adapter.start_task(spec, context)
        except Exception as e:
            with self.ctrl.store.tx():
                self.ctrl.store.x("UPDATE dispatches SET status='failed', ended_at=?, error=?, verify_run_id='-' "
                                  "WHERE dispatch_id=?", (now_iso(), f"adapter could not start: {e!r}", did))
            self._event("dispatch_start_failed", {"dispatch_id": did, "error": repr(e)})
            return "harness_error"

        t0 = self.clock()
        halt = None
        while True:
            status = self.adapter.poll_or_wait(eid, timeout=self.poll_interval)
            info = self.adapter.execution_info(eid)
            with self.ctrl.store.tx():
                self.ctrl.store.x("UPDATE dispatches SET execution_id=?, child_pid=COALESCE(?,child_pid), "
                                  "session_id=COALESCE(?,session_id) WHERE dispatch_id=?",
                                  (eid, info.get("pid"), info.get("session_id"), did))
            if status != "running":
                break
            if not self._renew():
                halt = "lease_lost"
            halt = halt or self._should_halt()
            if halt is None and self.clock() - t0 > self.policy["execution_timeout_sec"]:
                halt = "execution_timeout"
            if halt:
                self.adapter.cancel(eid)
                self.adapter.poll_or_wait(eid, timeout=30)
                break
            self._sleep(0)  # yield (poll_or_wait already waited)

        art_out = self.adapter.collect_artifacts(eid)
        o = art_out["outcome"]
        if halt == "execution_timeout":
            o["status"] = "timeout"
        elif halt and o["status"] in ("cancelled", "running", "interrupted"):
            o["status"] = "cancelled"
        with self.ctrl.store.tx():
            self.ctrl.store.x("UPDATE dispatches SET status=?, ended_at=?, cost_usd=?, error=?, permission_denials=?, "
                              "session_id=COALESCE(?,session_id) WHERE dispatch_id=?",
                              (o["status"], now_iso(), o.get("cost_usd") or 0, (o.get("error") or "")[:1000],
                               json.dumps(o.get("permission_denials") or []), o.get("session_id"), did))
        sess = o.get("session_id") or eid
        key_s = self.ctrl.register_session(self.adapter.name, sess, bool(o.get("resumable")))
        self.ctrl.end_session(key_s, o["status"])
        self.ctrl.record_claim("supervisor", self.adapter.name, art_out.get("claim", ""), key_s)
        self._event("dispatch_finished", {"dispatch_id": did, "status": o["status"], "cost_usd": o.get("cost_usd"),
                                          "session_id": o.get("session_id")})

        if o["status"] == "cancelled":
            with self.ctrl.store.tx():
                self.ctrl.store.x("UPDATE dispatches SET verify_run_id='-', state_after=? WHERE dispatch_id=?",
                                  (self.ctrl.global_state().value, did))
            return halt if halt in TERMINAL_STOPS else (self._should_halt() or "user_stop")
        # Independent verification. Pure API failures with no code change don't count as an attempt.
        changed = artifact_hash(self.ctrl.root, self.ctrl.contract.completion_policy.get("inputs") or None) != art
        if o["status"] in ("rate_limited", "interrupted", "timeout") and not changed:
            res = self.ctrl.refresh(f"supervisor:{self.run_id}")
            vid = "-"
        else:
            res = self._verify("post-dispatch")
            vid = res.get("run_id", "-")
        with self.ctrl.store.tx():
            self.ctrl.store.x("UPDATE dispatches SET verify_run_id=?, state_after=? WHERE dispatch_id=?",
                              (vid, res.get("state"), did))
        if halt in ("lease_lost",):
            return halt
        return self._wait(self.policy["cooldown_sec"], "cooldown")


def supervisor_report_rows(ctrl: Controller) -> list[dict]:
    return [dict(r) for r in ctrl.store.q(
        "SELECT dispatch_id, sup_run_id, issue_id, adapter, mode, resumed_from, session_id, status, started_at, "
        "ended_at, cost_usd, error, recovery_reason, verify_run_id, state_after, policy_version, permission_denials "
        "FROM dispatches ORDER BY started_at")]


def supervisor_report_markdown(ctrl: Controller) -> str:
    runs = ctrl.store.q("SELECT * FROM supervisor_runs ORDER BY started_at")
    rows = supervisor_report_rows(ctrl)
    if not runs and not rows:
        return ""
    lines = ["## Supervisor", "", "| run | started | ended | stop reason | policy |", "|---|---|---|---|---|"]
    for r in runs:
        lines.append(f"| {r['run_id']} | {r['started_at'][:19]} | {(r['ended_at'] or 'running')[:19]} | "
                     f"{r['stop_reason'] or '-'} | v{r['policy_version']} |")
    lines += ["", "| # | dispatch | issue | mode | session | outcome | cost $ | recovery reason | verification → state |",
              "|---|---|---|---|---|---|---|---|---|"]
    for i, d in enumerate(rows, 1):
        mode = d["mode"] + (f" ←{(d['resumed_from'] or '')[:8]}" if d["resumed_from"] else "")
        lines.append(f"| {i} | {d['dispatch_id']} | {d['issue_id']} | {mode} | {(d['session_id'] or '-')[:12]} | "
                     f"{d['status']}{(': ' + d['error'][:60]) if d['error'] else ''} | {d['cost_usd']:.3f} | "
                     f"{d['recovery_reason']} | {d['verify_run_id'] or '-'} → {d['state_after'] or '-'} |")
    return "\n".join(lines) + "\n"
