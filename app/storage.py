from __future__ import annotations

import sqlite3
import time
import hmac
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator


class ReservationState(str, Enum):
    NEW = "new"
    RETRY = "retry"
    PROCESSING = "processing"
    RESPONSE_READY = "response_ready"
    SENT = "sent"


@dataclass(frozen=True, slots=True)
class MessageReservation:
    state: ReservationState
    response_text: str | None = None
    next_chunk_index: int = 0


@dataclass(frozen=True, slots=True)
class Owner:
    max_user_id: str
    chat_id: str
    claimed_at: int


@dataclass(frozen=True, slots=True)
class DailyUsage:
    day: str
    requests: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Conversation:
    id: str
    title: str
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: int
    role: str
    content: str
    created_at: int


class DailyLimitExceeded(RuntimeError):
    pass


class Storage:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    max_user_id TEXT NOT NULL UNIQUE,
                    chat_id TEXT NOT NULL,
                    claimed_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pairing (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    code_hash BLOB NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_messages (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('processing', 'failed', 'response_ready', 'sent')
                    ),
                    response_text TEXT,
                    next_chunk_index INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    first_seen_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_state (
                    chat_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_conversation_idx
                    ON messages(chat_id, generation, id);

                CREATE TABLE IF NOT EXISTS model_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_usage (
                    day TEXT PRIMARY KEY,
                    requests INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS web_conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS llm_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('success', 'error')),
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cost REAL,
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS llm_events_created_idx
                    ON llm_events(created_at, id);

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS audit_log_created_idx
                    ON audit_log(created_at, id);
                """
            )

    def transaction(self) -> Iterator[sqlite3.Connection]:
        return _ImmediateTransaction(self._connect())

    def get_owner(self) -> Owner | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT max_user_id, chat_id, claimed_at FROM owner WHERE singleton = 1"
            ).fetchone()
        return Owner(**dict(row)) if row else None

    def set_pairing_code(self, code_hash: bytes, expires_at: int, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO pairing(singleton, code_hash, expires_at, created_at)
                   VALUES(1, ?, ?, ?)
                   ON CONFLICT(singleton) DO UPDATE SET
                     code_hash=excluded.code_hash,
                     expires_at=excluded.expires_at,
                     created_at=excluded.created_at""",
                (code_hash, expires_at, timestamp),
            )

    def claim_owner(
        self,
        candidate_hash: bytes,
        sender_id: str,
        chat_id: str,
        now: int | None = None,
    ) -> str:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM owner WHERE singleton = 1").fetchone():
                return "already_claimed"
            pairing = connection.execute(
                "SELECT code_hash, expires_at FROM pairing WHERE singleton = 1"
            ).fetchone()
            if pairing is None:
                return "not_configured"
            if timestamp > pairing["expires_at"]:
                return "expired"
            if not hmac.compare_digest(bytes(pairing["code_hash"]), candidate_hash):
                return "invalid"
            connection.execute(
                "INSERT INTO owner(singleton, max_user_id, chat_id, claimed_at) VALUES(1, ?, ?, ?)",
                (sender_id, chat_id, timestamp),
            )
            connection.execute("DELETE FROM pairing WHERE singleton = 1")
            return "claimed"

    def reserve_message(self, message_id: str, chat_id: str, now: int | None = None) -> MessageReservation:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT status, response_text, next_chunk_index
                   FROM processed_messages WHERE message_id = ?""",
                (message_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO processed_messages(
                           message_id, chat_id, status, first_seen_at, updated_at
                       ) VALUES(?, ?, 'processing', ?, ?)""",
                    (message_id, chat_id, timestamp, timestamp),
                )
                return MessageReservation(ReservationState.NEW)
            if row["status"] == "failed":
                connection.execute(
                    """UPDATE processed_messages
                       SET status='processing', error_code=NULL, updated_at=?
                       WHERE message_id=?""",
                    (timestamp, message_id),
                )
                return MessageReservation(ReservationState.RETRY)
            return MessageReservation(
                ReservationState(row["status"]),
                row["response_text"],
                int(row["next_chunk_index"]),
            )

    def mark_failed(self, message_id: str, error_code: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            connection.execute(
                """UPDATE processed_messages
                   SET status='failed', error_code=?, updated_at=?
                   WHERE message_id=? AND status='processing'""",
                (error_code[:100], timestamp, message_id),
            )

    def store_response(self, message_id: str, response_text: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE processed_messages
                   SET status='response_ready', response_text=?, error_code=NULL, updated_at=?
                   WHERE message_id=? AND status='processing'""",
                (response_text, timestamp, message_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("message is not in processing state")

    def mark_sent(self, message_id: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE processed_messages SET status='sent', updated_at=?
                   WHERE message_id=? AND status='response_ready'""",
                (timestamp, message_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("message has no ready response")

    def mark_chunk_sent(
        self, message_id: str, chunk_index: int, now: int | None = None
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE processed_messages
                   SET next_chunk_index=next_chunk_index+1, updated_at=?
                   WHERE message_id=? AND status='response_ready' AND next_chunk_index=?""",
                (timestamp, message_id, chunk_index),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("message chunk progress is out of sequence")

    def append_message(self, chat_id: str, role: str, content: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO conversation_state(chat_id, generation) VALUES(?, 1)",
                (chat_id,),
            )
            generation = connection.execute(
                "SELECT generation FROM conversation_state WHERE chat_id=?", (chat_id,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO messages(chat_id, generation, role, content, created_at) VALUES(?, ?, ?, ?, ?)",
                (chat_id, generation, role, content, timestamp),
            )

    def get_history(self, chat_id: str, limit: int = 50) -> list[tuple[str, str]]:
        if limit < 1:
            return []
        with self._connection() as connection:
            state = connection.execute(
                "SELECT generation FROM conversation_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if state is None:
                return []
            rows = connection.execute(
                """SELECT role, content FROM messages
                   WHERE chat_id=? AND generation=? ORDER BY id DESC LIMIT ?""",
                (chat_id, state["generation"], limit),
            ).fetchall()
        return [(row["role"], row["content"]) for row in reversed(rows)]

    def new_conversation(self, chat_id: str) -> int:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO conversation_state(chat_id, generation) VALUES(?, 1)
                   ON CONFLICT(chat_id) DO UPDATE SET generation=generation+1""",
                (chat_id,),
            )
            return int(connection.execute(
                "SELECT generation FROM conversation_state WHERE chat_id=?", (chat_id,)
            ).fetchone()[0])

    def set_model_setting(self, key: str, value: str, now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO model_settings(key, value, updated_at) VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, timestamp),
            )

    def get_model_setting(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM model_settings WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else None

    def get_model_settings(self) -> dict[str, str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT key, value FROM model_settings ORDER BY key"
            ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def create_web_conversation(self, title: str = "Новый диалог", now: int | None = None) -> Conversation:
        timestamp = int(time.time()) if now is None else now
        conversation_id = f"web:{uuid.uuid4().hex}"
        clean_title = title.strip()[:120] or "Новый диалог"
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO web_conversations(id, title, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (conversation_id, clean_title, timestamp, timestamp),
            )
        return Conversation(conversation_id, clean_title, timestamp, timestamp)

    def list_web_conversations(self, limit: int = 100) -> list[Conversation]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT id, title, created_at, updated_at FROM web_conversations
                   ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [Conversation(**dict(row)) for row in rows]

    def get_web_conversation(self, conversation_id: str) -> Conversation | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, title, created_at, updated_at FROM web_conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
        return Conversation(**dict(row)) if row else None

    def touch_web_conversation(
        self, conversation_id: str, *, title: str | None = None, now: int | None = None
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            if title is None:
                connection.execute(
                    "UPDATE web_conversations SET updated_at=? WHERE id=?",
                    (timestamp, conversation_id),
                )
            else:
                connection.execute(
                    "UPDATE web_conversations SET title=?, updated_at=? WHERE id=?",
                    (title.strip()[:120] or "Новый диалог", timestamp, conversation_id),
                )

    def get_messages(self, chat_id: str, limit: int = 200) -> list[StoredMessage]:
        with self._connection() as connection:
            state = connection.execute(
                "SELECT generation FROM conversation_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if state is None:
                return []
            rows = connection.execute(
                """SELECT id, role, content, created_at FROM messages
                   WHERE chat_id=? AND generation=? ORDER BY id DESC LIMIT ?""",
                (chat_id, state["generation"], max(1, min(limit, 1000))),
            ).fetchall()
        return [StoredMessage(**dict(row)) for row in reversed(rows)]

    def delete_web_conversation(self, conversation_id: str) -> bool:
        if not conversation_id.startswith("web:"):
            return False
        with self._connection() as connection:
            connection.execute("DELETE FROM messages WHERE chat_id=?", (conversation_id,))
            connection.execute("DELETE FROM conversation_state WHERE chat_id=?", (conversation_id,))
            cursor = connection.execute(
                "DELETE FROM web_conversations WHERE id=?", (conversation_id,)
            )
        return cursor.rowcount == 1

    def record_llm_event(
        self,
        *,
        provider: str,
        model: str,
        source: str,
        status: str,
        latency_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float | None = None,
        error_code: str | None = None,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO llm_events(
                       created_at, provider, model, source, status, latency_ms,
                       input_tokens, output_tokens, cost, error_code
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp,
                    provider[:40],
                    model[:160],
                    source[:40],
                    status,
                    max(0, latency_ms),
                    max(0, input_tokens),
                    max(0, output_tokens),
                    cost,
                    error_code[:100] if error_code else None,
                ),
            )

    def get_llm_stats(self, since: int) -> dict[str, object]:
        with self._connection() as connection:
            summary = connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                          SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,
                          COALESCE(SUM(input_tokens), 0) AS input_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(AVG(CASE WHEN status='success' THEN latency_ms END), 0) AS avg_latency_ms,
                          COALESCE(SUM(cost), 0) AS cost
                   FROM llm_events WHERE created_at>=?""",
                (since,),
            ).fetchone()
            daily = connection.execute(
                """SELECT date(created_at, 'unixepoch', 'localtime') AS day,
                          COUNT(*) AS requests,
                          SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                          COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
                   FROM llm_events WHERE created_at>=?
                   GROUP BY day ORDER BY day""",
                (since,),
            ).fetchall()
            models = connection.execute(
                """SELECT provider, model, COUNT(*) AS requests,
                          COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
                   FROM llm_events WHERE created_at>=?
                   GROUP BY provider, model ORDER BY requests DESC""",
                (since,),
            ).fetchall()
        return {
            "summary": dict(summary),
            "daily": [dict(row) for row in daily],
            "models": [dict(row) for row in models],
        }

    def get_runtime_counts(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM processed_messages GROUP BY status"
            ).fetchall()
        result = {"processing": 0, "failed": 0, "response_ready": 0, "sent": 0}
        result.update({str(row["status"]): int(row["count"]) for row in rows})
        return result

    def add_audit(self, action: str, details: str = "", now: int | None = None) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO audit_log(created_at, action, details) VALUES(?, ?, ?)",
                (timestamp, action[:100], details[:500]),
            )

    def get_audit(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, created_at, action, details FROM audit_log ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_daily_request(
        self,
        day: str,
        max_requests: int,
        max_tokens: int,
    ) -> DailyUsage:
        with self.transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO daily_usage(day) VALUES(?)", (day,))
            row = connection.execute(
                """SELECT day, requests, input_tokens, output_tokens
                   FROM daily_usage WHERE day=?""",
                (day,),
            ).fetchone()
            if row["requests"] >= max_requests:
                raise DailyLimitExceeded("daily request limit reached")
            if row["input_tokens"] + row["output_tokens"] >= max_tokens:
                raise DailyLimitExceeded("daily token limit reached")
            connection.execute(
                "UPDATE daily_usage SET requests=requests+1 WHERE day=?", (day,)
            )
            return DailyUsage(
                day=day,
                requests=int(row["requests"]) + 1,
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
            )

    def record_token_usage(self, day: str, input_tokens: int, output_tokens: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO daily_usage(day, requests, input_tokens, output_tokens)
                   VALUES(?, 0, ?, ?)
                   ON CONFLICT(day) DO UPDATE SET
                     input_tokens=input_tokens+excluded.input_tokens,
                     output_tokens=output_tokens+excluded.output_tokens""",
                (day, max(0, input_tokens), max(0, output_tokens)),
            )

    def get_daily_usage(self, day: str) -> DailyUsage:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT day, requests, input_tokens, output_tokens
                   FROM daily_usage WHERE day=?""",
                (day,),
            ).fetchone()
        if row is None:
            return DailyUsage(day, 0, 0, 0)
        return DailyUsage(
            day=str(row["day"]),
            requests=int(row["requests"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
        )


class _ImmediateTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
