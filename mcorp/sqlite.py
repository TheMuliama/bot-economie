"""Shared SQLite connection settings for M Corp stores."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SQLITE_TIMEOUT_SECONDS = 10.0


def open_database(database_path: Path) -> sqlite3.Connection:
    """Open one SQLite connection with the settings required by every store."""
    connection = sqlite3.connect(database_path, timeout=SQLITE_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
