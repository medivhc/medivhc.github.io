"""AgentAdapter boundary (§7.5.2).

Adapters never write authoritative state. They start/poll/cancel executions and hand back
an `Outcome` (observations) plus the agent's claim; the Controller verifies independently.

Every adapter classifies how an execution ended into one of:
  succeeded | failed | interrupted | rate_limited | permission_denied | cancelled | timeout
The Supervisor reacts to the class (retry/resume, back off, escalate, stop), never to prose.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

TERMINAL = {"succeeded", "failed", "interrupted", "rate_limited", "permission_denied", "cancelled", "timeout"}

# Environment that must never leak from the supervisor into an agent process: it would let the
# agent perform human-only actions (privilege widening).
SCRUB_ENV = ("RVD_ALLOW_AGENT_CONFIRM", "RVD_STATE_DIR")


@dataclass
class Capabilities:
    resume: bool = False
    cancel: bool = False
    hooks: bool = False
    tool_calls: bool = False
    non_interactive: bool = True


@dataclass
class Outcome:
    status: str
    session_id: str | None = None
    resumable: bool = False
    cost_usd: float = 0.0
    error: str = ""
    permission_denials: list = field(default_factory=list)
    exit_code: int | None = None
    claim: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Execution:
    execution_id: str
    status: str = "running"
    outcome: Outcome | None = None
    pid: int | None = None
    session_id: str | None = None


class AgentAdapter(ABC):
    name: str = "abstract"
    capabilities = Capabilities()

    @abstractmethod
    def start_task(self, task_spec: dict, context: dict) -> str: ...

    @abstractmethod
    def poll_or_wait(self, execution_id: str, timeout: float | None = None) -> str:
        """Return 'running' or a terminal status. Waits at most `timeout` seconds (None = until done)."""

    @abstractmethod
    def collect_artifacts(self, execution_id: str) -> dict: ...

    def request_resume(self, execution_id: str, instruction: str) -> dict:
        # Honest default: we cannot resume the original session; the caller must start a new
        # one with persisted context. Never pretend the original session was resumed.
        return {"resumed": False, "reason": f"{self.name} adapter does not support session resume"}

    def cancel(self, execution_id: str) -> dict:
        return {"cancelled": False, "reason": f"{self.name} adapter does not support cancel"}

    def execution_info(self, execution_id: str) -> dict:
        return {}


def build_context(ctrl) -> dict:
    """Persisted, adapter-neutral context for continuing a goal in any new session."""
    s = ctrl.status()
    return {"goal_id": s["goal_id"], "intent": s["intent"], "state": s["state"],
            "unmet": [r["id"] for r in s["requirements"] if r["mandatory"] and r["status"] != "VERIFIED"],
            "next": s["next"], "open_issues": [{k: i[k] for k in ("issue_id", "kind", "title", "suggested_action",
                                                                   "requirement_ids", "status")}
                                               for i in s["open_issues"]]}


def _child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    env.pop("CLAUDECODE", None)  # child agent CLIs set their own markers
    return env


def _kill_tree(pid: int, sig=signal.SIGTERM) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


# --------------------------------------------------------------------------- Mock
class MockAdapter(AgentAdapter):
    """Deterministic in-process worker (runs in a thread) for tests.

    worker(root, task_spec, context) may return a str (claim -> succeeded) or a dict of
    Outcome fields (e.g. {"status": "rate_limited"}). It can watch context["_cancel"]
    (a threading.Event) to emulate cooperative cancellation.
    """

    def __init__(self, name: str, worker: Callable[[Path, dict, dict], object], resume: bool = False):
        self.name = name
        self._worker = worker
        self.capabilities = Capabilities(resume=resume, cancel=True)
        self._runs: dict[str, Execution] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self.calls: list[dict] = []

    def _launch(self, task_spec: dict, context: dict, session_id: str) -> str:
        eid = f"{self.name}-{uuid.uuid4().hex[:8]}"
        ex = Execution(eid, session_id=session_id)
        self._runs[eid] = ex
        ev = threading.Event()
        self._cancel[eid] = ev
        self.calls.append({"execution_id": eid, "session_id": session_id, "task_spec": task_spec})

        def run():
            try:
                r = self._worker(Path(task_spec["project_root"]), task_spec, {**context, "_cancel": ev,
                                                                              "_session_id": session_id})
                if ev.is_set():
                    out = Outcome("cancelled", session_id, self.capabilities.resume)
                elif isinstance(r, dict):
                    out = Outcome(**{"session_id": session_id, "resumable": self.capabilities.resume, **r})
                else:
                    out = Outcome("succeeded", session_id, self.capabilities.resume, claim=str(r or ""))
            except Exception as e:  # a crashing worker is an interrupted execution, not a failed goal
                out = Outcome("interrupted", session_id, self.capabilities.resume, error=repr(e))
            ex.outcome, ex.status = out, out.status

        t = threading.Thread(target=run, daemon=True)
        self._threads[eid] = t
        t.start()
        return eid

    def start_task(self, task_spec: dict, context: dict) -> str:
        return self._launch(task_spec, context, f"mock-{uuid.uuid4().hex[:8]}")

    def request_resume(self, execution_id: str, instruction: str) -> dict:
        if not self.capabilities.resume:
            return super().request_resume(execution_id, instruction)
        prev = self._runs.get(execution_id)
        sid = prev.session_id if prev else execution_id
        spec = dict(self.calls[-1]["task_spec"]) if self.calls else {}
        spec["instruction"], spec["resume_session"] = instruction, sid
        return {"resumed": True, "execution_id": self._launch(spec, {}, sid)}

    def poll_or_wait(self, execution_id: str, timeout: float | None = None) -> str:
        self._threads[execution_id].join(timeout)
        return self._runs[execution_id].status

    def collect_artifacts(self, execution_id: str) -> dict:
        ex = self._runs[execution_id]
        o = ex.outcome or Outcome("interrupted", ex.session_id)
        return {"claim": o.claim, "outcome": o.to_dict()}

    def cancel(self, execution_id: str) -> dict:
        self._cancel[execution_id].set()
        return {"cancelled": True}

    def execution_info(self, execution_id: str) -> dict:
        return {"session_id": self._runs[execution_id].session_id}


# --------------------------------------------------------------------------- subprocess base
class _ProcAdapter(AgentAdapter):
    def __init__(self, timeout: float = 3600):
        self.timeout = timeout
        self._procs: dict[str, dict] = {}

    def _spawn(self, argv: list[str], cwd: Path, stdin_text: str | None, meta: dict) -> str:
        eid = f"{self.name}-{uuid.uuid4().hex[:10]}"
        out = tempfile.NamedTemporaryFile(prefix=f"rvd-{eid}-", suffix=".out", delete=False)
        err = tempfile.NamedTemporaryFile(prefix=f"rvd-{eid}-", suffix=".err", delete=False)
        p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                             stdout=out, stderr=err, env=_child_env(), start_new_session=True)
        if stdin_text is not None:
            try:
                p.stdin.write(stdin_text.encode()); p.stdin.close()
            except BrokenPipeError:
                pass
        self._procs[eid] = {"proc": p, "out": out.name, "err": err.name, "started": time.time(),
                            "cancelled": False, "timed_out": False, "cwd": str(cwd), **meta}
        return eid

    def poll_or_wait(self, execution_id: str, timeout: float | None = None) -> str:
        r = self._procs[execution_id]
        try:
            r["proc"].wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if time.time() - r["started"] > self.timeout:
                r["timed_out"] = True
                _kill_tree(r["proc"].pid)
                r["proc"].wait()
            else:
                return "running"
        return self._classify(execution_id).status

    def cancel(self, execution_id: str) -> dict:
        r = self._procs[execution_id]
        r["cancelled"] = True
        _kill_tree(r["proc"].pid)
        try:
            r["proc"].wait(timeout=15)
        except subprocess.TimeoutExpired:
            _kill_tree(r["proc"].pid, signal.SIGKILL)
        return {"cancelled": True}

    def collect_artifacts(self, execution_id: str) -> dict:
        o = self._classify(execution_id)
        return {"claim": o.claim, "outcome": o.to_dict()}

    def execution_info(self, execution_id: str) -> dict:
        r = self._procs[execution_id]
        return {"pid": r["proc"].pid, "session_id": self._peek_session(execution_id)}

    def _peek_session(self, execution_id: str) -> str | None:
        return None

    @abstractmethod
    def _classify(self, execution_id: str) -> Outcome: ...


# --------------------------------------------------------------------------- Claude Code headless
RATE_LIMIT_MARKERS = ("rate_limit", "rate limit", "429", "529", "overloaded")


class ClaudeCodeHeadlessAdapter(_ProcAdapter):
    """Claude Code via `claude -p --output-format stream-json --verbose`.

    The stream's first lines carry `session_id`, so even an abnormally terminated execution
    can be resumed with `claude -p --resume <session_id>`. Permissions are exactly what the
    accepted supervisor policy grants (`--allowedTools`, `--permission-mode`); this adapter
    never adds bypass flags.
    """

    name = "claude-code"
    capabilities = Capabilities(resume=True, cancel=True, hooks=True, tool_calls=True)
    FORBIDDEN_ARGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")

    def __init__(self, plugin_dir: Path, allowed_tools: str = "Bash Read Edit Write Glob Grep",
                 permission_mode: str = "default", max_turns: int | None = None, model: str | None = None,
                 extra_args: list[str] | None = None, timeout: float = 3600, claude_bin: str = "claude"):
        super().__init__(timeout)
        if permission_mode == "bypassPermissions":
            raise ValueError("supervisor policy may not use bypassPermissions")
        extra_args = list(extra_args or [])
        if any(a in self.FORBIDDEN_ARGS for a in extra_args):
            raise ValueError("supervisor policy may not pass permission-bypass flags")
        self.plugin_dir = Path(plugin_dir)
        self.claude_bin = claude_bin
        self.base = ["--output-format", "stream-json", "--verbose", "--plugin-dir", str(self.plugin_dir),
                     "--allowedTools", allowed_tools, "--permission-mode", permission_mode]
        if max_turns:
            self.base += ["--max-turns", str(max_turns)]
        if model:
            self.base += ["--model", model]
        self.base += extra_args

    def start_task(self, task_spec: dict, context: dict) -> str:
        if shutil.which(self.claude_bin) is None:
            raise FileNotFoundError("claude CLI not found on PATH")
        prompt = (f"{task_spec['instruction']}\n\nRVD context (authoritative state lives in the RVD store; your "
                  f"summary is not evidence):\n{json.dumps(context, ensure_ascii=False, indent=2)}")
        return self._spawn([self.claude_bin, "-p", prompt, *self.base], Path(task_spec["project_root"]), None, {})

    def request_resume(self, execution_id: str, instruction: str) -> dict:
        r = self._procs.get(execution_id)
        sid = self._peek_session(execution_id) if r else execution_id
        cwd = r["cwd"] if r else None
        if not sid or not cwd:
            return {"resumed": False, "reason": "no session id recorded for this execution"}
        eid = self._spawn([self.claude_bin, "-p", instruction, "--resume", sid, *self.base], Path(cwd), None,
                          {"resumed_session": sid})
        return {"resumed": True, "execution_id": eid}

    def resume_session(self, session_id: str, cwd: Path, instruction: str) -> dict:
        """Resume a session recorded by an earlier supervisor process (crash recovery)."""
        eid = self._spawn([self.claude_bin, "-p", instruction, "--resume", session_id, *self.base], Path(cwd), None,
                          {"resumed_session": session_id})
        return {"resumed": True, "execution_id": eid}

    def _lines(self, execution_id: str) -> list[dict]:
        out = []
        try:
            with open(self._procs[execution_id]["out"], errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            pass
        return out

    def _peek_session(self, execution_id: str) -> str | None:
        r = self._procs.get(execution_id, {})
        for e in self._lines(execution_id)[:20]:
            if e.get("session_id"):
                return e["session_id"]
        return r.get("resumed_session")

    def _classify(self, execution_id: str) -> Outcome:
        r = self._procs[execution_id]
        p = r["proc"]
        if p.poll() is None:
            return Outcome("running")
        lines = self._lines(execution_id)
        sid = self._peek_session(execution_id)
        res = next((e for e in reversed(lines) if e.get("type") == "result"), None)
        try:
            stderr = Path(r["err"]).read_text(errors="replace")[-4000:]
        except OSError:
            stderr = ""
        rl_rejected = any(e.get("type") == "rate_limit_event" and
                          str((e.get("rate_limit_info") or {}).get("status", "")).lower() in ("rejected", "blocked")
                          for e in lines)
        base = dict(session_id=sid, resumable=bool(sid), exit_code=p.returncode)
        if r["cancelled"]:
            return Outcome("cancelled", **base)
        if r["timed_out"]:
            return Outcome("timeout", error=f"exceeded {self.timeout}s", **base)
        if res is None:
            # killed / crashed before producing a result
            blob = stderr.lower()
            if rl_rejected or any(m in blob for m in RATE_LIMIT_MARKERS):
                return Outcome("rate_limited", error=stderr[-500:], **base)
            return Outcome("interrupted", error=f"no result line; exit {p.returncode}; {stderr[-500:]}", **base)
        cost = float(res.get("total_cost_usd") or 0)
        denials = res.get("permission_denials") or []
        claim = str(res.get("result") or "")[:4000]
        blob = (json.dumps(res.get("api_error_status")) + " " + claim[:500] + " " + stderr).lower()
        if res.get("is_error") and (rl_rejected or res.get("api_error_status") in (429, 529)
                                    or any(m in blob for m in RATE_LIMIT_MARKERS)):
            return Outcome("rate_limited", cost_usd=cost, error=str(res.get("api_error_status")), claim=claim, **base)
        if denials:
            return Outcome("permission_denied", cost_usd=cost, permission_denials=denials, claim=claim, **base)
        if res.get("is_error"):
            st = "interrupted" if res.get("subtype") in ("error_max_turns", "error_during_execution") else "failed"
            return Outcome(st, cost_usd=cost, error=str(res.get("subtype")), claim=claim, **base)
        return Outcome("succeeded", cost_usd=cost, claim=claim, **base)


# --------------------------------------------------------------------------- generic CLI agents
class ShellAdapter(_ProcAdapter):
    """Any non-interactive agent CLI (e.g. `codex exec`, `opencode run`, a script).

    The prompt is written to stdin and to $RVD_PROMPT_FILE. Exit-code convention:
      0 succeeded · 75 (EX_TEMPFAIL) rate_limited · 77 (EX_NOPERM) permission_denied ·
      killed by signal → interrupted · other → failed.  No session resume (resume=false).
    """

    capabilities = Capabilities(resume=False, cancel=True)

    def __init__(self, command: str, name: str = "shell", timeout: float = 3600):
        super().__init__(timeout)
        self.command = command
        self.name = name

    def start_task(self, task_spec: dict, context: dict) -> str:
        prompt = f"{task_spec['instruction']}\n\nRVD context:\n{json.dumps(context, ensure_ascii=False, indent=2)}"
        pf = tempfile.NamedTemporaryFile("w", prefix="rvd-prompt-", suffix=".md", delete=False)
        pf.write(prompt); pf.close()
        return self._spawn(["/bin/sh", "-c", self.command], Path(task_spec["project_root"]), prompt,
                           {"prompt_file": pf.name})

    def _spawn(self, argv, cwd, stdin_text, meta):  # inject RVD_PROMPT_FILE
        old = os.environ.get("RVD_PROMPT_FILE")
        if "prompt_file" in meta:
            os.environ["RVD_PROMPT_FILE"] = meta["prompt_file"]
        try:
            return super()._spawn(argv, cwd, stdin_text, meta)
        finally:
            if old is None:
                os.environ.pop("RVD_PROMPT_FILE", None)
            else:
                os.environ["RVD_PROMPT_FILE"] = old

    def _classify(self, execution_id: str) -> Outcome:
        r = self._procs[execution_id]
        rc = r["proc"].poll()
        if rc is None:
            return Outcome("running")
        try:
            claim = Path(r["out"]).read_text(errors="replace")[-4000:]
            err = Path(r["err"]).read_text(errors="replace")[-1000:]
        except OSError:
            claim, err = "", ""
        base = dict(exit_code=rc, claim=claim, error=err)
        if r["cancelled"]:
            return Outcome("cancelled", **base)
        if r["timed_out"]:
            return Outcome("timeout", **base)
        if rc == 0:
            return Outcome("succeeded", **base)
        if rc == 75:
            return Outcome("rate_limited", **base)
        if rc == 77:
            return Outcome("permission_denied", permission_denials=[{"detail": err[-300:]}], **base)
        if rc < 0:
            return Outcome("interrupted", **base)
        return Outcome("failed", **base)


def shlex_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)
