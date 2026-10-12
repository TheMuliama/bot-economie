"""Tests for persistent weekly production scheduling."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from mcorp.control import EconomyControlService
from mcorp.dashboard.scheduler import (
    ProductionScheduleStore,
    WeeklyProductionScheduler,
    next_weekly_occurrence,
)
from mcorp.inventory_store import InventoryStore
from mcorp.inventory_service import InventoryService
from mcorp.ledger import LedgerStore
from mcorp.owners import SYSTEM_OWNER_TYPE, InventoryOwner
from mcorp.shop_store import ShopStore


INSTITUTION = "mcorp:guild:1"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)  # Monday


class DashboardSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "ledger.sqlite3"
        ledger = LedgerStore(self.path)
        ledger.initialize()
        ledger.ensure_institution(INSTITUTION, "M Corp", 999)
        ledger.mark_initialized(INSTITUTION, 999, 0)
        inventory_store = InventoryStore(self.path)
        inventory_store.initialize()
        self.inventory = InventoryService(inventory_store)
        shops = ShopStore(self.path)
        shops.initialize()
        self.control = EconomyControlService(ledger, self.inventory, shops)
        self.store = ProductionScheduleStore(self.path)
        self.store.initialize()
        self.scheduler = WeeklyProductionScheduler(self.store, self.control, INSTITUTION)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create_due_schedule(self):
        schedule = self.store.create(INSTITUTION, "immunity", 50, 0, "08:00", "42", now=NOW)
        due = NOW + timedelta(days=7)
        return schedule, due

    def test_next_occurrence_is_strictly_in_the_future(self) -> None:
        self.assertEqual(next_weekly_occurrence(0, "09:00", NOW), NOW + timedelta(days=7))

    def test_invalid_schedule_values_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create(INSTITUTION, "immunity", 0, 0, "08:00", "42", now=NOW)
        with self.assertRaises(ValueError):
            next_weekly_occurrence(8, "08:00", NOW)
        with self.assertRaises(ValueError):
            next_weekly_occurrence(0, "25:00", NOW)

    def test_schedule_creation_and_persistence(self) -> None:
        created, _ = self.create_due_schedule()
        reopened = ProductionScheduleStore(self.path)
        reopened.initialize()
        persisted = reopened.get(created.schedule_id)
        self.assertEqual((persisted.item_id, persisted.quantity), ("immunity", 50))

    def test_schedule_update_recalculates_next_run(self) -> None:
        created, _ = self.create_due_schedule()
        updated = self.store.update(
            created.schedule_id, quantity=25, weekday=2, time_utc="10:30",
            enabled=True, now=NOW,
        )
        self.assertEqual((updated.quantity, updated.weekday, updated.time_utc), (25, 2, "10:30"))
        self.assertEqual(datetime.fromisoformat(updated.next_run_at).weekday(), 2)

    def test_disabled_schedule_does_not_run(self) -> None:
        created, due = self.create_due_schedule()
        self.store.update(
            created.schedule_id, quantity=50, weekday=0, time_utc="08:00",
            enabled=False, now=NOW,
        )
        self.assertEqual(self.scheduler.run_due_once(now=due), 0)

    def test_archived_schedule_is_hidden_and_cannot_run(self) -> None:
        created, due = self.create_due_schedule()
        self.store.archive(created.schedule_id, now=NOW)
        self.assertEqual(self.store.list(INSTITUTION), [])
        self.assertEqual(self.scheduler.run_due_once(now=due), 0)

    def test_due_schedule_produces_through_control_service(self) -> None:
        _, due = self.create_due_schedule()
        self.assertEqual(self.scheduler.run_due_once(now=due), 1)
        owner = InventoryOwner(SYSTEM_OWNER_TYPE, "M_CORP")
        self.assertEqual(self.inventory.get_quantity(owner, "immunity"), 50)

    def test_success_is_journaled_in_both_domains(self) -> None:
        schedule, due = self.create_due_schedule()
        self.scheduler.run_due_once(now=due)
        self.assertEqual(self.store.runs(schedule.schedule_id)[0].status, "SUCCEEDED")
        events = self.control.audit_events(INSTITUTION, event_type="SCHEDULED_PRODUCTION")
        self.assertEqual(events[0].status, "COMPLETED")

    def test_failed_production_is_journaled_without_stock(self) -> None:
        schedule, due = self.create_due_schedule()
        with patch.object(self.control, "produce_item", side_effect=RuntimeError("boom")):
            self.assertEqual(self.scheduler.run_due_once(now=due), 0)
        run = self.store.runs(schedule.schedule_id)[0]
        self.assertEqual((run.status, run.error), ("FAILED", "RuntimeError"))
        events = self.control.audit_events(INSTITUTION, status="FAILED")
        self.assertEqual(events[0].event_type, "SCHEDULED_PRODUCTION")

    def test_same_occurrence_is_never_produced_twice(self) -> None:
        _, due = self.create_due_schedule()
        self.assertEqual(self.scheduler.run_due_once(now=due), 1)
        self.assertEqual(self.scheduler.run_due_once(now=due), 0)
        owner = InventoryOwner(SYSTEM_OWNER_TYPE, "M_CORP")
        self.assertEqual(self.inventory.get_quantity(owner, "immunity"), 50)

    def test_two_store_instances_cannot_claim_same_occurrence(self) -> None:
        _, due = self.create_due_schedule()
        second = ProductionScheduleStore(self.path)
        first_claim = self.store.claim_due(INSTITUTION, now=due)
        second_claim = second.claim_due(INSTITUTION, now=due)
        self.assertEqual((len(first_claim), len(second_claim)), (1, 0))

    def test_missed_weeks_collapse_to_one_recovery_run(self) -> None:
        _, due = self.create_due_schedule()
        restart = due + timedelta(weeks=4)
        self.assertEqual(self.scheduler.run_due_once(now=restart), 1)
        self.assertEqual(self.scheduler.run_due_once(now=restart), 0)

    def test_restart_preserves_completed_occurrence(self) -> None:
        _, due = self.create_due_schedule()
        self.scheduler.run_due_once(now=due)
        restarted = WeeklyProductionScheduler(
            ProductionScheduleStore(self.path), self.control, INSTITUTION
        )
        self.assertEqual(restarted.run_due_once(now=due), 0)

    def test_dashboard_migration_is_additive_and_idempotent(self) -> None:
        self.store.initialize()
        self.store.initialize()
        with sqlite3.connect(self.path) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
        self.assertIn("production_schedules", tables)
        self.assertIn("production_schedule_runs", tables)
        self.assertIn("ledger_transactions", tables)


if __name__ == "__main__":
    unittest.main()
