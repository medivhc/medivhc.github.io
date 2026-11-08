"""Environment capability probing. Facts only — never fabricated.

`probe_requires` answers: can this alternative actually run here, and if not, why?
The answer is an ENVIRONMENT_FACT; the harness may look for other environments or
methods, but must not pretend the fact is otherwise.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass, field


@dataclass
class ProbeResult:
    runnable: bool
    missing: list[str] = field(default_factory=list)

    def reason(self) -> str:
        return "runnable" if self.runnable else "missing: " + ", ".join(self.missing)


def _arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


def probe_requires(requires: dict | None, env: dict | None = None) -> ProbeResult:
    env = os.environ if env is None else env
    requires = requires or {}
    missing: list[str] = []
    for tool in requires.get("tools", []):
        if shutil.which(tool, path=env.get("PATH")) is None:
            missing.append(f"tool:{tool}")
    for pat in requires.get("paths", []):
        if not glob.glob(os.path.expanduser(pat)):
            missing.append(f"path:{pat}")
    for var in requires.get("env", []):
        if not env.get(var):
            missing.append(f"env:{var}")
    archs = requires.get("arch", [])
    if archs and _arch() not in archs:
        missing.append(f"arch:{'|'.join(archs)} (host is {_arch()})")
    return ProbeResult(runnable=not missing, missing=missing)


INTERESTING_TOOLS = [
    "git", "python3", "gcc", "cc", "clang", "make", "cmake", "cargo", "go", "node",
    "aarch64-linux-gnu-gcc", "arm-linux-gnueabihf-gcc", "zig",
    "qemu-aarch64", "qemu-aarch64-static", "qemu-system-aarch64", "docker", "podman", "ssh", "file",
]


def capability_snapshot() -> dict:
    tools = {t: shutil.which(t) is not None for t in INTERESTING_TOOLS}
    devices = sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")))
    return {
        "arch": _arch(),
        "os": platform.system().lower(),
        "python": sys.version.split()[0],
        "tools": tools,
        "serial_devices": devices,
        "in_container": os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"),
        "remote_hosts_configured": sorted(k for k in os.environ if k.startswith("RVD_REMOTE_")),
    }


def fingerprint(snapshot: dict | None = None) -> str:
    snap = snapshot or capability_snapshot()
    core = {"arch": snap["arch"], "os": snap["os"], "python": snap["python"], "tools": snap["tools"],
            "serial_devices": snap["serial_devices"]}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]
