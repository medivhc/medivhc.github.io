"""Evidence execution and requirement-sufficiency rules.

Sufficiency is a relation between a *specific* requirement and a piece of evidence
(method ∈ requirement.accept.methods, passed, fresh, same contract version), not a
summable score.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import Check, GoalContract
from .environment import probe_requires
from .state_machine import RStatus
from .storage import Store, new_id, now_iso

EXCLUDE_DIRS = {".rvd", ".git", "__pycache__", "node_modules", ".pytest_cache", ".venv", "venv", "build", "out"}


def artifact_hash(project_root: Path, include: list[str] | None = None) -> str:
    """Content hash of the project's working tree (excluding .rvd and build outputs).

    Uses `git ls-files -co --exclude-standard` when available so .gitignore'd build
    artefacts don't make evidence stale; otherwise walks the tree.
    """
    root = Path(project_root)
    files: list[str] = []
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root,
                             capture_output=True, timeout=20)
        if out.returncode == 0:
            files = [f for f in out.stdout.decode().split("\0") if f]
    except (OSError, subprocess.TimeoutExpired):
        files = []
    if not files:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for fn in filenames:
                files.append(os.path.relpath(os.path.join(dirpath, fn), root))
    files = [f for f in files if not any(part in EXCLUDE_DIRS for part in Path(f).parts)]
    if include:
        files = [f for f in files if any(fnmatch.fnmatch(f, pat) for pat in include)]
    h = hashlib.sha256()
    for f in sorted(set(files)):
        p = root / f
        if not p.is_file():
            continue
        h.update(f.encode()); h.update(b"\0")
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:20]


@dataclass
class CheckRun:
    check_id: str
    ran: bool
    evidence_id: str | None
    passed: bool | None
    alternative_id: str | None
    method: str | None
    skipped: list[dict]  # alternatives not run, with reasons


def _coverage_text(contract: GoalContract, proves: list[str], method: str) -> tuple[list[str], list[str], list[str]]:
    full, partial, none = [], [], []
    for rid in proves:
        r = contract.requirement(rid)
        if method in r.methods:
            full.append(rid)
        elif method in r.partial_methods:
            partial.append(rid)
        else:
            none.append(rid)
    return full, partial, none


def run_check(store: Store, contract: GoalContract, check: Check, project_root: Path,
              evidence_dir: Path, run_id: str, env_fp: str, art_hash: str,
              allow_risk: set[str] | None = None) -> CheckRun:
    allow_risk = allow_risk or {"low", "medium"}
    skipped: list[dict] = []
    chosen = None
    for alt in check.alternatives:
        probe = probe_requires(alt.requires)
        full, partial, none = _coverage_text(contract, check.proves, alt.method)
        feasible = probe.runnable and alt.risk in allow_risk
        reason = probe.reason() if not probe.runnable else (
            "runnable" if alt.risk in allow_risk else f"risk={alt.risk} needs authorization")
        store.x("INSERT INTO strategy_proposals(id,check_id,alternative_id,method,coverage_json,remaining_gaps_json,"
                "feasible,reason,ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("sp"), check.id, alt.id, alt.method,
                 json.dumps({"full": full, "partial": partial}), json.dumps(none + ([alt.limitations] if alt.limitations else [])),
                 int(feasible), reason, now_iso()))
        if feasible and chosen is None:
            chosen = alt
        elif not feasible:
            skipped.append({"alternative": alt.id, "method": alt.method, "reason": reason})
    if chosen is None:
        return CheckRun(check.id, False, None, None, None, None, skipped)

    timeout = check.timeout_sec or contract.budget["check_timeout_sec"]
    evidence_dir.mkdir(parents=True, exist_ok=True)
    log_path = evidence_dir / f"{check.id}.{chosen.id}.log"
    t0 = time.time()
    env = dict(os.environ, RVD_PROJECT_ROOT=str(project_root), RVD_CHECK_ID=check.id)
    try:
        proc = subprocess.run(chosen.command, shell=True, cwd=project_root, capture_output=True,
                              timeout=timeout, env=env)
        exit_code: int | None = proc.returncode
        body = proc.stdout + b"\n--- stderr ---\n" + proc.stderr
    except subprocess.TimeoutExpired as e:
        exit_code = None
        body = (e.stdout or b"") + b"\n--- stderr ---\n" + (e.stderr or b"") + f"\n[rvd] TIMEOUT after {timeout}s".encode()
    header = (f"# rvd evidence\n# check={check.id} alternative={chosen.id} method={chosen.method}\n"
              f"# command={chosen.command}\n# cwd={project_root}\n# artifact_hash={art_hash} env={env_fp}\n"
              f"# exit_code={exit_code} duration_s={time.time() - t0:.2f}\n\n").encode()
    data = header + body
    log_path.write_bytes(data)
    passed = exit_code == 0
    full, partial, none = _coverage_text(contract, check.proves, chosen.method)
    claim = (f"{check.id} {'PASSED' if passed else 'FAILED'} via {chosen.method}; "
             f"fully supports {full or '-'}; partially supports {partial or '-'}")
    lim = [chosen.limitations] if chosen.limitations else []
    for rid in partial:
        lim.append(f"{rid}: {chosen.method} is only partial evidence (needs one of {contract.requirement(rid).methods})")
    for rid in none:
        lim.append(f"{rid}: {chosen.method} cannot support this requirement")
    if skipped:
        lim.append("not run: " + "; ".join(f"{s['alternative']} ({s['reason']})" for s in skipped))
    eid = new_id("ev")
    store.x("INSERT INTO evidence(evidence_id,check_id,alternative_id,requirement_ids,method,env_fingerprint,artifact_hash,"
            "command,exit_code,timestamp,log_path,log_sha256,passed,claim_supported,limitations,verifier,contract_version,run_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, check.id, chosen.id, json.dumps(check.proves), chosen.method, env_fp, art_hash, chosen.command,
             exit_code, now_iso(), str(log_path), hashlib.sha256(data).hexdigest(), int(passed), claim,
             " | ".join(lim), "rvd.evidence.run_check", contract.version, run_id))
    return CheckRun(check.id, True, eid, passed, chosen.id, chosen.method, skipped)


def latest_evidence_by_check(store: Store) -> dict[str, dict]:
    rows = store.q("SELECT e.* FROM evidence e JOIN (SELECT check_id, MAX(timestamp) ts FROM evidence GROUP BY check_id) m "
                   "ON e.check_id=m.check_id AND e.timestamp=m.ts")
    return {r["check_id"]: dict(r) for r in rows}


def evidence_is_valid(ev: dict) -> bool:
    """Evidence log must still exist and match its recorded hash (tamper/loss detection)."""
    p = Path(ev["log_path"])
    if not p.is_file():
        return False
    return hashlib.sha256(p.read_bytes()).hexdigest() == ev["log_sha256"]


def compute_requirement_statuses(store: Store, contract: GoalContract, current_artifact: str,
                                 blocked_checks: dict[str, list[dict]]) -> dict[str, tuple[RStatus, dict]]:
    """Pure-ish computation of each requirement's status from stored evidence.

    blocked_checks: check_id -> skipped alternatives (from the latest probe) for checks
    that had no runnable alternative.
    """
    latest = latest_evidence_by_check(store)
    out: dict[str, tuple[RStatus, dict]] = {}
    for r in contract.requirements:
        rel = [c for c in contract.checks if r.id in c.proves]
        detail: dict = {"checks": {}, "gaps": []}
        full_pass = partial_pass = failed = stale = False
        blocked_any = False
        for c in rel:
            ev = latest.get(c.id)
            if ev is None:
                if c.id in blocked_checks:
                    blocked_any = True
                    detail["checks"][c.id] = {"state": "blocked", "skipped": blocked_checks[c.id]}
                else:
                    detail["checks"][c.id] = {"state": "not_run"}
                continue
            fresh = (ev["artifact_hash"] == current_artifact and ev["contract_version"] == contract.version
                     and evidence_is_valid(ev))
            st = {"evidence_id": ev["evidence_id"], "method": ev["method"], "passed": bool(ev["passed"]),
                  "fresh": fresh, "log": ev["log_path"]}
            detail["checks"][c.id] = st
            if not fresh:
                stale = True
                continue
            if not ev["passed"]:
                failed = True
            elif ev["method"] in r.methods:
                full_pass = True
            elif ev["method"] in r.partial_methods:
                partial_pass = True
            if c.id in blocked_checks:
                blocked_any = True
        if not rel:
            detail["gaps"].append("no check defined that can prove this requirement")
        if failed:
            status = RStatus.FAILED
        elif full_pass:
            status = RStatus.VERIFIED
        elif partial_pass:
            status = RStatus.PARTIALLY_VERIFIED
            detail["gaps"].append(f"needs evidence via one of {r.methods}")
        elif stale:
            status = RStatus.STALE
            detail["gaps"].append("evidence exists but is stale (code, contract or log changed) — re-run `rvd verify`")
        elif blocked_any:
            status = RStatus.BLOCKED
            detail["gaps"].append(f"no runnable way to obtain {r.methods} evidence in this environment")
        else:
            status = RStatus.UNVERIFIED
        out[r.id] = (status, detail)
    return out
