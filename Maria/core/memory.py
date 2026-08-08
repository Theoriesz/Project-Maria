"""SQLite-backed memory layer for Maria."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from logging import Logger
from pathlib import Path
from typing import Any

from core.logger import get_logger


class Memory:
    """Provide all database interactions for Maria."""

    def __init__(self, database_path: Path, logger: Logger | None = None) -> None:
        self.database_path = database_path
        self.logger = logger or get_logger(__name__)

    def initialize(self) -> None:
        """Create required database tables if they do not already exist."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS interactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_input TEXT NOT NULL,
                        response TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fact TEXT NOT NULL CHECK (length(fact) > 0),
                        normalized_fact TEXT NOT NULL UNIQUE
                            CHECK (length(normalized_fact) > 0),
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_interactions_created_at
                        ON interactions (created_at);

                    CREATE INDEX IF NOT EXISTS idx_notes_title
                        ON notes (title);
                    """
                )
            self.logger.info("Database initialized at %s", self.database_path)
        except sqlite3.Error as exc:
            self.logger.exception("Database initialization failed.")
            raise RuntimeError("Unable to initialize Maria database.") from exc

    def record_interaction(self, user_input: str, response: str) -> int:
        """Record a user interaction and return its database id."""
        timestamp = _utc_timestamp()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO interactions (user_input, response, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (user_input, response, timestamp),
                )
                interaction_id = int(cursor.lastrowid)
            self.logger.info("Recorded interaction %s.", interaction_id)
            return interaction_id
        except sqlite3.Error as exc:
            self.logger.exception("Failed to record interaction.")
            raise RuntimeError("Unable to record interaction.") from exc

    def get_recent_interactions(self, limit: int = 5) -> list[dict[str, Any]]:
        """Return recent interactions in newest-first order."""
        safe_limit = max(1, min(limit, 50))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, user_input, response, created_at
                    FROM interactions
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to fetch recent interactions.")
            raise RuntimeError("Unable to fetch recent interactions.") from exc

    def store_memory(self, fact: str) -> tuple[int, bool]:
        """Store a fact and return its database id and creation status."""
        clean_fact = fact.strip()
        if not clean_fact:
            raise ValueError("Memory fact is required.")

        normalized_fact = clean_fact.casefold()
        timestamp = _utc_timestamp()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO memories (fact, normalized_fact, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(normalized_fact) DO NOTHING
                    """,
                    (clean_fact, normalized_fact, timestamp),
                )
                created = cursor.rowcount == 1
                if created:
                    memory_id = int(cursor.lastrowid)
                else:
                    row = connection.execute(
                        """
                        SELECT id
                        FROM memories
                        WHERE normalized_fact = ?
                        """,
                        (normalized_fact,),
                    ).fetchone()
                    if row is None:
                        raise sqlite3.IntegrityError(
                            "Unable to resolve duplicate memory."
                        )
                    memory_id = int(row["id"])
            if created:
                self.logger.info("Stored memory %s.", memory_id)
            else:
                self.logger.info("Memory %s already exists.", memory_id)
            return memory_id, created
        except sqlite3.Error as exc:
            self.logger.exception("Failed to store memory.")
            raise RuntimeError("Unable to store memory.") from exc

    def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        """Return one memory by its database id."""
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT id, fact, created_at
                    FROM memories
                    WHERE id = ?
                    """,
                    (memory_id,),
                ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            self.logger.exception("Failed to read memory.")
            raise RuntimeError("Unable to read memory.") from exc

    def search_memories(
        self,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return memories containing a literal case-insensitive query."""
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("Memory search query is required.")

        normalized_query = clean_query.casefold()
        safe_limit = max(1, min(limit, 100))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, fact, created_at
                    FROM memories
                    WHERE instr(normalized_fact, ?) > 0
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (normalized_query, safe_limit),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to search memories.")
            raise RuntimeError("Unable to search memories.") from exc

    def list_memories(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return stored memories in newest-first order."""
        safe_limit = max(1, min(limit, 100))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, fact, created_at
                    FROM memories
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to list memories.")
            raise RuntimeError("Unable to list memories.") from exc

    def create_note(self, title: str, body: str) -> int:
        """Create a note and return its database id."""
        clean_title = title.strip()
        clean_body = body.strip()
        if not clean_title:
            raise ValueError("Note title is required.")
        if not clean_body:
            raise ValueError("Note body is required.")

        timestamp = _utc_timestamp()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO notes (title, body, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (clean_title, clean_body, timestamp, timestamp),
                )
                note_id = int(cursor.lastrowid)
            self.logger.info("Created note %s.", note_id)
            return note_id
        except sqlite3.Error as exc:
            self.logger.exception("Failed to create note.")
            raise RuntimeError("Unable to create note.") from exc

    def list_notes(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent notes in newest-first order."""
        safe_limit = max(1, min(limit, 100))
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    """
                    SELECT id, title, created_at, updated_at
                    FROM notes
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                    """,
                    (safe_limit,),
                ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as exc:
            self.logger.exception("Failed to list notes.")
            raise RuntimeError("Unable to list notes.") from exc

    def get_note(self, note_id: int | None = None, title: str | None = None) -> dict[str, Any] | None:
        """Return one note by id or exact title."""
        if note_id is None and not title:
            raise ValueError("A note id or title is required.")

        try:
            with self._connection() as connection:
                if note_id is not None:
                    row = connection.execute(
                        """
                        SELECT id, title, body, created_at, updated_at
                        FROM notes
                        WHERE id = ?
                        """,
                        (note_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT id, title, body, created_at, updated_at
                        FROM notes
                        WHERE title = ?
                        ORDER BY updated_at DESC, id DESC
                        LIMIT 1
                        """,
                        (title,),
                    ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as exc:
            self.logger.exception("Failed to read note.")
            raise RuntimeError("Unable to read note.") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open a SQLite connection and close it after use."""
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with row dictionaries enabled."""
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _utc_timestamp() -> str:
    """Return the current UTC timestamp as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()
