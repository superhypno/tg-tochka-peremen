"""Небольшой SQLite-слой для бота без внешних зависимостей."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    first_source TEXT,
                    last_source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS test_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    source TEXT,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    result_type TEXT
                );
                CREATE TABLE IF NOT EXISTS answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES test_sessions(id) ON DELETE CASCADE,
                    question_index INTEGER NOT NULL,
                    question_key TEXT NOT NULL,
                    option_key TEXT NOT NULL,
                    option_text TEXT NOT NULL,
                    result_type TEXT NOT NULL,
                    answered_at TEXT NOT NULL,
                    UNIQUE(session_id, question_index)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    event_type TEXT NOT NULL,
                    payload TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON test_sessions(telegram_id);
                CREATE INDEX IF NOT EXISTS idx_events_user ON events(telegram_id);
                """
            )

    def upsert_user(self, user: Any, source: str | None) -> None:
        timestamp = now()
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name, first_source, last_source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name,
                    last_source=excluded.last_source, updated_at=excluded.updated_at
                """,
                (user.id, user.username, user.first_name, user.last_name, source, source, timestamp, timestamp),
            )

    def log_event(self, telegram_id: int, event_type: str, payload: dict[str, Any] | None = None) -> None:
        with self.connect() as con:
            con.execute(
                "INSERT INTO events (telegram_id, event_type, payload, created_at) VALUES (?, ?, ?, ?)",
                (telegram_id, event_type, json.dumps(payload, ensure_ascii=False) if payload else None, now()),
            )

    def start_session(self, telegram_id: int, source: str | None) -> int:
        with self.connect() as con:
            con.execute(
                "UPDATE test_sessions SET status='abandoned', completed_at=? WHERE telegram_id=? AND status='in_progress'",
                (now(), telegram_id),
            )
            cursor = con.execute(
                "INSERT INTO test_sessions (telegram_id, source, status, started_at) VALUES (?, ?, 'in_progress', ?)",
                (telegram_id, source, now()),
            )
            return int(cursor.lastrowid)

    def save_answer(
        self, session_id: int, question_index: int, question_key: str, option_key: str, option_text: str, result_type: str
    ) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO answers (session_id, question_index, question_key, option_key, option_text, result_type, answered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, question_index) DO NOTHING
                """,
                (session_id, question_index, question_key, option_key, option_text, result_type, now()),
            )

    def finish_session(self, session_id: int, result_type: str) -> None:
        with self.connect() as con:
            con.execute(
                "UPDATE test_sessions SET status='completed', completed_at=?, result_type=? WHERE id=?",
                (now(), result_type, session_id),
            )
