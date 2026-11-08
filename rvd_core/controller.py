"""TaskController: the single authority for state transitions.

Everything else (hooks, skills, adapters, the CLI) goes through this class.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

from . import environment
from .contracts import (ContractError, GoalContract, contract_from_json, diff_contracts,
                        hard_protected_requirements, load_contract_file, review_verification_change, sha256_text,
                        canonical_json)
from .evidence import artifact_hash, compute_requirement_statuses, run_check
from .recovery import next_action, open_issues, sync_issues
from .state_machine import GState, RStatus, TransitionError, check_transition, decide_global_state
from .storage import Store, new_id, now_iso

RVD_DIR = ".rvd"


class NotInitialized(RuntimeError):
    pass


class PermissionDenied(RuntimeError):
    pass


def find_project_root(start: Path | None = None) -> Path | None:
    """Locate the project by its explicit marker (.rvd/goal.yaml), never by bare cwd."""
    for var in ("RVD_PROJECT_ROOT", "CLAUDE_PROJECT_DIR"):
        v = os.environ.get(var)
        if v and (Path(v) / RVD_DIR / "goal.yaml").is_file():
            return Path(v).resolve()
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / RVD_DIR / "goal.yaml").is_file():
            return d
    return None


def state_dir_for(root: Path) -> Path:
    override = os.environ.get("RVD_STATE_DIR")
    if override:
        return Path(override) / hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return root / RVD_DIR


GITIGNORE = "state.db\nstate.db-*\nevidence/\nreports/\n"


class Controller:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.rvd = self.root / RVD_DIR
        self.state_dir = state_dir_for(self.root)
        self.store = Store(self.state_dir / "state.db")
        self._contract: GoalContract | None = None

    # ------------------------------------------------------------------ basics
    @classmethod
    def open(cls, start: Path | None = None) -> "Controller":
        root = find_project_root(start)
        if root is None:
            raise NotInitialized("no .rvd/goal.yaml found in this directory or its parents")
        return cls(root)

    def close(self) -> None:
        self.store.close()

    @property
    def initialized(self) -> bool:
        return self.store.q1("SELECT 1 FROM task_state WHERE id=1") is not None

    @property
    def contract(self) -> GoalContract:
        """The ACCEPTED contract (authority), not whatever is currently in goal.yaml."""
        if self._contract is None:
            row = self.store.q1("SELECT content_json FROM contract_versions ORDER BY version DESC LIMIT 1")
            if row is None:
                raise NotInitialized("goal contract not accepted yet; run `rvd init`")
            self._contract = contract_from_json(row["content_json"])
        return self._contract

    def global_state(self) -> GState:
        row = self.store.q1("SELECT state FROM task_state WHERE id=1")
        if row is None:
            return GState.NEW
        return GState(row["state"])

    def _transition(self, dst: GState, source: str, reason: str, actor: str) -> None:
        with self.store.tx():
            src = self.global_state()
            check_transition(src, dst, source)
            if src == dst:
                self.store.x("UPDATE task_state SET reason=?, updated_at=? WHERE id=1", (reason, now_iso()))
                return
            self.store.x("UPDATE task_state SET state=?, reason=?, updated_at=?, contract_version=? WHERE id=1",
                         (dst.value, reason, now_iso(), self.contract.version))
            self.store.event(actor, source, "transition", {"from": src.value, "to": dst.value, "reason": reason},
                             self.contract.version)

    def evidence_dir(self) -> Path:
        return self.state_dir / "evidence"

    # ------------------------------------------------------------------ init
    def init(self, accepted_by: str, confirm_hard: bool) -> dict:
        goal_path = self.rvd / "goal.yaml"
        c = load_contract_file(goal_path)
        hard = [con for con in c.constraints if con["kind"] == "HARD"] + [r for r in c.requirements if r.hard]
        if hard and not confirm_hard:
            raise PermissionDenied("contract has HARD constraints/requirements; a human must confirm them with "
                                   "`rvd init --confirm-hard --by <name>`")
        if hard and _is_agent_context() and not os.environ.get("RVD_ALLOW_AGENT_CONFIRM"):
            raise PermissionDenied("HARD constraints must be confirmed by a human, not from inside an agent session")
        (self.rvd / ".gitignore").write_text(GITIGNORE) if not (self.rvd / ".gitignore").exists() else None
        policy_path = self.rvd / "policy.yaml"
        if not policy_path.exists():
            policy_path.write_text(yaml.safe_dump({"budget": c.budget, "risk_policy": c.risk_policy},
                                                  sort_keys=True, allow_unicode=True))
        with self.store.tx():
            existing = self.store.q1("SELECT version, content_hash FROM contract_versions ORDER BY version DESC LIMIT 1")
            if existing:
                if existing["content_hash"] == c.content_hash():
                    return {"ok": True, "already_initialized": True, "goal_id": c.goal_id, "version": c.version}
                raise PermissionDenied("project already initialized with a different contract; use "
                                       "`rvd amend propose` to change it")
            self.store.x("INSERT INTO contract_versions(version,content_json,content_hash,accepted_at,accepted_by,source)"
                         " VALUES(?,?,?,?,?,?)", (c.version, c.to_json(), c.content_hash(), now_iso(), accepted_by, "init"))
            self.store.x("INSERT INTO task_state(id,goal_id,state,contract_version,updated_at,reason) VALUES(1,?,?,?,?,?)",
                         (c.goal_id, GState.NEW.value, c.version, now_iso(), "initialized"))
            for r in c.requirements:
                self.store.x("INSERT INTO requirement_status(req_id,status,updated_at) VALUES(?,?,?)",
                             (r.id, RStatus.UNVERIFIED.value, now_iso()))
            self.store.meta_set("goal_file_hash", sha256_text(goal_path.read_text(encoding="utf-8")))
            self.store.event(accepted_by, "cli", "contract_accepted", {"version": c.version, "hard": len(hard)}, c.version)
            self._contract = c
            self._transition(GState.READY, "controller", "contract accepted", accepted_by)
        return {"ok": True, "goal_id": c.goal_id, "version": c.version, "requirements": len(c.requirements)}

    # ------------------------------------------------------------------ contract drift (scenario I)
    def contract_drift(self) -> dict | None:
        """Detect edits to goal.yaml that did not go through an approved amendment.

        The accepted contract in the DB stays authoritative; the drift is audited once.
        """
        goal_path = self.rvd / "goal.yaml"
        try:
            text = goal_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"drift": "goal.yaml missing"}
        try:
            raw = yaml.safe_load(text)
            same = sha256_text(canonical_json(raw)) == self.contract.content_hash()
        except yaml.YAMLError:
            same = False
        if same:
            return None
        h = sha256_text(text)
        if self.store.meta_get("drift_logged_hash") != h:
            with self.store.tx():
                self.store.meta_set("drift_logged_hash", h)
                self.store.event("unknown", "controller", "contract_drift_ignored",
                                 {"file_hash": h, "note": "goal.yaml differs from accepted contract; ignored until an "
                                                          "amendment is approved"}, self.contract.version)
        return {"drift": "goal.yaml differs from the accepted contract and is being ignored",
                "accepted_version": self.contract.version}

    # ------------------------------------------------------------------ verify / refresh / decide
    def _progress_fp(self) -> dict[str, str]:
        rows = self.store.q("SELECT e.check_id, e.artifact_hash, e.passed, e.method, e.alternative_id FROM evidence e "
                            "JOIN (SELECT check_id, MAX(timestamp) ts FROM evidence GROUP BY check_id) m "
                            "ON e.check_id=m.check_id AND e.timestamp=m.ts")
        fp = {r["check_id"]: f"{r['artifact_hash']}:{r['passed']}:{r['method']}:{r['alternative_id']}" for r in rows}
        blocked = json.loads(self.store.meta_get("blocked_checks", "{}"))
        snap_fp = self.store.meta_get("env_fingerprint", "")
        for cid in blocked:
            fp.setdefault(cid, f"blocked:{snap_fp}:{json.dumps(blocked[cid], sort_keys=True)}")
        return fp

    def verify(self, actor: str = "rvd-cli", only_checks: list[str] | None = None,
               allow_risk: set[str] | None = None) -> dict:
        self._require_init()
        if self.global_state() == GState.CANCELLED:
            raise PermissionDenied("task is CANCELLED; `rvd resume --by <user>` first")
        owner = f"{actor}:{os.getpid()}"
        if not self.store.acquire_lease("verify", owner, ttl_sec=self.contract.budget["check_timeout_sec"] * 4):
            return {"ok": False, "error": "another verification is running (lease held)"}
        try:
            self.contract_drift()
            self._transition(GState.VERIFYING, "controller", "verification started", actor)
            snap = environment.capability_snapshot()
            env_fp = environment.fingerprint(snap)
            art = artifact_hash(self.root, self.contract.completion_policy.get("inputs") or None)
            run_id = new_id("run")
            ev_dir = self.evidence_dir() / run_id
            blocked: dict[str, list] = json.loads(self.store.meta_get("blocked_checks", "{}"))
            ran: set[str] = set()
            results = []
            for chk in self.contract.checks:
                if only_checks and chk.id not in only_checks:
                    continue
                with self.store.tx():
                    res = run_check(self.store, self.contract, chk, self.root, ev_dir, run_id, env_fp, art, allow_risk)
                if res.ran:
                    ran.add(chk.id)
                    blocked.pop(chk.id, None)
                else:
                    blocked[chk.id] = res.skipped
                results.append({"check": chk.id, "ran": res.ran, "passed": res.passed, "method": res.method,
                                "alternative": res.alternative_id, "evidence_id": res.evidence_id,
                                "skipped": res.skipped})
            with self.store.tx():
                self.store.meta_set("blocked_checks", json.dumps(blocked))
                self.store.meta_set("env_fingerprint", env_fp)
                self.store.meta_set("env_snapshot", json.dumps(snap))
                self.store.event(actor, "verifier", "verification_run",
                                 {"run_id": run_id, "artifact_hash": art, "env": env_fp, "results": results},
                                 self.contract.version)
            decision = self._recompute(actor, ran_checks=ran, art=art)
            return {"ok": True, "run_id": run_id, "artifact_hash": art, "results": results, **decision}
        except Exception as e:
            self._record_error(actor, f"verification error: {e!r}")
            raise
        finally:
            self.store.release_lease("verify", owner)

    def refresh(self, actor: str = "rvd") -> dict:
        """Cheap recomputation (no checks run): used by hooks. Detects stale evidence."""
        self._require_init()
        self.contract_drift()
        return self._recompute(actor, ran_checks=set(), art=artifact_hash(
            self.root, self.contract.completion_policy.get("inputs") or None))

    def _recompute(self, actor: str, ran_checks: set[str], art: str) -> dict:
        c = self.contract
        blocked = json.loads(self.store.meta_get("blocked_checks", "{}"))
        statuses = compute_requirement_statuses(self.store, c, art, blocked)
        with self.store.tx():
            for rid, (st, detail) in statuses.items():
                prev = self.store.q1("SELECT status FROM requirement_status WHERE req_id=?", (rid,))
                if prev is None or prev["status"] != st.value:
                    self.store.event(actor, "controller", "requirement_status",
                                     {"req": rid, "from": prev["status"] if prev else None, "to": st.value}, c.version)
                self.store.x("INSERT OR REPLACE INTO requirement_status(req_id,status,updated_at,detail_json) "
                             "VALUES(?,?,?,?)", (rid, st.value, now_iso(), json.dumps(detail)))
            sync_issues(self.store, c, statuses, self._progress_fp(), ran_checks, actor)
            return self._decide(actor, statuses)

    def _decide(self, actor: str, statuses: dict) -> dict:
        state = self.global_state()
        if state == GState.CANCELLED:
            return {"state": state.value, "reason": "cancelled"}
        mandatory = {r.id for r in self.contract.requirements if r.mandatory}
        issues = open_issues(self.store)
        actionable = sum(1 for i in issues if i["status"] == "open")
        blocked = sum(1 for i in issues if i["status"] == "blocked")
        exhausted = any(i["status"] == "exhausted" for i in issues) and actionable == 0
        dst, reason = decide_global_state({k: v[0] for k, v in statuses.items()}, mandatory,
                                          open_actionable=actionable, open_blocked=blocked, exhausted=exhausted)
        if dst == GState.SATISFIED and state != GState.VERIFYING:
            # SATISFIED is only granted at the end of a verification pass.
            if state == GState.SATISFIED:
                return {"state": state.value, "reason": "still satisfied"}
            dst, reason = GState.RECOVERING, "evidence looks complete; run `rvd verify` to confirm"
        if state == GState.SATISFIED and dst != GState.SATISFIED:
            reason = "re-opened: " + reason
        source = "controller.decide" if dst == GState.SATISFIED else "controller"
        try:
            self._transition(dst, source, reason, actor)
        except TransitionError:
            # e.g. READY -> SATISFIED is not a legal edge: go through VERIFYING-less path conservatively
            self._transition(GState.RECOVERING, "controller", reason, actor)
            dst = GState.RECOVERING
        return {"state": dst.value, "reason": reason}

    def _record_error(self, actor: str, msg: str) -> None:
        try:
            with self.store.tx():
                self.store.event(actor, "controller", "error", {"message": msg})
                src = self.global_state()
                if src not in (GState.CANCELLED, GState.ERROR):
                    self.store.x("UPDATE task_state SET state=?, reason=?, updated_at=? WHERE id=1",
                                 (GState.ERROR.value, msg[:500], now_iso()))
        except Exception:
            pass

    # ------------------------------------------------------------------ queries
    def requirement_rows(self) -> list[dict]:
        rows = []
        mand = {r.id: r for r in self.contract.requirements}
        for r in self.store.q("SELECT * FROM requirement_status ORDER BY req_id"):
            if r["req_id"] not in mand:
                continue
            req = mand[r["req_id"]]
            rows.append({"id": r["req_id"], "status": r["status"], "mandatory": req.mandatory,
                         "description": req.description, "accepts": req.methods,
                         "detail": json.loads(r["detail_json"])})
        return rows

    def status(self) -> dict:
        self._require_init()
        row = self.store.q1("SELECT * FROM task_state WHERE id=1")
        attempts = [dict(a) for a in self.store.q("SELECT kind, ts, payload_json FROM events WHERE kind IN "
                                                  "('verification_run','issue_opened','issue_exhausted','issue_resolved',"
                                                  "'stop_blocked','task_completion_blocked','amendment_proposed',"
                                                  "'amendment_approved','amendment_rejected','contract_drift_ignored') "
                                                  "ORDER BY id DESC LIMIT 15")]
        return {
            "project": str(self.root), "goal_id": row["goal_id"], "intent": self.contract.intent,
            "state": row["state"], "reason": row["reason"], "contract_version": self.contract.version,
            "contract_drift": self.contract_drift(),
            "requirements": self.requirement_rows(),
            "open_issues": open_issues(self.store),
            "next": next_action(self.store, row["state"]),
            "recent_events": attempts,
        }

    def next(self) -> dict:
        self._require_init()
        return next_action(self.store, self.global_state().value)

    def unmet_mandatory(self) -> list[str]:
        return [r["id"] for r in self.requirement_rows() if r["mandatory"] and r["status"] != RStatus.VERIFIED.value]

    def progress_fingerprint(self) -> str:
        """Fingerprint of everything that would count as new information."""
        art = artifact_hash(self.root, self.contract.completion_policy.get("inputs") or None)
        ev = self.store.q1("SELECT COUNT(*) n, MAX(timestamp) t FROM evidence")
        st = self.store.q("SELECT req_id, status FROM requirement_status ORDER BY req_id")
        return sha256_text(f"{art}|{ev['n']}|{ev['t']}|" + ",".join(f"{r['req_id']}={r['status']}" for r in st))[:16]

    # ------------------------------------------------------------------ user controls
    def cancel(self, by: str, reason: str = "") -> dict:
        self._require_init()
        self._transition(GState.CANCELLED, "user", reason or f"cancelled by {by}", by)
        return {"state": "CANCELLED"}

    def resume(self, by: str) -> dict:
        self._require_init()
        if _is_agent_context() and not os.environ.get("RVD_ALLOW_AGENT_CONFIRM"):
            raise PermissionDenied("only a human may resume a cancelled/paused task")
        st = self.global_state()
        if st == GState.CANCELLED:
            self._transition(GState.READY, "user", f"resumed by {by}", by)
        elif st in (GState.PAUSED_INCOMPLETE, GState.ERROR, GState.BLOCKED):
            self._transition(GState.READY, "user", f"resumed by {by}", by)
        with self.store.tx():
            self.store.x("UPDATE issues SET status='open', no_progress=0, attempts=0 WHERE status='exhausted'")
        return {"state": self.global_state().value}

    def reset_issue(self, issue_id: str, actor: str, note: str) -> dict:
        """Mark that a genuinely new strategy is being tried for an exhausted issue."""
        with self.store.tx():
            row = self.store.q1("SELECT * FROM issues WHERE issue_id=?", (issue_id,))
            if row is None:
                raise KeyError(issue_id)
            tried = json.loads(row["tried_json"]) + [{"ts": now_iso(), "by": actor, "strategy": note}]
            if any(t["strategy"] == note for t in json.loads(row["tried_json"])):
                raise PermissionDenied("this strategy was already tried for this issue; propose a different one")
            self.store.x("UPDATE issues SET status=CASE WHEN auto_allowed=1 THEN 'open' ELSE 'blocked' END, "
                         "no_progress=0, tried_json=?, updated_at=? WHERE issue_id=?",
                         (json.dumps(tried, ensure_ascii=False), now_iso(), issue_id))
            self.store.event(actor, "recovery", "strategy_switch", {"issue_id": issue_id, "strategy": note},
                             self.contract.version)
        return self.refresh(actor)

    # ------------------------------------------------------------------ amendments
    def amend_propose(self, new_path: Path, reason: str, proposer: str) -> dict:
        self._require_init()
        new = load_contract_file(Path(new_path))
        old = self.contract
        if new.goal_id != old.goal_id:
            raise ContractError("goal_id cannot change through an amendment")
        if new.version != old.version + 1:
            raise ContractError(f"amended contract must have version {old.version + 1}")
        d = diff_contracts(old, new)
        if not d["changes"]:
            raise ContractError("no changes")
        aid = new_id("amd")
        status = "proposed"
        notes = ""
        if d["verification_only"] and not d["needs_human"]:
            ok, problems = review_verification_change(old, new)
            if ok:
                status, notes = "auto_approved", "verification-only change; coverage review passed"
            else:
                status, notes = "rejected", "coverage review failed: " + "; ".join(problems)
        with self.store.tx():
            self.store.x("INSERT INTO amendments(amendment_id,base_version,proposed_json,diff_json,reason,proposer,status,"
                         "needs_human,reviewed_by,reviewed_at,review_notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (aid, old.version, new.to_json(), json.dumps(d), reason, proposer, status,
                          int(d["needs_human"]), "rvd.coverage_review" if status != "proposed" else None,
                          now_iso() if status != "proposed" else None, notes, now_iso()))
            self.store.event(proposer, "amendment", "amendment_proposed",
                             {"amendment_id": aid, "diff": d, "status": status}, old.version)
            if status == "auto_approved":
                self._apply_amendment(aid, new, "rvd.coverage_review")
        return {"amendment_id": aid, "status": status, "needs_human": d["needs_human"], "changes": d["changes"],
                "notes": notes}

    def amend_approve(self, aid: str, by: str, role: str) -> dict:
        self._require_init()
        if _is_agent_context() and not os.environ.get("RVD_ALLOW_AGENT_CONFIRM"):
            raise PermissionDenied("amendments must be approved by a human outside the agent session")
        with self.store.tx():
            row = self.store.q1("SELECT * FROM amendments WHERE amendment_id=?", (aid,))
            if row is None:
                raise KeyError(aid)
            if row["status"] != "proposed":
                raise PermissionDenied(f"amendment is {row['status']}")
            if row["proposer"] == by:
                raise PermissionDenied("proposer cannot approve their own amendment")
            owner = self.contract.raw.get("owner")
            if row["needs_human"] and role != "owner" and by != owner:
                raise PermissionDenied(f"this amendment touches HARD/acceptance terms; only the contract owner "
                                       f"({owner}) may approve")
            if row["base_version"] != self.contract.version:
                raise PermissionDenied("amendment is based on an outdated contract version")
            new = contract_from_json(row["proposed_json"])
            self.store.x("UPDATE amendments SET status='approved', reviewed_by=?, reviewed_at=? WHERE amendment_id=?",
                         (by, now_iso(), aid))
            self.store.event(by, "amendment", "amendment_approved", {"amendment_id": aid, "role": role},
                             self.contract.version)
            self._apply_amendment(aid, new, by)
        return {"amendment_id": aid, "status": "approved", "version": new.version}

    def _apply_amendment(self, aid: str, new: GoalContract, by: str) -> None:
        self.store.x("INSERT INTO contract_versions(version,content_json,content_hash,accepted_at,accepted_by,source) "
                     "VALUES(?,?,?,?,?,?)", (new.version, new.to_json(), new.content_hash(), now_iso(), by, aid))
        for r in new.requirements:
            self.store.x("INSERT OR IGNORE INTO requirement_status(req_id,status,updated_at) VALUES(?,?,?)",
                         (r.id, RStatus.UNVERIFIED.value, now_iso()))
        self.store.x("DELETE FROM requirement_status WHERE req_id NOT IN (%s)" %
                     ",".join("?" * len(new.requirements)), tuple(r.id for r in new.requirements))
        self._contract = new
        # Write the accepted contract back so the file matches the authority.
        (self.rvd / "goal.yaml").write_text(yaml.safe_dump(new.raw, sort_keys=False, allow_unicode=True),
                                            encoding="utf-8")
        st = self.global_state()
        if st == GState.SATISFIED:
            self.store.x("UPDATE task_state SET state=?, reason=?, updated_at=?, contract_version=? WHERE id=1",
                         (GState.RECOVERING.value, "contract amended; re-verification required", now_iso(), new.version))
        else:
            self.store.x("UPDATE task_state SET contract_version=? WHERE id=1", (new.version,))

    def list_amendments(self) -> list[dict]:
        return [dict(r) | {"diff": json.loads(r["diff_json"])} for r in
                self.store.q("SELECT amendment_id, base_version, reason, proposer, status, needs_human, reviewed_by, "
                             "review_notes, created_at, diff_json FROM amendments ORDER BY created_at")]

    # ------------------------------------------------------------------ claims & sessions
    def record_claim(self, source: str, actor: str, text: str, session_key: str | None = None) -> None:
        """Worker/agent natural-language claims are stored as untrusted input only."""
        with self.store.tx():
            self.store.event(actor, source, "agent_claim_untrusted",
                             {"text": (text or "")[:4000], "session": session_key}, self._safe_version())

    def _safe_version(self) -> int | None:
        try:
            return self.contract.version
        except NotInitialized:
            return None

    def register_session(self, adapter: str, external_id: str, resumable: bool) -> str:
        key = f"{adapter}:{external_id}"
        with self.store.tx():
            if self.store.q1("SELECT 1 FROM sessions WHERE session_key=?", (key,)):
                self.store.x("UPDATE sessions SET last_seen=?, status='active' WHERE session_key=?", (now_iso(), key))
            else:
                self.store.x("INSERT INTO sessions(session_key,adapter,external_id,started_at,last_seen,status,resumable)"
                             " VALUES(?,?,?,?,?,?,?)", (key, adapter, external_id, now_iso(), now_iso(), "active",
                                                         int(resumable)))
        return key

    def end_session(self, key: str, status: str) -> None:
        with self.store.tx():
            self.store.x("UPDATE sessions SET status=?, last_seen=? WHERE session_key=?", (status, now_iso(), key))

    def _require_init(self) -> None:
        if not self.initialized:
            raise NotInitialized("run `rvd init` first")

    # ------------------------------------------------------------------ report
    def report_markdown(self) -> str:
        s = self.status()
        icon = {"VERIFIED": "✅", "PARTIALLY_VERIFIED": "🟡", "FAILED": "❌", "BLOCKED": "⛔", "STALE": "⌛",
                "UNVERIFIED": "❔", "IN_PROGRESS": "…"}
        lines = [f"# RVD report — {s['goal_id']} (contract v{s['contract_version']})", "",
                 f"**Global state: `{s['state']}`** — {s['reason']}", "",
                 f"Intent: {s['intent']}", "",
                 "> This report is generated from the evidence store. Agent summaries are not used as evidence.", ""]
        if s["contract_drift"]:
            lines += [f"⚠️ {s['contract_drift']['drift']}", ""]
        lines += ["## Requirements", "", "| ID | Mandatory | Status | Accepts | Evidence / gaps |", "|---|---|---|---|---|"]
        for r in s["requirements"]:
            ev = []
            for cid, c in r["detail"].get("checks", {}).items():
                if "evidence_id" in c:
                    ev.append(f"{cid}: {c['method']} {'pass' if c['passed'] else 'FAIL'}"
                              f"{'' if c['fresh'] else ' (stale)'} `{c['evidence_id']}`")
                else:
                    ev.append(f"{cid}: {c.get('state')}")
            gaps = "; ".join(r["detail"].get("gaps", []))
            lines.append(f"| {r['id']} | {'yes' if r['mandatory'] else 'no'} | {icon.get(r['status'], '')} "
                         f"{r['status']} | {', '.join(r['accepts'])} | {'<br>'.join(ev)}{'<br>**gap:** ' + gaps if gaps else ''} |")
        lines += ["", "## Open issues", ""]
        if not s["open_issues"]:
            lines.append("None.")
        for i in s["open_issues"]:
            lines += [f"- **{i['issue_id']}** [{i['status']}/{i['kind']}] {i['title']} — reqs {i['requirement_ids']}, "
                      f"attempts {i['attempts']}, no-progress {i['no_progress']}",
                      f"  - hypothesis: {i['root_cause_hypothesis']}",
                      f"  - next: {i['suggested_action']}"]
            if i["blocking_resource"]:
                lines.append(f"  - blocked on: {i['blocking_resource']}")
        lines += ["", "## Strategy proposals considered (latest per alternative)", ""]
        for p in self.store.q("SELECT check_id, alternative_id, method, feasible, reason, coverage_json, "
                              "remaining_gaps_json FROM strategy_proposals p WHERE ts = (SELECT MAX(ts) FROM "
                              "strategy_proposals q WHERE q.check_id=p.check_id AND q.alternative_id=p.alternative_id) "
                              "ORDER BY check_id"):
            cov = json.loads(p["coverage_json"])
            lines.append(f"- `{p['check_id']}/{p['alternative_id']}` ({p['method']}): "
                         f"{'feasible' if p['feasible'] else 'not feasible — ' + p['reason']}; "
                         f"full {cov['full'] or '-'}, partial {cov['partial'] or '-'}")
        from .supervisor import supervisor_report_markdown  # local import: supervisor depends on controller
        sup = supervisor_report_markdown(self)
        if sup:
            lines += ["", sup]
        lines += ["", "## Next action", "", "```json", json.dumps(s["next"], indent=2, ensure_ascii=False), "```", "",
                  "## Recent events", ""]
        for e in s["recent_events"]:
            lines.append(f"- {e['ts']} `{e['kind']}`")
        return "\n".join(lines) + "\n"


def _is_agent_context() -> bool:
    # Claude Code sets CLAUDECODE=1 in tool subprocesses; other adapters can set RVD_AGENT_CONTEXT=1.
    return os.environ.get("CLAUDECODE") == "1" or os.environ.get("RVD_AGENT_CONTEXT") == "1"
