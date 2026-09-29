"""SQLite-backed state for delivery dedup and issue -> Devin session mapping.

Kept deliberately small: it is enough to deduplicate webhook retries and to
resume the right Devin session when someone replies to its question on an
issue. A richer dispatch state machine is tracked in TODO.md.
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone


class Store:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
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
                    delivery_id TEXT PRIMARY KEY,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS issues (
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    session_id TEXT,
                    session_url TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (repo, issue_number)
                );
                """
            )

    def mark_delivery(self, delivery_id: str) -> bool:
        """Record a webhook delivery. Returns False if already seen."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO deliveries (delivery_id, received_at) VALUES (?, ?)",
                    (delivery_id, now),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def record_dispatch(
        self, repo: str, issue_number: int, session_id: str, session_url: str, status: str
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO issues (repo, issue_number, session_id, session_url, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repo, issue_number) DO UPDATE SET
                    session_id = excluded.session_id,
                    session_url = excluded.session_url,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (repo, issue_number, session_id, session_url, status, now, now),
            )

    def update_status(self, repo: str, issue_number: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "UPDATE issues SET status = ?, updated_at = ? WHERE repo = ? AND issue_number = ?",
                (status, now, repo, issue_number),
            )

    def get_issue(self, repo: str, issue_number: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM issues WHERE repo = ? AND issue_number = ?",
                (repo, issue_number),
            ).fetchone()
        return dict(row) if row else None
