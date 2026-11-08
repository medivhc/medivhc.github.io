"""Open-issue derivation, clustering, progress (information-gain) tracking and next actions.

Issues are derived deterministically from requirement statuses, so replaying the
same verification twice is idempotent: the same root-cause signature maps to the
same issue row (UNIQUE signature), never a duplicate.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .contracts import GoalContract
from .state_machine import RStatus
from .storage import Store, new_id, now_iso

PRIORITY = {"needs_verification": 0, "code_defect": 1, "missing_verification": 2, "evidence_gap": 3,
            "environment_gap": 4, "needs_physical_hardware_validation": 5}


def _sig(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


_EXC = re.compile(r"^(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Interrupt)):?\s*(.*)$", re.M)


def error_signature(log_path: str) -> str | None:
    """Normalized last exception line of a log, used to cluster same-root-cause failures."""
    try:
        text = Path(log_path).read_text(errors="replace")[-20000:]
    except OSError:
        return None
    m = _EXC.findall(text)
    if not m:
        return None
    exc, msg = m[-1]
    msg = re.sub(r"(/[^\s'\"]+)+", "<path>", msg)
    msg = re.sub(r"\b0x[0-9a-f]+\b|\b\d+\b", "<n>", msg)
    return f"{exc}: {msg[:160]}"


def derive_issue_specs(contract: GoalContract, statuses: dict[str, tuple[RStatus, dict]]) -> list[dict]:
    specs: dict[str, dict] = {}

    def add(sig: str, **kw):
        if sig in specs:
            specs[sig]["requirement_ids"] = sorted(set(specs[sig]["requirement_ids"]) | set(kw["requirement_ids"]))
            specs[sig]["check_ids"] = sorted(set(specs[sig]["check_ids"]) | set(kw["check_ids"]))
        else:
            specs[sig] = {"signature": sig, **kw}

    for r in contract.requirements:
        if not r.mandatory:
            continue
        st, d = statuses[r.id]
        if st == RStatus.VERIFIED:
            continue
        checks = d.get("checks", {})
        if st == RStatus.FAILED:
            for cid, c in checks.items():
                if c.get("fresh") and c.get("passed") is False:
                    esig = error_signature(c["log"])
                    # same normalized exception across checks => one root-cause cluster
                    sig = _sig("error", esig) if esig else _sig("check_failed", cid)
                    add(sig, kind="code_defect", requirement_ids=[r.id],
                        title=(f"failing with `{esig}`" if esig else f"check `{cid}` is failing"),
                        root_cause_hypothesis=f"see failing log {c['log']}",
                        suggested_action=(f"Read {c['log']}, find the root cause, make the minimal fix, then run "
                                          f"`rvd verify` (all checks, to catch regressions). "
                                          "Do not edit the goal contract or tests to make them pass."),
                        auto_allowed=1, check_ids=[cid], blocking_resource="")
        elif st in (RStatus.STALE, RStatus.UNVERIFIED) and any(c.get("state") != "blocked" for c in checks.values()):
            runnable = [cid for cid, c in checks.items() if c.get("state") != "blocked"]
            add(_sig("needs_verification", *sorted(runnable)), kind="needs_verification", requirement_ids=[r.id],
                title=f"requirement evidence is {st.value.lower()}",
                root_cause_hypothesis="no fresh evidence for the current code/contract",
                suggested_action="Run `rvd verify` to produce fresh evidence.", auto_allowed=1,
                check_ids=runnable, blocking_resource="")
        elif not checks:
            add(_sig("missing_verification", r.id), kind="missing_verification", requirement_ids=[r.id],
                title=f"{r.id} has no verification check",
                root_cause_hypothesis="the contract defines no way to obtain evidence for this requirement",
                suggested_action=("Design a check whose method is one of "
                                  f"{r.methods} and propose it with `rvd amend propose` (verification-only changes "
                                  "are auto-reviewed for coverage)."), auto_allowed=1, check_ids=[], blocking_resource="")
        else:
            # BLOCKED or PARTIALLY_VERIFIED: the accepted method can't run here.
            missing = sorted({m for c in checks.values() for s in c.get("skipped", [])
                              for m in s["reason"].replace("missing: ", "").split(", ")})
            hw = "real_hardware" in r.methods and len(r.methods) == 1
            kind = "needs_physical_hardware_validation" if hw else (
                "evidence_gap" if st == RStatus.PARTIALLY_VERIFIED else "environment_gap")
            add(_sig(kind, r.id), kind=kind, requirement_ids=[r.id],
                title=(f"{r.id} needs {'/'.join(r.methods)} evidence that this environment cannot produce"),
                root_cause_hypothesis="environment fact, not a code failure: " + (", ".join(missing) or "no runnable alternative"),
                suggested_action=("Provide the missing resource (" + (", ".join(missing) or "see `rvd status`") +
                                  ") or authorize an alternative environment; then `rvd verify`. Lower-tier evidence "
                                  "already gathered is kept but cannot close this requirement."),
                auto_allowed=0, check_ids=list(checks.keys()), blocking_resource=", ".join(missing))
    return list(specs.values())


def sync_issues(store: Store, contract: GoalContract, statuses: dict[str, tuple[RStatus, dict]],
                progress_fp: dict[str, str], ran_checks: set[str], actor: str) -> None:
    """Upsert derived issues; resolve vanished ones; update attempt/no-progress counters.

    progress_fp: check_id -> fingerprint of (artifact hash, outcome, method). An issue
    gets an attempt counted only when one of its checks actually ran this round; if the
    fingerprint of all its checks is unchanged from last time, that's no information gain.
    """
    budget = contract.budget
    specs = derive_issue_specs(contract, statuses)
    live = {s["signature"] for s in specs}
    ts = now_iso()
    for row in store.q("SELECT issue_id, signature, status FROM issues WHERE status IN ('open','blocked','exhausted')"):
        if row["signature"] not in live:
            store.x("UPDATE issues SET status='resolved', updated_at=? WHERE issue_id=?", (ts, row["issue_id"]))
            store.event(actor, "recovery", "issue_resolved", {"issue_id": row["issue_id"]}, contract.version)
    for s in specs:
        fp = _sig(*[progress_fp.get(c, "") for c in sorted(s["check_ids"])])
        ran = bool(set(s["check_ids"]) & ran_checks)
        row = store.q1("SELECT * FROM issues WHERE signature=?", (s["signature"],))
        base_status = "open" if s["auto_allowed"] else "blocked"
        if row is None:
            iid = new_id("iss")
            store.x("INSERT INTO issues(issue_id,signature,kind,status,title,root_cause_hypothesis,blocking_resource,"
                    "requirement_ids,suggested_action,auto_allowed,attempts,no_progress,last_fingerprint,tried_json,"
                    "next_condition,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, s["signature"], s["kind"], base_status, s["title"], s["root_cause_hypothesis"],
                     s["blocking_resource"], json.dumps(s["requirement_ids"]), s["suggested_action"], s["auto_allowed"],
                     1 if ran else 0, 0, fp, "[]",
                     ("when " + s["blocking_resource"] + " becomes available") if s["blocking_resource"] else "",
                     ts, ts))
            store.event(actor, "recovery", "issue_opened", {"issue_id": iid, "kind": s["kind"],
                                                             "requirements": s["requirement_ids"]}, contract.version)
            continue
        attempts, no_prog, status = row["attempts"], row["no_progress"], row["status"]
        if row["status"] == "resolved":
            status, attempts, no_prog = base_status, 0, 0  # regression: re-open with a fresh budget
            store.event(actor, "recovery", "issue_reopened", {"issue_id": row["issue_id"]}, contract.version)
        if ran:
            attempts += 1
            no_prog = no_prog + 1 if fp == row["last_fingerprint"] else 0
        prev_status = status
        status = base_status  # exhaustion is re-derived: genuine progress (new fingerprint) lifts it
        if s["auto_allowed"] and (no_prog >= budget["max_consecutive_no_progress"]
                                  or attempts >= budget["max_attempts_per_issue"]):
            if prev_status != "exhausted":
                store.event(actor, "recovery", "issue_exhausted",
                            {"issue_id": row["issue_id"], "attempts": attempts, "no_progress": no_prog},
                            contract.version)
            status = "exhausted"
        store.x("UPDATE issues SET kind=?, status=?, title=?, root_cause_hypothesis=?, blocking_resource=?, "
                "requirement_ids=?, suggested_action=?, auto_allowed=?, attempts=?, no_progress=?, last_fingerprint=?, "
                "updated_at=? WHERE issue_id=?",
                (s["kind"], status, s["title"], s["root_cause_hypothesis"], s["blocking_resource"],
                 json.dumps(s["requirement_ids"]), s["suggested_action"], s["auto_allowed"], attempts, no_prog,
                 fp, ts, row["issue_id"]))


def open_issues(store: Store) -> list[dict]:
    rows = [dict(r) for r in store.q("SELECT * FROM issues WHERE status != 'resolved'")]
    for r in rows:
        r["requirement_ids"] = json.loads(r["requirement_ids"])
    rows.sort(key=lambda r: (r["status"] != "open", PRIORITY.get(r["kind"], 9), r["created_at"]))
    return rows


def next_action(store: Store, global_state: str) -> dict:
    if global_state == "SATISFIED":
        return {"action": "none", "reason": "goal satisfied"}
    if global_state == "CANCELLED":
        return {"action": "none", "reason": "task cancelled by user; automation must not restart it"}
    issues = open_issues(store)
    actionable = [i for i in issues if i["status"] == "open"]
    if actionable:
        i = actionable[0]
        return {"action": "recover" if i["kind"] != "needs_verification" else "verify",
                "issue_id": i["issue_id"], "kind": i["kind"], "requirements": i["requirement_ids"],
                "instructions": i["suggested_action"], "attempts": i["attempts"], "no_progress": i["no_progress"]}
    exhausted = [i for i in issues if i["status"] == "exhausted"]
    if exhausted:
        i = exhausted[0]
        return {"action": "escalate", "issue_id": i["issue_id"], "kind": i["kind"],
                "requirements": i["requirement_ids"],
                "instructions": ("Repeated attempts produced no new information. Change strategy: re-decompose the "
                                 "problem, try a different verification path, or hand the minimal decision to a human. "
                                 "Use `rvd recover " + i["issue_id"] + " --reset` after a genuinely new approach.")}
    blocked = [i for i in issues if i["status"] == "blocked"]
    if blocked:
        return {"action": "await_resource", "issues": [
            {"issue_id": i["issue_id"], "requirements": i["requirement_ids"], "needs": i["blocking_resource"],
             "next_condition": i["next_condition"]} for i in blocked]}
    return {"action": "verify", "reason": "no evidence yet"}
