"""AgentAdapter boundary (§7.5.2).

Adapters never write authoritative state. They start/poll/cancel executions and hand
back artifacts and claims; the Controller verifies independently.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .controller import Controller


@dataclass
class Capabilities:
    resume: bool = False
    cancel: bool = False
    hooks: bool = False
    tool_calls: bool = False
    non_interactive: bool = True


@dataclass
class Execution:
    execution_id: str
    status: str = "running"  # running | finished | failed | cancelled
    claim: str = ""
    artifacts: list[str] = field(default_factory=list)


class AgentAdapter(ABC):
    name: str = "abstract"
    capabilities = Capabilities()

    @abstractmethod
    def start_task(self, task_spec: dict, context: dict) -> str: ...

    @abstractmethod
    def poll_or_wait(self, execution_id: str) -> str: ...

    @abstractmethod
    def collect_artifacts(self, execution_id: str) -> dict: ...

    def request_resume(self, execution_id: str, instruction: str) -> dict:
        # Honest default: we cannot resume the original session; caller must start a new
        # one with persisted context from `Controller.next()`/`status()`.
        return {"resumed": False, "reason": f"{self.name} adapter does not support session resume"}

    def cancel(self, execution_id: str) -> dict:
        return {"cancelled": False, "reason": f"{self.name} adapter does not support cancel"}


def build_context(ctrl: Controller) -> dict:
    """Persisted, adapter-neutral context for continuing a goal in any new session."""
    s = ctrl.status()
    return {"goal_id": s["goal_id"], "intent": s["intent"], "state": s["state"],
            "unmet": [r["id"] for r in s["requirements"] if r["mandatory"] and r["status"] != "VERIFIED"],
            "next": s["next"], "open_issues": [{k: i[k] for k in ("issue_id", "kind", "title", "suggested_action",
                                                                   "requirement_ids", "status")}
                                               for i in s["open_issues"]]}


class MockAdapter(AgentAdapter):
    """Deterministic in-process worker used to test cross-adapter hand-off."""

    def __init__(self, name: str, worker: Callable[[Path, dict, dict], str], resume: bool = False):
        self.name = name
        self._worker = worker
        self.capabilities = Capabilities(resume=resume, cancel=True)
        self._runs: dict[str, Execution] = {}
        self.project_root: Path | None = None

    def start_task(self, task_spec: dict, context: dict) -> str:
        eid = f"{self.name}-{uuid.uuid4().hex[:8]}"
        ex = Execution(eid)
        self._runs[eid] = ex
        try:
            ex.claim = self._worker(Path(task_spec["project_root"]), task_spec, context)
            ex.status = "finished"
        except Exception as e:  # a crashing worker is a failed execution, not a failed goal
            ex.status, ex.claim = "failed", f"worker crashed: {e!r}"
        return eid

    def poll_or_wait(self, execution_id: str) -> str:
        return self._runs[execution_id].status

    def collect_artifacts(self, execution_id: str) -> dict:
        ex = self._runs[execution_id]
        return {"claim": ex.claim, "artifacts": ex.artifacts}

    def cancel(self, execution_id: str) -> dict:
        self._runs[execution_id].status = "cancelled"
        return {"cancelled": True}


class ClaudeCodeHeadlessAdapter(AgentAdapter):
    """Claude Code via `claude -p` (P2 groundwork). Resume uses `--resume <session_id>`."""

    name = "claude-code"
    capabilities = Capabilities(resume=True, cancel=False, hooks=True, tool_calls=True)

    def __init__(self, plugin_dir: Path, extra_args: list[str] | None = None, timeout: int = 3600):
        self.plugin_dir = Path(plugin_dir)
        self.extra_args = extra_args or []
        self.timeout = timeout
        self._runs: dict[str, dict] = {}

    def _run(self, args: list[str], cwd: Path) -> dict:
        if shutil.which("claude") is None:
            return {"status": "failed", "error": "claude CLI not found"}
        p = subprocess.run(["claude", "-p", *args, "--output-format", "json", "--plugin-dir", str(self.plugin_dir),
                            *self.extra_args], cwd=cwd, capture_output=True, text=True, timeout=self.timeout)
        try:
            out = json.loads(p.stdout or "{}")
        except json.JSONDecodeError:
            out = {"raw": p.stdout}
        return {"status": "finished" if p.returncode == 0 else "failed", "result": out, "stderr": p.stderr[-4000:],
                "session_id": out.get("session_id")}

    def start_task(self, task_spec: dict, context: dict) -> str:
        prompt = (f"{task_spec['instruction']}\n\nRVD context (authoritative state lives in the RVD store; your summary "
                  f"is not evidence):\n{json.dumps(context, ensure_ascii=False, indent=2)}")
        r = self._run([prompt], Path(task_spec["project_root"]))
        eid = r.get("session_id") or f"cc-{uuid.uuid4().hex[:8]}"
        self._runs[eid] = r | {"cwd": task_spec["project_root"]}
        return eid

    def poll_or_wait(self, execution_id: str) -> str:
        return self._runs[execution_id]["status"]

    def collect_artifacts(self, execution_id: str) -> dict:
        r = self._runs[execution_id]
        return {"claim": json.dumps(r.get("result"))[:4000], "stderr": r.get("stderr", "")}

    def request_resume(self, execution_id: str, instruction: str) -> dict:
        cwd = Path(self._runs.get(execution_id, {}).get("cwd", "."))
        r = self._run(["--resume", execution_id, instruction], cwd)
        self._runs[execution_id] = r | {"cwd": str(cwd)}
        return {"resumed": r["status"] == "finished", "detail": r.get("stderr", "")[-500:]}
