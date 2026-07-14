"""Thread-safe SQLite/SQLCipher persistence for agency state."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import AgencyConfig

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


class AgencyStore:
    """Small durable ledger.

    Connections are short-lived so plugin reloads and gateway threads do not
    share transaction state. Writes are serialized inside this process;
    SQLite WAL and busy_timeout cover separate Hermes processes.
    """

    def __init__(self, config: AgencyConfig):
        self.config = config
        self.path = config.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            with suppress(OSError):
                os.chmod(self.path.parent, 0o700)
        self._lock = threading.RLock()
        self._driver = self._select_driver()
        self._initialize()

    def _select_driver(self):
        if not self.config.database_encryption:
            import sqlite3

            return sqlite3
        key = os.environ.get(self.config.database_key_env, "")
        if not key:
            raise RuntimeError(
                f"database_encryption is enabled but {self.config.database_key_env} is unset"
            )
        try:
            from sqlcipher3 import dbapi2 as sqlcipher
        except ImportError as exc:
            raise RuntimeError(
                "SQLCipher support is required; install the plugin's encryption extra"
            ) from exc
        return sqlcipher

    @contextmanager
    def connection(self) -> Iterator[Any]:
        conn = self._driver.connect(str(self.path), timeout=10.0)
        try:
            if self.config.database_encryption:
                secret = os.environ[self.config.database_key_env].encode("utf-8")
                raw_key = hashlib.sha256(secret).hexdigest()
                conn.execute(f"PRAGMA key = \"x'{raw_key}'\"")
                conn.execute("PRAGMA cipher_memory_security = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._lock, self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, created_at DESC);
                CREATE TABLE IF NOT EXISTS intentions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    autonomy TEXT NOT NULL,
                    due_at TEXT,
                    source TEXT NOT NULL DEFAULT 'agent',
                    last_considered_at TEXT,
                    last_acted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_intentions_status_priority
                    ON intentions(status, priority DESC, updated_at DESC);
                CREATE TABLE IF NOT EXISTS reflections (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    insight TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_reflections_created
                    ON reflections(created_at DESC);
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    intention_id TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    delivery_status TEXT NOT NULL DEFAULT 'not_applicable',
                    FOREIGN KEY(intention_id) REFERENCES intentions(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_created
                    ON decisions(created_at DESC);
                """
            )
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is not None:
                current = self._loads(row[0], 0)
                if type(current) is not int or current < 0:
                    raise RuntimeError("invalid agency database schema version")
                if current > SCHEMA_VERSION:
                    raise RuntimeError(
                        f"agency database schema {current} is newer than supported "
                        f"schema {SCHEMA_VERSION}"
                    )
            self._set_meta_conn(conn, "schema_version", SCHEMA_VERSION)
        self._restrict_permissions()

    def _restrict_permissions(self) -> None:
        if os.name != "nt":
            try:
                os.chmod(self.path.parent, 0o700)
                for candidate in self.path.parent.glob(f"{self.path.name}*"):
                    if candidate.is_file():
                        os.chmod(candidate, 0o600)
            except OSError:
                pass

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _loads(value: str, default: Any) -> Any:
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    def _set_meta_conn(self, conn: Any, key: str, value: Any) -> None:
        conn.execute(
            """INSERT INTO meta(key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value, updated_at=excluded.updated_at""",
            (key, self._json(value), iso_now()),
        )

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock, self.connection() as conn:
            self._set_meta_conn(conn, key, value)

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return default if row is None else self._loads(row[0], default)

    def add_event(
        self,
        kind: str,
        *,
        session_id: str = "",
        task_id: str = "",
        platform: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._lock, self.connection() as conn:
            cur = conn.execute(
                """INSERT INTO events(
                       created_at, kind, session_id, task_id, platform, summary, metadata
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    iso_now(),
                    kind[:80],
                    session_id[:200],
                    task_id[:200],
                    platform[:80],
                    summary[:4000],
                    self._json(metadata or {}),
                ),
            )
            event_id = int(cur.lastrowid)
        return event_id

    def recent_events(
        self, limit: int = 25, kinds: list[str] | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        params: list[Any] = []
        where = ""
        if kinds:
            clean = [str(item)[:80] for item in kinds[:20]]
            where = f"WHERE kind IN ({','.join('?' for _ in clean)})"
            params.extend(clean)
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(
                f"""SELECT id, created_at, kind, session_id, task_id, platform, summary, metadata
                    FROM events {where} ORDER BY id DESC LIMIT ?""",
                params,
            ).fetchall()
        return [
            {
                "id": row[0],
                "created_at": row[1],
                "kind": row[2],
                "session_id": row[3],
                "task_id": row[4],
                "platform": row[5],
                "summary": row[6],
                "metadata": self._loads(row[7], {}),
            }
            for row in rows
        ]

    def prune_events(self) -> int:
        cutoff = (utc_now() - timedelta(days=self.config.event_retention_days)).isoformat()
        with self._lock, self.connection() as conn:
            old = conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,)).rowcount
            overflow = conn.execute(
                """DELETE FROM events WHERE id NOT IN
                   (SELECT id FROM events ORDER BY id DESC LIMIT ?)""",
                (self.config.maximum_events,),
            ).rowcount
        return max(0, old) + max(0, overflow)

    def add_intention(
        self,
        title: str,
        *,
        rationale: str = "",
        priority: int = 50,
        autonomy: str = "propose",
        due_at: str | None = None,
        source: str = "agent",
    ) -> dict[str, Any]:
        clean_title = title.strip()[:500]
        if not clean_title:
            raise ValueError("intention title is required")
        if autonomy not in {"reflect", "propose", "message"}:
            raise ValueError("invalid intention autonomy")
        now = iso_now()
        item_id = uuid.uuid4().hex[:16]
        with self._lock, self.connection() as conn:
            conn.execute(
                """INSERT INTO intentions(
                       id, created_at, updated_at, title, rationale, priority,
                       status, autonomy, due_at, source
                   )
                   VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)""",
                (
                    item_id,
                    now,
                    now,
                    clean_title,
                    rationale.strip()[:2000],
                    max(0, min(int(priority), 100)),
                    autonomy,
                    due_at,
                    source[:80],
                ),
            )
        return self.get_intention(item_id) or {}

    def get_intention(self, item_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT id, created_at, updated_at, title, rationale, priority, status,
                          autonomy, due_at, source, last_considered_at, last_acted_at
                   FROM intentions WHERE id = ?""",
                (item_id,),
            ).fetchone()
        return self._intention_row(row) if row else None

    @staticmethod
    def _intention_row(row: Any) -> dict[str, Any]:
        keys = (
            "id",
            "created_at",
            "updated_at",
            "title",
            "rationale",
            "priority",
            "status",
            "autonomy",
            "due_at",
            "source",
            "last_considered_at",
            "last_acted_at",
        )
        return dict(zip(keys, row, strict=True))

    def list_intentions(self, status: str = "active", limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        if status not in {"active", "blocked", "completed", "cancelled", "all"}:
            raise ValueError("invalid intention status")
        if status == "all":
            sql = "SELECT * FROM intentions ORDER BY priority DESC, updated_at DESC LIMIT ?"
            params: tuple[Any, ...] = (limit,)
        else:
            sql = (
                "SELECT * FROM intentions WHERE status = ? "
                "ORDER BY priority DESC, updated_at DESC LIMIT ?"
            )
            params = (status, limit)
        with self.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._intention_row(row) for row in rows]

    def update_intention(
        self,
        item_id: str,
        *,
        status: str | None = None,
        priority: int | None = None,
        title: str | None = None,
        rationale: str | None = None,
        considered: bool = False,
        acted: bool = False,
    ) -> dict[str, Any] | None:
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [iso_now()]
        if status is not None:
            if status not in {"active", "blocked", "completed", "cancelled"}:
                raise ValueError("invalid intention status")
            updates.append("status = ?")
            params.append(status)
        if priority is not None:
            updates.append("priority = ?")
            params.append(max(0, min(int(priority), 100)))
        if title is not None:
            if not title.strip():
                raise ValueError("intention title is required")
            updates.append("title = ?")
            params.append(title.strip()[:500])
        if rationale is not None:
            updates.append("rationale = ?")
            params.append(rationale.strip()[:2000])
        if considered:
            updates.append("last_considered_at = ?")
            params.append(iso_now())
        if acted:
            updates.append("last_acted_at = ?")
            params.append(iso_now())
        params.append(item_id)
        with self._lock, self.connection() as conn:
            conn.execute(f"UPDATE intentions SET {', '.join(updates)} WHERE id = ?", params)
        return self.get_intention(item_id)

    def add_reflection(
        self,
        kind: str,
        summary: str,
        *,
        insight: str = "",
        confidence: float = 0.5,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_kind = kind.strip()[:80] or "general"
        clean_summary = summary.strip()[:2000]
        clean_insight = insight.strip()[:4000]
        clean_confidence = max(0.0, min(float(confidence), 1.0))
        if not clean_summary:
            raise ValueError("reflection summary is required")
        item_id, created = uuid.uuid4().hex[:16], iso_now()
        with self._lock, self.connection() as conn:
            conn.execute(
                """INSERT INTO reflections(
                       id, created_at, kind, summary, insight, confidence, metadata
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    created,
                    clean_kind,
                    clean_summary,
                    clean_insight,
                    clean_confidence,
                    self._json(metadata or {}),
                ),
            )
        return {
            "id": item_id,
            "created_at": created,
            "kind": clean_kind,
            "summary": clean_summary,
            "insight": clean_insight,
            "confidence": clean_confidence,
            "metadata": metadata or {},
        }

    def recent_reflections(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id, created_at, kind, summary, insight, confidence, metadata
                   FROM reflections ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        keys = ("id", "created_at", "kind", "summary", "insight", "confidence", "metadata")
        result = []
        for row in rows:
            item = dict(zip(keys, row, strict=True))
            item["metadata"] = self._loads(item["metadata"], {})
            result.append(item)
        return result

    def add_decision(
        self,
        action: str,
        reason: str,
        *,
        intention_id: str | None = None,
        message: str = "",
        delivery_status: str = "not_applicable",
    ) -> dict[str, Any]:
        clean_reason = reason.strip()[:2000]
        clean_message = message.strip()[:4000]
        clean_status = delivery_status.strip()[:80]
        if action not in {"silent", "speak"}:
            raise ValueError("decision action must be silent or speak")
        if not clean_reason:
            raise ValueError("decision reason is required")
        if action == "speak" and not clean_message:
            raise ValueError("speak decision message is required")
        item_id, created = uuid.uuid4().hex[:16], iso_now()
        with self._lock, self.connection() as conn:
            conn.execute(
                """INSERT INTO decisions
                   (id, created_at, action, reason, intention_id, message, delivery_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    created,
                    action,
                    clean_reason,
                    intention_id,
                    clean_message,
                    clean_status,
                ),
            )
        return {
            "id": item_id,
            "created_at": created,
            "action": action,
            "reason": clean_reason,
            "intention_id": intention_id,
            "message": clean_message,
            "delivery_status": clean_status,
        }

    def recent_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id, created_at, action, reason, intention_id, message, delivery_status
                   FROM decisions ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        keys = (
            "id",
            "created_at",
            "action",
            "reason",
            "intention_id",
            "message",
            "delivery_status",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def proactive_count_since(self, since: datetime) -> int:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE action = 'speak' AND created_at >= ?",
                (since.astimezone(UTC).isoformat(),),
            ).fetchone()
        return int(row[0])

    def last_proactive_decision(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT id, created_at, action, reason, intention_id, message, delivery_status
                   FROM decisions WHERE action = 'speak' ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "created_at",
            "action",
            "reason",
            "intention_id",
            "message",
            "delivery_status",
        )
        return dict(zip(keys, row, strict=True))
