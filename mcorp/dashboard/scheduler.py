"""Persistent, weekly-only production scheduler.

SQLite is used here as a scheduler repository.  Economy mutations still go
through :class:`EconomyControlService`; this module never edits inventory.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from ..control import EconomyControlService
from ..sqlite import open_database


@dataclass(frozen=True)
class ProductionSchedule:
    schedule_id: str
    institution_id: str
    item_id: str
    quantity: int
    weekday: int
    time_utc: str
    enabled: bool
    created_by: str
    created_at: str
    last_run_at: str | None
    next_run_at: str
    archived_at: str | None


@dataclass(frozen=True)
class ScheduleRun:
    execution_id: str
    schedule_id: str
    scheduled_for: str
    started_at: str
    finished_at: str | None
    status: str
    error: str | None


def next_weekly_occurrence(weekday: int, time_utc: str, after: datetime) -> datetime:
    if not 0 <= weekday <= 6:
        raise ValueError("weekday must be between 0 and 6")
    try:
        hour, minute = (int(part) for part in time_utc.split(":"))
        candidate = after.astimezone(UTC).replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (ValueError, TypeError) as exc:
        raise ValueError("time_utc must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("time_utc must use HH:MM")
    candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
    if candidate <= after:
        candidate += timedelta(days=7)
    return candidate


class ProductionScheduleStore:
    """Additive SQLite persistence and atomic occurrence claims."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS production_schedules (
                    schedule_id TEXT PRIMARY KEY,
                    institution_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
                    time_utc TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT,
                    next_run_at TEXT NOT NULL,
                    archived_at TEXT
                );

                CREATE TABLE IF NOT EXISTS production_schedule_runs (
                    execution_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL REFERENCES production_schedules(schedule_id),
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    UNIQUE (schedule_id, scheduled_for)
                );

                CREATE INDEX IF NOT EXISTS idx_production_schedules_due
                    ON production_schedules(institution_id, enabled, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_production_runs_schedule
                    ON production_schedule_runs(schedule_id, scheduled_for DESC);
                """
            )

    def create(
        self, institution_id: str, item_id: str, quantity: int, weekday: int,
        time_utc: str, created_by: str, *, now: datetime | None = None,
    ) -> ProductionSchedule:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        next_run = next_weekly_occurrence(weekday, time_utc, now)
        schedule_id = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO production_schedules (
                    schedule_id, institution_id, item_id, quantity, weekday, time_utc,
                    enabled, created_by, created_at, next_run_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    schedule_id, institution_id, item_id.casefold(), quantity, weekday,
                    time_utc, created_by, now.isoformat(), next_run.isoformat(),
                ),
            )
        schedule = self.get(schedule_id)
        assert schedule is not None
        return schedule

    def get(self, schedule_id: str) -> ProductionSchedule | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM production_schedules WHERE schedule_id = ?", (schedule_id,)
            ).fetchone()
        return None if row is None else self._schedule(row)

    def list(self, institution_id: str, *, include_archived: bool = False) -> list[ProductionSchedule]:
        suffix = "" if include_archived else " AND archived_at IS NULL"
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM production_schedules WHERE institution_id = ?" + suffix
                + " ORDER BY next_run_at",
                (institution_id,),
            ).fetchall()
        return [self._schedule(row) for row in rows]

    def update(
        self, schedule_id: str, *, quantity: int, weekday: int, time_utc: str,
        enabled: bool, now: datetime | None = None,
    ) -> ProductionSchedule:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        next_run = next_weekly_occurrence(weekday, time_utc, now)
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE production_schedules SET quantity = ?, weekday = ?, time_utc = ?,
                   enabled = ?, next_run_at = ?
                   WHERE schedule_id = ? AND archived_at IS NULL""",
                (quantity, weekday, time_utc, int(enabled), next_run.isoformat(), schedule_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(schedule_id)
        schedule = self.get(schedule_id)
        assert schedule is not None
        return schedule

    def archive(self, schedule_id: str, *, now: datetime | None = None) -> None:
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE production_schedules SET enabled = 0, archived_at = ?
                   WHERE schedule_id = ? AND archived_at IS NULL""",
                (timestamp, schedule_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(schedule_id)

    def claim_due(self, institution_id: str, *, now: datetime | None = None) -> list[tuple[ProductionSchedule, ScheduleRun]]:
        """Atomically claim at most one overdue occurrence per schedule.

        Advancing ``next_run_at`` in the same immediate transaction makes this
        safe across processes.  The unique constraint is a second guard.
        """
        now = (now or datetime.now(UTC)).astimezone(UTC)
        claimed: list[tuple[ProductionSchedule, ScheduleRun]] = []
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM production_schedules
                   WHERE institution_id = ? AND enabled = 1 AND archived_at IS NULL
                     AND next_run_at <= ? ORDER BY next_run_at""",
                (institution_id, now.isoformat()),
            ).fetchall()
            for row in rows:
                schedule = self._schedule(row)
                scheduled_for = schedule.next_run_at
                execution_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{schedule.schedule_id}:{scheduled_for}"))
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO production_schedule_runs (
                        execution_id, schedule_id, scheduled_for, started_at, status
                    ) VALUES (?, ?, ?, ?, 'RUNNING')""",
                    (execution_id, schedule.schedule_id, scheduled_for, now.isoformat()),
                )
                future = next_weekly_occurrence(schedule.weekday, schedule.time_utc, now)
                connection.execute(
                    "UPDATE production_schedules SET next_run_at = ? WHERE schedule_id = ?",
                    (future.isoformat(), schedule.schedule_id),
                )
                if cursor.rowcount:
                    claimed.append((schedule, ScheduleRun(
                        execution_id, schedule.schedule_id, scheduled_for,
                        now.isoformat(), None, "RUNNING", None,
                    )))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return claimed

    def finish(self, run: ScheduleRun, status: str, *, error: str | None = None, now: datetime | None = None) -> None:
        if status not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("invalid run status")
        finished = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self._connection() as connection:
            connection.execute(
                """UPDATE production_schedule_runs SET finished_at = ?, status = ?, error = ?
                   WHERE execution_id = ? AND status = 'RUNNING'""",
                (finished, status, error, run.execution_id),
            )
            if status == "SUCCEEDED":
                connection.execute(
                    "UPDATE production_schedules SET last_run_at = ? WHERE schedule_id = ?",
                    (finished, run.schedule_id),
                )

    def runs(self, schedule_id: str) -> list[ScheduleRun]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM production_schedule_runs WHERE schedule_id = ?
                   ORDER BY scheduled_for DESC""", (schedule_id,)
            ).fetchall()
        return [self._run(row) for row in rows]

    def _connection(self) -> sqlite3.Connection:
        return open_database(self.database_path)

    @staticmethod
    def _schedule(row: sqlite3.Row) -> ProductionSchedule:
        return ProductionSchedule(
            str(row["schedule_id"]), str(row["institution_id"]), str(row["item_id"]),
            int(row["quantity"]), int(row["weekday"]), str(row["time_utc"]),
            bool(row["enabled"]), str(row["created_by"]), str(row["created_at"]),
            row["last_run_at"], str(row["next_run_at"]), row["archived_at"],
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> ScheduleRun:
        return ScheduleRun(
            str(row["execution_id"]), str(row["schedule_id"]), str(row["scheduled_for"]),
            str(row["started_at"]), row["finished_at"], str(row["status"]), row["error"],
        )


class WeeklyProductionScheduler:
    def __init__(
        self, store: ProductionScheduleStore, control: EconomyControlService,
        institution_id: str, *, error_hook: Callable[[Exception], None] | None = None,
    ) -> None:
        self.store = store
        self.control = control
        self.institution_id = institution_id
        self.error_hook = error_hook

    def run_due_once(self, *, now: datetime | None = None) -> int:
        completed = 0
        for schedule, run in self.store.claim_due(self.institution_id, now=now):
            try:
                self.control.produce_item(
                    self.institution_id, schedule.item_id, schedule.quantity,
                    administrator_id=int(schedule.created_by),
                )
            except Exception as exc:
                self.store.finish(run, "FAILED", error=type(exc).__name__, now=now)
                self.control.record_admin_action(
                    self.institution_id, "SCHEDULED_PRODUCTION", schedule.created_by,
                    "ADMIN", schedule.item_id, None, schedule.quantity,
                    f"schedule:{schedule.schedule_id}", status="FAILED",
                )
                if self.error_hook:
                    self.error_hook(exc)
            else:
                self.store.finish(run, "SUCCEEDED", now=now)
                self.control.record_admin_action(
                    self.institution_id, "SCHEDULED_PRODUCTION", schedule.created_by,
                    "ADMIN", schedule.item_id, None, schedule.quantity,
                    f"schedule:{schedule.schedule_id}",
                )
                completed += 1
        return completed
