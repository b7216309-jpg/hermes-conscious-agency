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
from zoneinfo import ZoneInfo

from .config import AgencyConfig

SCHEMA_VERSION = 2


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
            conn.execute(
                """CREATE TABLE IF NOT EXISTS meta (
                       key TEXT PRIMARY KEY,
                       value TEXT NOT NULL,
                       updated_at TEXT NOT NULL
                   )"""
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
            conn.executescript(
                """
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
                CREATE TABLE IF NOT EXISTS subjective_entries (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    capture_key TEXT NOT NULL UNIQUE,
                    model_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    prior_entry_id TEXT,
                    output_text TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(prior_entry_id) REFERENCES subjective_entries(id)
                        ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_subjective_model_created
                    ON subjective_entries(model_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_subjective_source_created
                    ON subjective_entries(source, created_at DESC);
                """
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

    def _normalize_due_at(self, value: Any) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("due_at must be a valid ISO-8601 date or date-time") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo(self.config.timezone))
        return parsed.astimezone(UTC).isoformat()

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
                    self._normalize_due_at(due_at),
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

    def intention_status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in ("active", "blocked", "completed", "cancelled")}
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM intentions GROUP BY status"
            ).fetchall()
        for status, count in rows:
            if status in counts:
                counts[status] = int(count)
        return counts

    def update_intention(
        self,
        item_id: str,
        *,
        status: str | None = None,
        priority: int | None = None,
        title: str | None = None,
        rationale: str | None = None,
        due_at: str | None = None,
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
        if due_at is not None:
            updates.append("due_at = ?")
            params.append(self._normalize_due_at(due_at))
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

    @staticmethod
    def _subjective_row(row: Any) -> dict[str, Any]:
        keys = (
            "id",
            "created_at",
            "capture_key",
            "model_id",
            "source",
            "condition",
            "prompt_version",
            "session_id",
            "prior_entry_id",
            "output_text",
            "output_sha256",
            "metadata",
        )
        item = dict(zip(keys, row, strict=True))
        item["metadata"] = AgencyStore._loads(item["metadata"], {})
        return item

    def latest_subjective_entry(
        self,
        model_id: str,
        *,
        source: str = "",
        condition: str = "",
        prompt_version: str = "",
        exclude_session_id: str = "",
    ) -> dict[str, Any] | None:
        clean_model = model_id.strip()[:500] or "unknown"
        clauses = ["model_id = ?"]
        params: list[Any] = [clean_model]
        if source.strip():
            clean_source = source.strip().lower()
            if clean_source not in {"cron", "conversation"}:
                raise ValueError("subjective source must be cron or conversation")
            clauses.append("source = ?")
            params.append(clean_source)
        if condition.strip():
            clean_condition = condition.strip().lower()
            if clean_condition not in {"cold", "continuity"}:
                raise ValueError("subjective condition must be cold or continuity")
            clauses.append("condition = ?")
            params.append(clean_condition)
        if prompt_version.strip():
            clauses.append("prompt_version = ?")
            params.append(prompt_version.strip()[:80])
        if exclude_session_id.strip():
            clauses.append("session_id != ?")
            params.append(exclude_session_id.strip()[:500])
        with self.connection() as conn:
            row = conn.execute(
                """SELECT id, created_at, capture_key, model_id, source, condition,
                          prompt_version, session_id, prior_entry_id, output_text,
                          output_sha256, metadata
                   FROM subjective_entries
                   WHERE """
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                params,
            ).fetchone()
        return self._subjective_row(row) if row else None

    def add_subjective_entry(
        self,
        *,
        capture_key: str,
        model_id: str,
        source: str,
        condition: str,
        prompt_version: str,
        session_id: str,
        output_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_capture = capture_key.strip()[:500]
        clean_model = model_id.strip()[:500] or "unknown"
        clean_source = source.strip().lower()
        clean_condition = condition.strip().lower()
        clean_version = prompt_version.strip()[:80]
        response = str(output_text)
        if not clean_capture:
            raise ValueError("subjective capture_key is required")
        if clean_source not in {"cron", "conversation"}:
            raise ValueError("subjective source must be cron or conversation")
        if clean_condition not in {"cold", "continuity"}:
            raise ValueError("subjective condition must be cold or continuity")
        if not clean_version:
            raise ValueError("subjective prompt_version is required")
        if not response:
            raise ValueError("subjective output_text is required")
        item_id, created = uuid.uuid4().hex[:16], iso_now()
        digest = hashlib.sha256(response.encode("utf-8")).hexdigest()
        with self._lock, self.connection() as conn:
            # Keep the per-model/source link read and entry write in one cross-process write lock.
            conn.execute("BEGIN IMMEDIATE")
            prior_id = None
            if clean_condition == "continuity":
                prior_row = conn.execute(
                    """SELECT id FROM subjective_entries
                       WHERE model_id = ? AND source = ? AND condition = ?
                             AND prompt_version = ?
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (clean_model, clean_source, clean_condition, clean_version),
                ).fetchone()
                prior_id = prior_row[0] if prior_row else None
            conn.execute(
                """INSERT OR IGNORE INTO subjective_entries(
                       id, created_at, capture_key, model_id, source, condition,
                       prompt_version, session_id, prior_entry_id, output_text,
                       output_sha256, metadata
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    created,
                    clean_capture,
                    clean_model,
                    clean_source,
                    clean_condition,
                    clean_version,
                    session_id.strip()[:500],
                    prior_id,
                    response,
                    digest,
                    self._json(metadata or {}),
                ),
            )
            row = conn.execute(
                """SELECT id, created_at, capture_key, model_id, source, condition,
                          prompt_version, session_id, prior_entry_id, output_text,
                          output_sha256, metadata
                   FROM subjective_entries WHERE capture_key = ?""",
                (clean_capture,),
            ).fetchone()
        if row is None:  # pragma: no cover - SQLite should make this unreachable
            raise RuntimeError("subjective entry was not persisted")
        return self._subjective_row(row)

    def recent_subjective_entries(
        self,
        limit: int = 100,
        *,
        model_id: str = "",
        source: str = "",
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100_000))
        clauses: list[str] = []
        params: list[Any] = []
        if model_id.strip():
            clauses.append("model_id = ?")
            params.append(model_id.strip()[:500])
        if source.strip():
            clean_source = source.strip().lower()
            if clean_source not in {"cron", "conversation"}:
                raise ValueError("subjective source must be cron or conversation")
            clauses.append("source = ?")
            params.append(clean_source)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT id, created_at, capture_key, model_id, source, condition,
                          prompt_version, session_id, prior_entry_id, output_text,
                          output_sha256, metadata
                   FROM subjective_entries"""
                + where
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._subjective_row(row) for row in rows]

    def subjective_summary(self) -> dict[str, Any]:
        with self.connection() as conn:
            aggregate = conn.execute(
                """SELECT COUNT(*), MIN(created_at), MAX(created_at),
                          COALESCE(AVG(LENGTH(output_text)), 0),
                          SUM(CASE WHEN LOWER(TRIM(output_text)) = '[silent]' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN prior_entry_id IS NOT NULL THEN 1 ELSE 0 END)
                   FROM subjective_entries"""
            ).fetchone()
            models = conn.execute(
                """SELECT model_id, COUNT(*) FROM subjective_entries
                   GROUP BY model_id ORDER BY COUNT(*) DESC, model_id ASC"""
            ).fetchall()
            sources = conn.execute(
                """SELECT source, COUNT(*) FROM subjective_entries
                   GROUP BY source ORDER BY source ASC"""
            ).fetchall()
        return {
            "entries": int(aggregate[0] or 0),
            "first_at": aggregate[1],
            "last_at": aggregate[2],
            "average_chars": round(float(aggregate[3] or 0), 2),
            "silent_entries": int(aggregate[4] or 0),
            "continuity_links": int(aggregate[5] or 0),
            "models": {str(row[0]): int(row[1]) for row in models},
            "sources": {str(row[0]): int(row[1]) for row in sources},
        }
