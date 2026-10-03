"""Private, local persistence for explicit memories, history, and reminders.

Only ``remember`` stores facts. Conversation history is kept separately and is
never automatically promoted to a fact. Each operation opens its own SQLite
connection so a store can safely be shared by the CLI and background workers.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any, Iterator


MAX_FACT_LENGTH = 4096
MAX_QUERY_LENGTH = 1024
MAX_HISTORY_LENGTH = 32768
MAX_RESULTS = 100
MAX_SEARCH_TOKENS = 32
MAX_HISTORY_PAIRS = 500


def _text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value.strip()


def _integer(value: int, name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < 1 or value > (maximum if maximum is not None else 2**63 - 1):
        bound = f" between 1 and {maximum}" if maximum is not None else " a positive SQLite integer"
        raise ValueError(f"{name} must be{bound}")
    return value


def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


class MemoryStore:
    """A SQLite store at ``data_dir / 'memory.sqlite3'``.

    ``recent_history`` takes a message limit and returns complete user/assistant
    pairs. ``list_reminders`` lists pending reminders only. Due reminders are
    claimed atomically, so two workers cannot deliver the same reminder.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db_path = self.data_dir / "memory.sqlite3"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.db_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("memory database must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    search_text TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_text TEXT NOT NULL,
                    reply TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS reminders_due
                    ON reminders(delivered_at, due_at);
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def remember(self, text: str) -> int:
        text = _text(text, "memory text", MAX_FACT_LENGTH)
        search_text = " " + " ".join(_tokens(text)) + " "
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO memories(text, search_text) VALUES (?, ?)",
                (text, search_text),
            )
            return int(cursor.lastrowid)

    def recall(self, query: str = "", limit: int = 5) -> list[dict[str, Any]]:
        if not isinstance(query, str):
            raise TypeError("query must be a string")
        if len(query) > MAX_QUERY_LENGTH:
            raise ValueError(f"query must be at most {MAX_QUERY_LENGTH} characters")
        limit = _integer(limit, "limit", MAX_RESULTS)
        query = query.strip()
        with self._connection() as connection:
            if not query:
                rows = connection.execute(
                    "SELECT id, text FROM memories ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                tokens = list(dict.fromkeys(_tokens(query)))[:MAX_SEARCH_TOKENS]
                if not tokens:
                    return []
                # Tokens are parameters, never SQL. Whole-token matches avoid
                # e.g. recalling "concatenate" when searching for "cat".
                terms = ["(instr(search_text, ?) > 0) * 10" for _ in tokens]
                score = " + ".join(terms) + " + (instr(search_text, ?) > 0) * 5"
                parameters = [f" {token} " for token in tokens]
                parameters.append(" " + " ".join(_tokens(query)) + " ")
                rows = connection.execute(
                    f"SELECT id, text, ({score}) AS score FROM memories "
                    "WHERE score > 0 ORDER BY score DESC, id DESC LIMIT ?",
                    (*parameters, limit),
                ).fetchall()
        return [{"id": row["id"], "text": row["text"]} for row in rows]

    def forget(self, memory_id: int) -> bool:
        memory_id = _integer(memory_id, "memory_id")
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cursor.rowcount == 1

    def save_turn(self, user_text: str, reply: str) -> None:
        user_text = _text(user_text, "user_text", MAX_HISTORY_LENGTH)
        reply = _text(reply, "reply", MAX_HISTORY_LENGTH)
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO history(user_text, reply) VALUES (?, ?)", (user_text, reply)
            )
            connection.execute(
                "DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                (MAX_HISTORY_PAIRS,),
            )

    def recent_history(self, limit: int = 6) -> list[dict[str, str]]:
        limit = _integer(limit, "limit", MAX_RESULTS)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT user_text, reply FROM history ORDER BY id DESC LIMIT ?",
                (limit // 2,),
            ).fetchall()
        messages: list[dict[str, str]] = []
        for row in reversed(rows):
            messages.extend(
                [
                    {"role": "user", "content": row["user_text"]},
                    {"role": "assistant", "content": row["reply"]},
                ]
            )
        return messages

    def add_reminder(self, text: str, due_at: str) -> dict[str, Any]:
        text = _text(text, "reminder text", MAX_FACT_LENGTH)
        due_at = _text(due_at, "due_at", 128)
        try:
            due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("due_at must be an ISO8601 timestamp with a timezone") from error
        if due.tzinfo is None or due.utcoffset() is None:
            raise ValueError("due_at must include a timezone")
        if due <= datetime.now(timezone.utc):
            raise ValueError("due_at must be in the future")
        try:
            due_at = _timestamp(due)
        except (OverflowError, ValueError) as error:
            raise ValueError("due_at must fit within the supported UTC date range") from error
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO reminders(text, due_at) VALUES (?, ?)", (text, due_at)
            )
            return {"id": int(cursor.lastrowid), "text": text, "due_at": due_at}

    def list_reminders(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, text, due_at FROM reminders WHERE delivered_at IS NULL "
                "ORDER BY due_at, id"
            ).fetchall()
        return [dict(row) for row in rows]

    def cancel_reminder(self, id: int) -> bool:
        id = _integer(id, "id")
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM reminders WHERE id = ? AND delivered_at IS NULL", (id,)
            )
            return cursor.rowcount == 1

    def due_reminders(self) -> list[dict[str, Any]]:
        now = _timestamp(datetime.now(timezone.utc))
        with self._connection() as connection:
            # Acquire the write lock before selecting, including across stores
            # and processes. Selection and claiming share one transaction.
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, text, due_at FROM reminders "
                "WHERE delivered_at IS NULL AND due_at <= ? ORDER BY due_at, id",
                (now,),
            ).fetchall()
            connection.execute(
                "UPDATE reminders SET delivered_at = ? "
                "WHERE delivered_at IS NULL AND due_at <= ?",
                (now, now),
            )
        return [dict(row) for row in rows]
