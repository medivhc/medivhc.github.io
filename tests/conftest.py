import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
EXAMPLES = PLUGIN / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "RVD_AGENT_CONTEXT", "RVD_PROJECT_ROOT", "CLAUDE_PROJECT_DIR",
                        "RVD_STATE_DIR", "RVD_ALLOW_AGENT_CONFIRM")}
    env.update(extra or {})
    return env


@pytest.fixture(autouse=True)
def _human_context(monkeypatch):
    for k in ("CLAUDECODE", "RVD_AGENT_CONTEXT", "RVD_PROJECT_ROOT", "CLAUDE_PROJECT_DIR", "RVD_STATE_DIR",
              "RVD_ALLOW_AGENT_CONFIRM"):
        monkeypatch.delenv(k, raising=False)


class Proj:
    def __init__(self, root: Path):
        self.root = root

    def rvd(self, *args, env=None, check=None):
        p = subprocess.run([sys.executable, str(PLUGIN / "scripts" / "rvd.py"), *args], cwd=self.root,
                           capture_output=True, text=True, env=clean_env(env), timeout=300)
        if check is not None:
            assert p.returncode == check, f"rvd {args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
        return p

    def rvdj(self, *args, **kw):
        p = self.rvd("--json", *args, **kw)
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else None

    def hook(self, event: dict, env=None):
        ev = {"session_id": "sess-1", "transcript_path": "/dev/null", "cwd": str(self.root),
              "permission_mode": "default", **event}
        p = subprocess.run([sys.executable, str(PLUGIN / "scripts" / "hook_entry.py")], input=json.dumps(ev),
                           capture_output=True, text=True, env=clean_env(env), timeout=60)
        out = json.loads(p.stdout) if p.stdout.strip() else None
        return p.returncode, out, p.stderr

    def write(self, rel: str, text: str):
        (self.root / rel).write_text(text)

    def status(self):
        return self.rvdj("status")[1]

    def req(self, rid):
        return next(r for r in self.status()["requirements"] if r["id"] == rid)["status"]


@pytest.fixture
def make_project(tmp_path):
    def _make(name: str, init=True) -> Proj:
        dst = tmp_path / name
        shutil.copytree(EXAMPLES / name, dst, ignore=shutil.ignore_patterns("state.db*", "evidence", "reports", "build"))
        p = Proj(dst)
        if init:
            p.rvd("init", "--confirm-hard", "--by", "xiang", check=0)
        return p
    return _make


FIXED_CALC = '''
def add(a, b): return a + b
def sub(a, b): return a - b
def mul(a, b): return a * b
def div(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b
def mean(xs):
    return None if not xs else sum(xs) / len(xs)
'''
BROKEN_IMPORT_CALC = "import does_not_exist_module  # syntax ok, import fails\n" + FIXED_CALC
