"""`rvd` command-line interface: the deterministic surface used by hooks, skills and humans.

Exit codes (stable):
  0  success (for `verify`: goal SATISFIED)
  1  internal error
  2  usage error
  3  project not initialized / not found
  4  permission denied (human authorization required)
  5  invalid goal contract
  10 command succeeded but goal is NOT satisfied (verify) / doctor found problems
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__, environment
from .contracts import ContractError, load_contract_file
from .controller import Controller, NotInitialized, PermissionDenied, find_project_root, state_dir_for

EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_NOINIT, EXIT_PERM, EXIT_CONTRACT, EXIT_UNSATISFIED = 0, 1, 2, 3, 4, 5, 10
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _emit(args, data, text: str | None = None) -> None:
    if args.json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        sys.stdout.write((text if text is not None else json.dumps(data, ensure_ascii=False, indent=2, default=str)) + "\n")


def _ctrl(args) -> Controller:
    start = Path(args.project) if args.project else None
    if start is not None and not (start / ".rvd" / "goal.yaml").is_file():
        raise NotInitialized(f"{start}/.rvd/goal.yaml not found")
    return Controller(start.resolve()) if start else Controller.open()


def _status_text(s: dict) -> str:
    out = [f"goal {s['goal_id']} v{s['contract_version']}: {s['state']} — {s['reason']}"]
    if s.get("contract_drift"):
        out.append("WARNING: " + s["contract_drift"]["drift"])
    for r in s["requirements"]:
        gaps = "; ".join(r["detail"].get("gaps", []))
        out.append(f"  {r['id']:<10} {r['status']:<19} {'M' if r['mandatory'] else ' '} {r['description'][:60]}"
                   + (f"  [{gaps}]" if gaps else ""))
    if s["open_issues"]:
        out.append("open issues:")
        for i in s["open_issues"]:
            out.append(f"  {i['issue_id']} [{i['status']}/{i['kind']}] {i['title']} (attempts {i['attempts']}, "
                       f"no-progress {i['no_progress']})")
    out.append("next: " + json.dumps(s["next"], ensure_ascii=False))
    return "\n".join(out)


def cmd_init(args) -> int:
    root = Path(args.project or ".").resolve()
    goal = root / ".rvd" / "goal.yaml"
    if args.from_file:
        goal.parent.mkdir(parents=True, exist_ok=True)
        if goal.exists() and not args.force:
            raise PermissionDenied(f"{goal} exists; use --force to overwrite before acceptance")
        goal.write_text(Path(args.from_file).read_text(encoding="utf-8"), encoding="utf-8")
    if not goal.exists():
        tmpl = PLUGIN_ROOT / "templates" / "goal.yaml"
        goal.parent.mkdir(parents=True, exist_ok=True)
        goal.write_text(tmpl.read_text(encoding="utf-8"), encoding="utf-8")
        _emit(args, {"ok": False, "created_template": str(goal)},
              f"Created template {goal}. Fill in requirements/checks, then run `rvd init` again.")
        return EXIT_USAGE
    ctrl = Controller(root)
    try:
        res = ctrl.init(accepted_by=args.by or getpass.getuser(), confirm_hard=args.confirm_hard)
        _emit(args, res, f"accepted goal {res['goal_id']} v{res['version']}" +
              (" (already initialized)" if res.get("already_initialized") else ""))
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_status(args) -> int:
    ctrl = _ctrl(args)
    try:
        if not args.no_refresh:
            ctrl.refresh("rvd-cli")
        s = ctrl.status()
        _emit(args, s, _status_text(s))
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_verify(args) -> int:
    ctrl = _ctrl(args)
    try:
        allow = {"low", "medium", "high"} if args.allow_high_risk else None
        res = ctrl.verify(actor=args.actor, only_checks=args.check or None, allow_risk=allow)
        if not res.get("ok"):
            _emit(args, res, res.get("error"))
            return EXIT_ERR
        lines = [f"run {res['run_id']} (artifact {res['artifact_hash']})"]
        for r in res["results"]:
            if r["ran"]:
                lines.append(f"  {r['check']:<20} {'PASS' if r['passed'] else 'FAIL'} via {r['method']} ({r['alternative']})")
            else:
                lines.append(f"  {r['check']:<20} NOT RUN: " + "; ".join(f"{s['alternative']}: {s['reason']}"
                                                                         for s in r["skipped"]))
        lines.append(f"=> {res['state']}: {res['reason']}")
        _emit(args, res, "\n".join(lines))
        return EXIT_OK if res["state"] == "SATISFIED" else EXIT_UNSATISFIED
    finally:
        ctrl.close()


def cmd_next(args) -> int:
    ctrl = _ctrl(args)
    try:
        ctrl.refresh("rvd-cli")
        n = ctrl.next()
        sys.stdout.write(json.dumps(n, ensure_ascii=False, indent=None if args.json else 2) + "\n")
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_report(args) -> int:
    ctrl = _ctrl(args)
    try:
        ctrl.refresh("rvd-cli")
        md = ctrl.report_markdown()
        out = Path(args.out) if args.out else ctrl.state_dir / "reports" / "latest.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        _emit(args, {"report": str(out), "state": ctrl.global_state().value}, md if not args.quiet else str(out))
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_issue(args) -> int:
    ctrl = _ctrl(args)
    try:
        ctrl.refresh("rvd-cli")
        issues = ctrl.status()["open_issues"]
        _emit(args, issues, "\n".join(f"{i['issue_id']} [{i['status']}/{i['kind']}] {i['title']} reqs={i['requirement_ids']}\n"
                                      f"    next: {i['suggested_action']}" for i in issues) or "no open issues")
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_recover(args) -> int:
    ctrl = _ctrl(args)
    try:
        if args.reset:
            if not args.strategy:
                raise PermissionDenied("--reset requires --strategy '<what is genuinely different this time>'")
            res = ctrl.reset_issue(args.issue_id, args.actor, args.strategy)
            _emit(args, res)
            return EXIT_OK
        ctrl.refresh("rvd-cli")
        issue = next((i for i in ctrl.status()["open_issues"] if i["issue_id"] == args.issue_id), None)
        if issue is None:
            _emit(args, {"error": "no such open issue"}, "no such open issue")
            return EXIT_USAGE
        _emit(args, issue, f"{issue['title']}\nhypothesis: {issue['root_cause_hypothesis']}\n"
                           f"action: {issue['suggested_action']}\nauto-allowed: {bool(issue['auto_allowed'])}")
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_amend(args) -> int:
    ctrl = _ctrl(args)
    try:
        if args.amend_cmd == "propose":
            res = ctrl.amend_propose(Path(args.file), args.reason, args.by or getpass.getuser())
        elif args.amend_cmd == "approve":
            res = ctrl.amend_approve(args.amendment_id, args.by or getpass.getuser(), args.role)
        else:
            res = ctrl.list_amendments()
        _emit(args, res)
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_cancel(args) -> int:
    ctrl = _ctrl(args)
    try:
        _emit(args, ctrl.cancel(args.by or getpass.getuser(), args.reason or ""))
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_resume(args) -> int:
    ctrl = _ctrl(args)
    try:
        _emit(args, ctrl.resume(args.by or getpass.getuser()))
    finally:
        ctrl.close()
    return EXIT_OK


def cmd_env(args) -> int:
    snap = environment.capability_snapshot()
    _emit(args, {"fingerprint": environment.fingerprint(snap), **snap})
    return EXIT_OK


def _plugin_enabled_somewhere(root: Path | None) -> tuple[bool | None, list[str]]:
    """Best-effort look at Claude Code settings for this plugin / disableAllHooks."""
    notes = []
    found: bool | None = None
    candidates = [Path.home() / ".claude" / "settings.json"]
    if root:
        candidates += [root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]
    for p in candidates:
        try:
            s = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if s.get("disableAllHooks"):
            notes.append(f"{p}: disableAllHooks=true — hook-level gate is OFF")
            found = False
        ep = s.get("enabledPlugins") or {}
        for k, v in ep.items():
            if k.split("@")[0] == "rvd-harness":
                found = bool(v) if found is not False else False
                notes.append(f"{p}: enabledPlugins[{k}]={v}")
    return found, notes


def cmd_doctor(args) -> int:
    checks: list[dict] = []

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    for mod in ("yaml", "jsonschema"):
        try:
            __import__(mod)
            add(f"import {mod}", True)
        except ImportError as e:
            add(f"import {mod}", False, str(e))
    hooks_json = PLUGIN_ROOT / "hooks" / "hooks.json"
    try:
        hj = json.loads(hooks_json.read_text())
        events = sorted(hj["hooks"].keys())
        add("plugin hooks.json", all(e in events for e in ("SessionStart", "Stop", "TaskCompleted")), ", ".join(events))
    except Exception as e:
        add("plugin hooks.json", False, repr(e))
    root = Path(args.project).resolve() if args.project else find_project_root()
    enabled, notes = _plugin_enabled_somewhere(root)
    if enabled is False:
        add("claude-code plugin/hook enablement", False, "; ".join(notes))
    else:
        add("claude-code plugin/hook enablement", True,
            ("; ".join(notes) or "no settings entry found (plugin may be loaded with --plugin-dir)") +
            f"; CLAUDE_CODE_STOP_HOOK_BLOCK_CAP={os.environ.get('CLAUDE_CODE_STOP_HOOK_BLOCK_CAP', 'default(8)')}")
    if root is None:
        add("project", False, "no .rvd/goal.yaml found")
    else:
        try:
            load_contract_file(root / ".rvd" / "goal.yaml")
            add("goal.yaml schema", True, str(root / ".rvd" / "goal.yaml"))
        except ContractError as e:
            add("goal.yaml schema", False, str(e))
        db = state_dir_for(root) / "state.db"
        if db.exists():
            try:
                con = sqlite3.connect(str(db))
                r = con.execute("PRAGMA integrity_check").fetchone()[0]
                add("state.db integrity", r == "ok", f"{db}: {r}")
                hb = con.execute("SELECT value FROM meta WHERE key='hook_heartbeat'").fetchone()
                sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                st = con.execute("SELECT state FROM task_state WHERE id=1").fetchone()
                con.close()
                add("initialized", st is not None, st[0] if st else "run `rvd init`")
                add("hook heartbeat", hb is not None,
                    f"last hook run {hb[0]}" if hb else
                    "no hook has ever run for this project — the Stop/TaskCompleted gate is NOT active "
                    "(load the plugin: `claude --plugin-dir <rvd-harness>` or install it)")
                if hb is None and sessions:
                    add("gate degradation", False, "sessions exist but no hook heartbeat")
            except sqlite3.DatabaseError as e:
                add("state.db integrity", False, repr(e))
        else:
            add("initialized", False, "no state.db — run `rvd init`")
    ok = all(c["ok"] for c in checks)
    _emit(args, {"ok": ok, "checks": checks},
          "\n".join(f"[{'ok' if c['ok'] else 'FAIL'}] {c['check']}: {c['detail']}" for c in checks))
    return EXIT_OK if ok else EXIT_UNSATISFIED


def cmd_hook(args) -> int:
    from .hooks import main_stdin
    return main_stdin()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rvd", description="RVD 2.0 goal/evidence harness")
    p.add_argument("--version", action="version", version=f"rvd {__version__}")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--project", help="project root (default: search upward for .rvd/goal.yaml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="accept the goal contract in .rvd/goal.yaml")
    s.add_argument("--from", dest="from_file", help="copy a contract file into .rvd/goal.yaml first")
    s.add_argument("--force", action="store_true")
    s.add_argument("--by", help="who accepts the contract")
    s.add_argument("--confirm-hard", action="store_true", help="human confirmation of HARD constraints")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("status"); s.add_argument("--no-refresh", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("verify"); s.add_argument("--check", action="append")
    s.add_argument("--actor", default="rvd-cli"); s.add_argument("--allow-high-risk", action="store_true")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("next"); s.set_defaults(fn=cmd_next)
    s = sub.add_parser("report"); s.add_argument("--out"); s.add_argument("--quiet", action="store_true")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("issue"); s.add_argument("issue_cmd", choices=["list"]); s.set_defaults(fn=cmd_issue)
    s = sub.add_parser("recover"); s.add_argument("issue_id"); s.add_argument("--reset", action="store_true")
    s.add_argument("--strategy"); s.add_argument("--actor", default="rvd-cli"); s.set_defaults(fn=cmd_recover)
    s = sub.add_parser("amend"); a = s.add_subparsers(dest="amend_cmd", required=True)
    ap = a.add_parser("propose"); ap.add_argument("file"); ap.add_argument("--reason", required=True)
    ap.add_argument("--by")
    aa = a.add_parser("approve"); aa.add_argument("amendment_id"); aa.add_argument("--by")
    aa.add_argument("--role", default="reviewer", choices=["owner", "reviewer"])
    a.add_parser("list")
    s.set_defaults(fn=cmd_amend)
    s = sub.add_parser("cancel"); s.add_argument("--by"); s.add_argument("--reason"); s.set_defaults(fn=cmd_cancel)
    s = sub.add_parser("resume"); s.add_argument("--by"); s.set_defaults(fn=cmd_resume)
    s = sub.add_parser("env", help="environment capability snapshot"); s.set_defaults(fn=cmd_env)
    s = sub.add_parser("doctor"); s.set_defaults(fn=cmd_doctor)
    s = sub.add_parser("hook", help="hook entry: reads Claude Code hook JSON on stdin"); s.set_defaults(fn=cmd_hook)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code not in (0, None) else EXIT_OK
    try:
        return args.fn(args)
    except NotInitialized as e:
        sys.stderr.write(f"rvd: {e}\n"); return EXIT_NOINIT
    except PermissionDenied as e:
        sys.stderr.write(f"rvd: permission denied: {e}\n"); return EXIT_PERM
    except ContractError as e:
        sys.stderr.write(f"rvd: {e}\n"); return EXIT_CONTRACT
    except KeyError as e:
        sys.stderr.write(f"rvd: not found: {e}\n"); return EXIT_USAGE
    except Exception as e:  # pragma: no cover
        sys.stderr.write(f"rvd: internal error: {e!r}\n"); return EXIT_ERR


if __name__ == "__main__":
    sys.exit(main())
