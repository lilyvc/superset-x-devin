"""SQLite persistence for workflow state, sessions, events, and webhook dedup."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .states import State

JSON_WORKFLOW_FIELDS = {"labels", "triage", "investigation", "remediation", "analysis"}
JSON_SESSION_FIELDS = {"structured_output", "pull_requests"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True)


def _decode(row: sqlite3.Row | dict, fields: set[str]) -> dict:
    result = dict(row)
    for field in fields:
        if result.get(field):
            try:
                result[field] = json.loads(result[field])
            except (TypeError, json.JSONDecodeError):
                pass
    return result


class Store:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY, received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
                    title TEXT, issue_url TEXT, author TEXT, labels TEXT,
                    state TEXT NOT NULL, needs_info_kind TEXT,
                    attempt INTEGER DEFAULT 0, remediation_attempts INTEGER DEFAULT 0,
                    pr_url TEXT, pr_number INTEGER, failure_reason TEXT,
                    triage TEXT, investigation TEXT, remediation TEXT, analysis TEXT,
                    last_comment_id INTEGER DEFAULT 0, discovered_at TEXT,
                    triaged_started_at TEXT,
                    started_at TEXT, investigated_at TEXT, reproduced_at TEXT,
                    root_cause_at TEXT, remediation_started_at TEXT,
                    pr_opened_at TEXT, completed_at TEXT, waiting_since TEXT,
                    updated_at TEXT, UNIQUE(repo, issue_number)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY, workflow_id INTEGER NOT NULL,
                    role TEXT NOT NULL, url TEXT, devin_status TEXT,
                    devin_status_detail TEXT, acus REAL,
                    structured_output TEXT, output_fingerprint TEXT,
                    acted_fingerprint TEXT DEFAULT '', pull_requests TEXT,
                    created_at TEXT, last_polled_at TEXT, finished_at TEXT,
                    active INTEGER DEFAULT 1, attempts INTEGER DEFAULT 0,
                    stall_nudged INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id INTEGER,
                    at TEXT NOT NULL, kind TEXT NOT NULL, from_state TEXT,
                    to_state TEXT, detail TEXT
                );
                CREATE TABLE IF NOT EXISTS learned_rules (
                    repo TEXT NOT NULL, rule TEXT NOT NULL, note_id TEXT,
                    workflow_id INTEGER, pr_url TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY (repo, rule)
                );
                CREATE TABLE IF NOT EXISTS issue_origins (
                    repo TEXT NOT NULL, issue_number INTEGER NOT NULL,
                    origin TEXT NOT NULL, parent_issue_number INTEGER,
                    session_id TEXT, created_at TEXT NOT NULL,
                    PRIMARY KEY (repo, issue_number)
                );
                """
            )
            self._ensure_columns(conn, "sessions", {
                "acted_fingerprint": "TEXT DEFAULT ''", "attempts": "INTEGER DEFAULT 0",
                "stall_nudged": "INTEGER DEFAULT 0",
            })
            self._ensure_columns(conn, "workflows", {
                "updated_at": "TEXT", "last_comment_id": "INTEGER DEFAULT 0",
                "triage": "TEXT", "triaged_started_at": "TEXT",
                "ci_status": "TEXT", "ci_checked_at": "TEXT",
                "ci_timeout_notified": "INTEGER DEFAULT 0",
                "origin": "TEXT DEFAULT 'HUMAN_REPORTED'",
                "parent_issue_number": "INTEGER",
                "discovered_by_session_id": "TEXT",
                # PR head sha recorded when the PR was first seen; compared at
                # merge time to detect whether humans pushed extra commits.
                "pr_head_sha": "TEXT",
                "merged_without_changes": "INTEGER",
                # Cursors for comment surfaces on the tracked PR — each lives
                # in a different id namespace.
                "last_pr_comment_id": "INTEGER DEFAULT 0",
                "last_pr_review_comment_id": "INTEGER DEFAULT 0",
                "last_pr_review_id": "INTEGER DEFAULT 0",
                # CI-failure handoff: last failing head sha handed to the
                # Remediator, how many were handed off, and the separate
                # CI-fix PR it opened when the failure was unrelated.
                "ci_failed_sha": "TEXT",
                "ci_fix_attempts": "INTEGER DEFAULT 0",
                "ci_fix_pr_url": "TEXT",
                "ci_fix_pr_merged": "INTEGER DEFAULT 0",
            })

    @staticmethod
    def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def mark_delivery(self, delivery_id: str) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("INSERT INTO deliveries VALUES (?, ?)", (delivery_id, utcnow()))
            return True
        except sqlite3.IntegrityError:
            return False

    def upsert_workflow(self, repo: str, issue_number: int, **fields) -> dict:
        fields = dict(fields)
        fields.setdefault("updated_at", utcnow())
        fields.setdefault("state", "DISCOVERED")
        columns = ["repo", "issue_number"] + list(fields)
        values = [repo, issue_number] + [
            _json(fields[c]) if c in JSON_WORKFLOW_FIELDS else fields[c] for c in fields
        ]
        updates = ", ".join(f"{c}=excluded.{c}" for c in fields if c != "state")
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO workflows ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(repo,issue_number) DO UPDATE SET {updates or 'updated_at=excluded.updated_at'}",
                values,
            )
        return self.get_workflow(repo, issue_number)

    def get_workflow(self, repo: str, issue_number: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE repo=? AND issue_number=?", (repo, issue_number)
            ).fetchone()
        return _decode(row, JSON_WORKFLOW_FIELDS) if row else None

    def get_workflow_by_id(self, workflow_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        return _decode(row, JSON_WORKFLOW_FIELDS) if row else None

    def list_workflows(self, states=None) -> list[dict]:
        with self._conn() as conn:
            if states:
                vals = [getattr(s, "value", s) for s in states]
                rows = conn.execute(
                    f"SELECT * FROM workflows WHERE state IN ({','.join('?' for _ in vals)}) "
                    "ORDER BY discovered_at, id", vals
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM workflows ORDER BY discovered_at, id").fetchall()
        return [_decode(r, JSON_WORKFLOW_FIELDS) for r in rows]

    def set_state(self, workflow_id: int, state, **fields) -> dict | None:
        state = getattr(state, "value", state)
        old = self.get_workflow_by_id(workflow_id)
        fields.update(state=state, updated_at=utcnow())
        assignments, values = [], []
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(_json(value) if key in JSON_WORKFLOW_FIELDS else value)
        values.append(workflow_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE workflows SET {','.join(assignments)} WHERE id=?", values)
        if old and old.get("state") != state:
            self.add_event(workflow_id, "transition", old.get("state"), state)
        return self.get_workflow_by_id(workflow_id)

    def add_event(self, workflow_id: int | None, kind: str, from_state=None,
                  to_state=None, detail: Any = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events(workflow_id,at,kind,from_state,to_state,detail) VALUES(?,?,?,?,?,?)",
                (workflow_id, utcnow(), kind, getattr(from_state, "value", from_state),
                 getattr(to_state, "value", to_state),
                 json.dumps(detail, sort_keys=True) if isinstance(detail, (dict, list)) else detail),
            )

    def get_events(self, workflow_id: int) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM events WHERE workflow_id=? ORDER BY id", (workflow_id,)
            ).fetchall()]

    def record_session(self, **fields) -> dict:
        fields.setdefault("created_at", utcnow())
        fields.setdefault("active", 1)
        fields.setdefault("structured_output", {})
        fields.setdefault("pull_requests", [])
        columns = list(fields)
        values = [_json(fields[c]) if c in JSON_SESSION_FIELDS else fields[c] for c in columns]
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO sessions({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                "ON CONFLICT(session_id) DO UPDATE SET workflow_id=excluded.workflow_id, role=excluded.role, "
                "url=excluded.url, active=excluded.active",
                values,
            )
        return self.get_session(fields["session_id"])

    def update_session(self, session_id: str, **fields) -> dict | None:
        if not fields:
            return self.get_session(session_id)
        assignments, values = [], []
        for key, value in fields.items():
            assignments.append(f"{key}=?")
            values.append(_json(value) if key in JSON_SESSION_FIELDS else value)
        values.append(session_id)
        with self._conn() as conn:
            conn.execute(f"UPDATE sessions SET {','.join(assignments)} WHERE session_id=?", values)
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return _decode(row, JSON_SESSION_FIELDS) if row else None

    def get_sessions(self, workflow_id: int, role=None, active_only=False) -> list[dict]:
        query, values = "SELECT * FROM sessions WHERE workflow_id=?", [workflow_id]
        if role:
            query += " AND role=?"
            values.append(getattr(role, "value", role))
        if active_only:
            query += " AND active=1"
        query += " ORDER BY created_at"
        with self._conn() as conn:
            rows = conn.execute(query, values).fetchall()
        return [_decode(r, JSON_SESSION_FIELDS) for r in rows]

    def delete_sessions(self, workflow_id: int, role=None) -> int:
        query, values = "DELETE FROM sessions WHERE workflow_id=?", [workflow_id]
        if role:
            query += " AND role=?"
            values.append(getattr(role, "value", role))
        with self._conn() as conn:
            return conn.execute(query, values).rowcount

    def count_active_sessions(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT count(*) FROM sessions WHERE active=1").fetchone()[0]

    def count_running_sessions(self) -> int:
        """Active sessions that are not parked waiting on a human reply."""
        with self._conn() as conn:
            return conn.execute(
                "SELECT count(*) FROM sessions s JOIN workflows w ON w.id=s.workflow_id "
                "WHERE s.active=1 AND w.state NOT IN (?, ?)",
                (State.NEEDS_INFO.value, State.BLOCKED.value),
            ).fetchone()[0]

    def total_acus(self) -> float:
        with self._conn() as conn:
            return float(
                conn.execute("SELECT COALESCE(SUM(acus), 0) FROM sessions").fetchone()[0]
            )

    def list_active_sessions(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM sessions WHERE active=1 ORDER BY created_at").fetchall()
        return [_decode(r, JSON_SESSION_FIELDS) for r in rows]

    def record_issue_origin(self, repo: str, issue_number: int, origin: str,
                            parent_issue_number: int | None = None,
                            session_id: str | None = None) -> None:
        """Provenance for an issue before intake discovers it (e.g. an Analyst
        files a follow-up issue; discovery later stamps it DEVIN_DISCOVERED)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO issue_origins VALUES (?,?,?,?,?,?)",
                (repo, issue_number, origin, parent_issue_number, session_id, utcnow()),
            )
        # If the workflow already exists, stamp it now.
        workflow = self.get_workflow(repo, issue_number)
        if workflow and workflow.get("origin") != origin:
            self.set_state(workflow["id"], workflow["state"], origin=origin,
                           parent_issue_number=parent_issue_number,
                           discovered_by_session_id=session_id)


    def get_issue_origin(self, repo: str, issue_number: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM issue_origins WHERE repo=? AND issue_number=?",
                (repo, issue_number),
            ).fetchone()
        return dict(row) if row else None

    def recent_analyses(self, repo: str, limit: int = 10) -> list[dict]:
        """Completed defect-family analyses for this repo, newest first —
        the knowledge loop feeding later sessions' prompts."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT issue_number, analysis FROM workflows "
                "WHERE repo=? AND analysis IS NOT NULL AND analysis != '{}' "
                "ORDER BY discovered_at DESC, id DESC LIMIT ?",
                (repo, limit),
            ).fetchall()
        return [_decode(r, JSON_WORKFLOW_FIELDS) for r in rows]

    def has_learned_rule(self, repo: str, rule: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM learned_rules WHERE repo=? AND rule=?",
                                (repo, rule)).fetchone() is not None

    def add_learned_rule(self, repo: str, rule: str, note_id: str | None,
                         workflow_id: int, pr_url: str | None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO learned_rules VALUES (?,?,?,?,?,?)",
                (repo, rule, note_id, workflow_id, pr_url, utcnow()),
            )

    def list_learned_rules(self, repo: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT l.rule, l.note_id, l.pr_url, l.created_at, w.issue_number "
                "FROM learned_rules l LEFT JOIN workflows w ON w.id=l.workflow_id "
                "WHERE l.repo=? ORDER BY l.created_at DESC", (repo,),
            ).fetchall()
        return [dict(r) for r in rows]

    def children_of(self, repo: str, issue_number: int) -> list[dict]:
        """Workflows that were discovered by (as follow-ups of) this issue."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM workflows WHERE repo=? AND parent_issue_number=? "
                "ORDER BY discovered_at, id",
                (repo, issue_number),
            ).fetchall()
        return [_decode(r, JSON_WORKFLOW_FIELDS) for r in rows]
