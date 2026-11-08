"""Goal Contract loading, schema validation and canonical hashing.

The contract is platform-agnostic: nothing here knows about Claude Code.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

DEFAULT_BUDGET = {
    "max_attempts_per_issue": 6,
    "max_consecutive_no_progress": 2,
    "max_stop_blocks_per_session": 6,  # below Claude Code's default Stop block cap (8)
    "time_budget_sec": 4 * 3600,
    "check_timeout_sec": 300,
    "parallelism": 1,
    "cooldown_sec": 0,
}
DEFAULT_RISK = {
    "network": "ask",
    "remote_execution": "ask",
    "paid_resources": "deny",
    "production": "deny",
    "delete_files": "ask",
    "download_images": "ask",
}
DEFAULT_COMPLETION = {"require_all_mandatory": True, "unreferenced_tasks": "allow", "inputs": []}


class ContractError(ValueError):
    pass


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class Requirement:
    id: str
    description: str
    mandatory: bool
    methods: list[str]
    partial_methods: list[str]
    predicate: str = ""
    hard: bool = False


@dataclass
class Alternative:
    id: str
    method: str
    command: str
    requires: dict = field(default_factory=dict)
    limitations: str = ""
    risk: str = "low"


@dataclass
class Check:
    id: str
    proves: list[str]
    alternatives: list[Alternative]
    description: str = ""
    timeout_sec: int | None = None


@dataclass
class GoalContract:
    raw: dict

    # --- derived views -------------------------------------------------
    @property
    def goal_id(self) -> str:
        return self.raw["goal_id"]

    @property
    def version(self) -> int:
        return int(self.raw["version"])

    @property
    def intent(self) -> str:
        return self.raw["intent"]

    @property
    def budget(self) -> dict:
        return {**DEFAULT_BUDGET, **self.raw.get("budget", {})}

    @property
    def risk_policy(self) -> dict:
        return {**DEFAULT_RISK, **self.raw.get("risk_policy", {})}

    @property
    def completion_policy(self) -> dict:
        return {**DEFAULT_COMPLETION, **self.raw.get("completion_policy", {})}

    @property
    def requirements(self) -> list[Requirement]:
        out = []
        for r in self.raw["requirements"]:
            out.append(Requirement(
                id=r["id"], description=r["description"], mandatory=r["mandatory"],
                methods=list(r["accept"]["methods"]),
                partial_methods=list(r["accept"].get("partial_methods", [])),
                predicate=r.get("predicate", ""), hard=bool(r.get("hard", False)),
            ))
        return out

    def requirement(self, rid: str) -> Requirement:
        for r in self.requirements:
            if r.id == rid:
                return r
        raise KeyError(rid)

    @property
    def checks(self) -> list[Check]:
        out = []
        for c in self.raw.get("checks", []):
            alts = [Alternative(id=a["id"], method=a["method"], command=a["command"],
                                requires=a.get("requires", {}), limitations=a.get("limitations", ""),
                                risk=a.get("risk", "low")) for a in c["alternatives"]]
            out.append(Check(id=c["id"], proves=list(c["proves"]), alternatives=alts,
                             description=c.get("description", ""), timeout_sec=c.get("timeout_sec")))
        return out

    @property
    def constraints(self) -> list[dict]:
        return list(self.raw.get("constraints", []))

    def content_hash(self) -> str:
        return sha256_text(canonical_json(self.raw))

    def to_json(self) -> str:
        return canonical_json(self.raw)


def validate_raw(raw: Any) -> list[str]:
    """Return a list of human-readable validation errors (empty if valid)."""
    if not isinstance(raw, dict):
        return ["contract must be a mapping"]
    v = Draft202012Validator(_schema("goal.schema.json"))
    errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in v.iter_errors(raw)]
    if errors:
        return errors
    # semantic checks the JSON schema can't express
    req_ids = [r["id"] for r in raw["requirements"]]
    if len(set(req_ids)) != len(req_ids):
        errors.append("requirements: duplicate ids")
    check_ids = [c["id"] for c in raw.get("checks", [])]
    if len(set(check_ids)) != len(check_ids):
        errors.append("checks: duplicate ids")
    for c in raw.get("checks", []):
        for rid in c["proves"]:
            if rid not in req_ids:
                errors.append(f"checks/{c['id']}: proves unknown requirement {rid}")
        alt_ids = [a["id"] for a in c["alternatives"]]
        if len(set(alt_ids)) != len(alt_ids):
            errors.append(f"checks/{c['id']}: duplicate alternative ids")
    for con in raw.get("constraints", []):
        for rid in con.get("protects", []):
            if rid not in req_ids:
                errors.append(f"constraints/{con['id']}: protects unknown requirement {rid}")
    covered = {rid for c in raw.get("checks", []) for rid in c["proves"]}
    for r in raw["requirements"]:
        if r["mandatory"] and r["id"] not in covered:
            # Not an error: the requirement simply stays UNVERIFIED until a check is added
            # via amendment. Surface as a warning-level message the caller may print.
            pass
    return errors


def load_contract_file(path: Path) -> GoalContract:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ContractError(f"goal contract not found: {path}") from e
    except yaml.YAMLError as e:
        raise ContractError(f"goal contract is not valid YAML: {e}") from e
    errs = validate_raw(raw)
    if errs:
        raise ContractError("goal contract invalid:\n  - " + "\n  - ".join(errs))
    return GoalContract(raw=raw)


def contract_from_json(s: str) -> GoalContract:
    return GoalContract(raw=json.loads(s))


def hard_protected_requirements(c: GoalContract) -> set[str]:
    ids = {r.id for r in c.requirements if r.hard}
    for con in c.constraints:
        if con["kind"] == "HARD":
            ids.update(con.get("protects", []))
    return ids


def diff_contracts(old: GoalContract, new: GoalContract) -> dict:
    """Classify the changes between two contracts.

    Returns {"changes": [...], "needs_human": bool, "verification_only": bool}.
    A change "needs_human" if it touches: a HARD constraint, a hard/protected
    requirement, mandatory flags, acceptance methods/predicate of a mandatory
    requirement, removal of a requirement, or the intent / risk policy.
    Changes that only touch `checks` (how to obtain evidence) are verification-only;
    they are still reviewed for coverage by `review_verification_change`.
    """
    changes: list[dict] = []
    needs_human = False
    o, n = old.raw, new.raw
    protected = hard_protected_requirements(old)

    if o.get("intent") != n.get("intent"):
        changes.append({"field": "intent", "kind": "goal"}); needs_human = True
    if o.get("risk_policy") != n.get("risk_policy"):
        changes.append({"field": "risk_policy", "kind": "policy"}); needs_human = True
    if o.get("budget") != n.get("budget") or o.get("completion_policy") != n.get("completion_policy"):
        changes.append({"field": "budget/completion_policy", "kind": "policy"}); needs_human = True

    oreq = {r["id"]: r for r in o["requirements"]}
    nreq = {r["id"]: r for r in n["requirements"]}
    for rid in oreq.keys() - nreq.keys():
        changes.append({"field": f"requirements/{rid}", "kind": "removed"}); needs_human = True
    for rid in nreq.keys() - oreq.keys():
        changes.append({"field": f"requirements/{rid}", "kind": "added"}); needs_human = True
    for rid in oreq.keys() & nreq.keys():
        a, b = oreq[rid], nreq[rid]
        if a != b:
            sensitive = (a.get("accept") != b.get("accept") or a.get("predicate") != b.get("predicate")
                         or a.get("mandatory") != b.get("mandatory") or a.get("hard") != b.get("hard"))
            changes.append({"field": f"requirements/{rid}", "kind": "acceptance" if sensitive else "text",
                            "protected": rid in protected})
            if sensitive or rid in protected:
                needs_human = True

    ocon = {c["id"]: c for c in o.get("constraints", [])}
    ncon = {c["id"]: c for c in n.get("constraints", [])}
    for cid in ocon.keys() | ncon.keys():
        a, b = ocon.get(cid), ncon.get(cid)
        if a != b:
            hard_touched = (a or {}).get("kind") == "HARD" or (b or {}).get("kind") == "HARD"
            changes.append({"field": f"constraints/{cid}", "kind": "constraint", "hard": hard_touched})
            if hard_touched or (a and b and a.get("kind") != b.get("kind")):
                needs_human = True
            elif a is None or b is None:
                needs_human = True

    if o.get("checks", []) != n.get("checks", []):
        changes.append({"field": "checks", "kind": "verification"})
    if o.get("environment_assumptions", []) != n.get("environment_assumptions", []):
        changes.append({"field": "environment_assumptions", "kind": "assumption"})

    verification_only = bool(changes) and all(c["kind"] in ("verification", "assumption") for c in changes)
    return {"changes": changes, "needs_human": needs_human, "verification_only": verification_only}


def review_verification_change(old: GoalContract, new: GoalContract) -> tuple[bool, list[str]]:
    """Independent coverage review for a verification-only change.

    The new set of checks must still be *able* to fully verify every requirement
    that the old checks could fully verify (i.e. a check proving it with an
    alternative whose method is in the requirement's accepted methods). This blocks
    "replace the requirement's test with an easier one that can't actually prove it".
    """
    problems: list[str] = []

    def full_coverage(c: GoalContract) -> set[str]:
        cov = set()
        reqs = {r.id: r for r in c.requirements}
        for ch in c.checks:
            for rid in ch.proves:
                if any(a.method in reqs[rid].methods for a in ch.alternatives):
                    cov.add(rid)
        return cov

    lost = full_coverage(old) - full_coverage(new)
    for rid in sorted(lost):
        problems.append(f"{rid}: new checks can no longer fully verify this requirement with an accepted method")
    return (not problems), problems


def deep_copy(c: GoalContract) -> GoalContract:
    return GoalContract(raw=copy.deepcopy(c.raw))
