"""SQLite persistence with versioned migrations, atomic transactions and leases."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE contract_versions (
        version INTEGER PRIMARY KEY, content_json TEXT NOT NULL, content_hash TEXT NOT NULL,
        accepted_at TEXT NOT NULL, accepted_by TEXT NOT NULL, source TEXT NOT NULL);
    CREATE TABLE task_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), goal_id TEXT NOT NULL, state TEXT NOT NULL,
        contract_version INTEGER NOT NULL, updated_at TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '');
    CREATE TABLE requirement_status (
        req_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}');
    CREATE TABLE evidence (
        evidence_id TEXT PRIMARY KEY, check_id TEXT NOT NULL, alternative_id TEXT, requirement_ids TEXT NOT NULL,
        method TEXT NOT NULL, env_fingerprint TEXT NOT NULL, artifact_hash TEXT NOT NULL, command TEXT NOT NULL,
        exit_code INTEGER, timestamp TEXT NOT NULL, log_path TEXT NOT NULL, log_sha256 TEXT NOT NULL,
        passed INTEGER NOT NULL, claim_supported TEXT NOT NULL, limitations TEXT NOT NULL, verifier TEXT NOT NULL,
        contract_version INTEGER NOT NULL, run_id TEXT NOT NULL);
    CREATE INDEX evidence_check ON evidence(check_id, timestamp);
    CREATE TABLE issues (
        issue_id TEXT PRIMARY KEY, signature TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, status TEXT NOT NULL,
        title TEXT NOT NULL, root_cause_hypothesis TEXT NOT NULL DEFAULT '', blocking_resource TEXT NOT NULL DEFAULT '',
        requirement_ids TEXT NOT NULL, suggested_action TEXT NOT NULL DEFAULT '', auto_allowed INTEGER NOT NULL DEFAULT 1,
        attempts INTEGER NOT NULL DEFAULT 0, no_progress INTEGER NOT NULL DEFAULT 0, last_fingerprint TEXT NOT NULL DEFAULT '',
        tried_json TEXT NOT NULL DEFAULT '[]', next_condition TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY, issue_id TEXT, executor TEXT NOT NULL, session_key TEXT,
        started_at TEXT NOT NULL, ended_at TEXT, outcome TEXT, fingerprint TEXT, notes TEXT NOT NULL DEFAULT '');
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT NOT NULL, source TEXT NOT NULL,
        kind TEXT NOT NULL, payload_json TEXT NOT NULL, contract_version INTEGER);
    CREATE TABLE amendments (
        amendment_id TEXT PRIMARY KEY, base_version INTEGER NOT NULL, proposed_json TEXT NOT NULL, diff_json TEXT NOT NULL,
        reason TEXT NOT NULL, proposer TEXT NOT NULL, status TEXT NOT NULL, needs_human INTEGER NOT NULL,
        reviewed_by TEXT, reviewed_at TEXT, review_notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE sessions (
        session_key TEXT PRIMARY KEY, adapter TEXT NOT NULL, external_id TEXT NOT NULL, started_at TEXT NOT NULL,
        last_seen TEXT NOT NULL, status TEXT NOT NULL, resumable INTEGER NOT NULL DEFAULT 0,
        stop_blocks INTEGER NOT NULL DEFAULT 0, last_block_fingerprint TEXT NOT NULL DEFAULT '',
        consecutive_no_progress INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE hook_events (
        dedupe_key TEXT PRIMARY KEY, event TEXT NOT NULL, session_id TEXT, ts TEXT NOT NULL, decision_json TEXT NOT NULL);
    CREATE TABLE leases (name TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL);
    CREATE TABLE strategy_proposals (
        id TEXT PRIMARY KEY, check_id TEXT NOT NULL, alternative_id TEXT NOT NULL, method TEXT NOT NULL,
        coverage_json TEXT NOT NULL, remaining_gaps_json TEXT NOT NULL, feasible INTEGER NOT NULL,
        reason TEXT NOT NULL, ts TEXT NOT NULL);
    """,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class StorageError(RuntimeError):
    pass


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # --- infrastructure ----------------------------------------------------
    def _migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row["value"]) if row else 0
        if current > SCHEMA_VERSION:
            raise StorageError(f"state.db schema v{current} is newer than this rvd (v{SCHEMA_VERSION}); upgrade rvd")
        for v in range(current + 1, SCHEMA_VERSION + 1):
            body = MIGRATIONS[v].replace(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);", "")
            # executescript manages its own transaction; one script = one atomic migration
            self.conn.executescript(
                "BEGIN IMMEDIATE;" + body +
                f"INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','{v}');COMMIT;")

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic, serialized transaction (BEGIN IMMEDIATE). Nested calls join the outer one."""
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()

    # --- generic helpers ---------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def q1(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql: str, args: tuple = ()) -> None:
        self.conn.execute(sql, args)

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        r = self.q1("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def meta_set(self, key: str, value: str) -> None:
        self.x("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))

    def event(self, actor: str, source: str, kind: str, payload: dict[str, Any] | None = None,
              contract_version: int | None = None) -> None:
        self.x("INSERT INTO events(ts,actor,source,kind,payload_json,contract_version) VALUES(?,?,?,?,?,?)",
               (now_iso(), actor, source, kind, json.dumps(payload or {}, ensure_ascii=False), contract_version))

    # --- leases (multi-process safety) -------------------------------------
    def acquire_lease(self, name: str, owner: str, ttl_sec: float) -> bool:
        with self.tx():
            r = self.q1("SELECT owner, expires_at FROM leases WHERE name=?", (name,))
            t = time.time()
            if r and r["expires_at"] > t and r["owner"] != owner:
                return False
            self.x("INSERT OR REPLACE INTO leases(name,owner,expires_at) VALUES(?,?,?)", (name, owner, t + ttl_sec))
            return True

    def release_lease(self, name: str, owner: str) -> None:
        with self.tx():
            self.x("DELETE FROM leases WHERE name=? AND owner=?", (name, owner))

    def release_leases_of(self, owner_prefix: str) -> int:
        with self.tx():
            cur = self.conn.execute("DELETE FROM leases WHERE owner LIKE ?", (owner_prefix + "%",))
            return cur.rowcount


def default_owner() -> str:
    return f"pid{os.getpid()}"
