"""Minimal Discord display-name persistence for dashboard presentation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .sqlite import open_database


class DiscordIdentityStore:
    """Remember known Discord names without implementing rename-item behavior."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS discord_identities (
                    discord_id TEXT PRIMARY KEY,
                    known_nickname TEXT,
                    imposed_nickname TEXT,
                    previous_nickname TEXT,
                    rename_expires_at TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def remember_known_nickname(self, discord_id: str, nickname: str) -> None:
        clean_name = nickname.strip()
        if not discord_id or not clean_name:
            return
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO discord_identities (discord_id, known_nickname, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(discord_id) DO UPDATE SET
                       known_nickname = excluded.known_nickname,
                       updated_at = excluded.updated_at""",
                (discord_id, clean_name, datetime.now(UTC).isoformat()),
            )

    def known_nickname(self, discord_id: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT known_nickname FROM discord_identities WHERE discord_id = ?",
                (discord_id,),
            ).fetchone()
        if row is None or row["known_nickname"] is None:
            return None
        return str(row["known_nickname"])

    def _connection(self) -> sqlite3.Connection:
        return open_database(self.database_path)
