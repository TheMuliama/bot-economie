"""Application service used by every dashboard route."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..catalog import ITEM_CATALOG
from ..control import ControlError, EconomyControlService
from ..inventory import InventoryStore
from ..inventory_service import InventoryService
from ..ledger import LedgerStore
from ..shops import LICENSE_EXPIRED, SHOP_ACTIVE, ShopStore
from .permissions import DashboardUser
from .scheduler import ProductionScheduleStore


class DashboardService:
    """Dashboard façade: routes never touch a store or SQLite directly."""

    def __init__(
        self, control: EconomyControlService, schedules: ProductionScheduleStore,
        institution_id: str,
    ) -> None:
        self.control = control
        self.schedules = schedules
        self.institution_id = institution_id

    @classmethod
    def from_database(cls, database_path: Path, institution_id: str) -> "DashboardService":
        ledger = LedgerStore(database_path)
        inventory_store = InventoryStore(database_path)
        shops = ShopStore(database_path)
        schedules = ProductionScheduleStore(database_path)
        ledger.initialize()
        inventory_store.initialize()
        shops.initialize()
        schedules.initialize()
        control = EconomyControlService(ledger, InventoryService(inventory_store), shops)
        control.ensure_official_shop(institution_id)
        return cls(control, schedules, institution_id)

    def overview(self) -> dict[str, Any]:
        reports = self.control.list_shop_reports(self.institution_id)
        events = self.control.audit_events(self.institution_id, limit=500)
        return {
            "money": self.control.economy_metrics(self.institution_id),
            "reconciliation": self.control.latest_reconciliation_snapshot(self.institution_id),
            "active_shops": sum(report.shop.status == SHOP_ACTIVE for report in reports),
            "expired_licenses": sum(report.shop.license_status == LICENSE_EXPIRED for report in reports),
            "restock_errors": sum(event.event_type == "WHOLESALE_PURCHASE" and event.status != "COMPLETED" for event in events),
            "recovery_required": sum(event.status == "RECOVERY_REQUIRED" for event in events),
            "items": self.control.item_metrics(),
        }

    def shop_reports(self):
        return self.control.list_shop_reports(self.institution_id)

    def shop_report(self, shop_id: str):
        return self.control.shop_report(self.institution_id, shop_id)

    def license_stock_rules(self, shop_id: str) -> dict[str, int]:
        return self.control.license_stock_rules(self.institution_id, shop_id)

    def stock_data(self) -> dict[str, Any]:
        return {
            "metrics": self.control.item_metrics(),
            "distribution": self.control.inventory_distribution(),
        }

    def wholesale_items(self):
        return self.control.wholesale_catalog(self.institution_id)

    def permissions_matrix(self) -> dict[str, Any]:
        values = {
            (row["item_id"], row["shop_id"]): bool(row["can_purchase"])
            for row in self.control.item_permissions(self.institution_id)
        }
        return {
            "shops": [report.shop for report in self.shop_reports()],
            "items": tuple(ITEM_CATALOG.values()),
            "values": values,
        }

    def audit(self, **filters: Any):
        return self.control.audit_events(self.institution_id, **filters)

    def restock_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        wholesale = {item.item_id: item for item in self.wholesale_items()}
        for report in self.shop_reports():
            for policy in report.restock_policies:
                state = "autorisé"
                reason = None
                try:
                    self.control.calculate_restock_quote(
                        self.institution_id, report.shop.shop_id, policy.item_id
                    )
                except ControlError as exc:
                    state, reason = "bloqué", type(exc).__name__
                rows.append({
                    "shop": report.shop, "policy": policy,
                    "stock": report.inventory.get(policy.item_id, 0),
                    "wholesale": wholesale.get(policy.item_id),
                    "state": state, "reason": reason,
                })
        return rows

    def alerts(self) -> list[dict[str, str]]:
        alerts: list[dict[str, str]] = []
        snapshot = self.control.latest_reconciliation_snapshot(self.institution_id)
        if snapshot is None:
            alerts.append({"severity": "WARNING", "kind": "RECONCILIATION_UNKNOWN", "message": "Aucun snapshot Red Bank récent."})
        elif not snapshot.is_reconciled:
            alerts.append({"severity": "CRITICAL", "kind": "VAULT_MISMATCH", "message": f"Écart coffre/ledger : {snapshot.difference:+d}."})
        for report in self.shop_reports():
            if report.shop.license_status == LICENSE_EXPIRED:
                alerts.append({"severity": "WARNING", "kind": "LICENSE_EXPIRED", "message": report.shop.display_name})
        metrics = {metric.item_id: metric for metric in self.control.item_metrics()}
        for item_id in ITEM_CATALOG:
            stock = 0 if item_id not in metrics else metrics[item_id].mcorp_stock
            if stock < 5:
                alerts.append({"severity": "WARNING", "kind": "LOW_MCORP_STOCK", "message": f"{item_id}: {stock}"})
        for row in self.restock_rows():
            if row["state"] == "bloqué":
                alerts.append({"severity": "WARNING", "kind": "RESTOCK_BLOCKED", "message": f"{row['shop'].shop_id}/{row['policy'].item_id}: {row['reason']}"})
        for event in self.audit(status="RECOVERY_REQUIRED", limit=100):
            alerts.append({"severity": "CRITICAL", "kind": "RECOVERY_REQUIRED", "message": event.event_type})
        return alerts

    def produce(self, user: DashboardUser, item_id: str, quantity: int, reason: str) -> int:
        old_value = next((m.mcorp_stock for m in self.control.item_metrics() if m.item_id == item_id.casefold()), 0)
        balance = self.control.produce_item(
            self.institution_id, item_id, quantity, administrator_id=int(user.discord_id)
        )
        self._audit(user, "DASHBOARD_PRODUCTION", item_id, old_value, balance, reason)
        return balance

    def create_shop(
        self, user: DashboardUser, shop_id: str, display_name: str,
        owner_type: str, owner_id: str, reason: str,
    ):
        shop = self.control.create_shop(
            self.institution_id, shop_id, display_name, owner_type, owner_id,
            administrator_id=int(user.discord_id),
        )
        self._audit(user, "DASHBOARD_SHOP_CREATED", shop_id, None, asdict(shop), reason, shop_id=shop_id)
        return shop

    def configure_shop(self, user: DashboardUser, shop_id: str, reason: str, **values: Any):
        previous = self.shop_report(shop_id).shop
        updated = self.control.configure_shop(
            self.institution_id, shop_id, administrator_id=int(user.discord_id), **values
        )
        self._audit(user, "DASHBOARD_SHOP_CONFIGURED", shop_id, asdict(previous), asdict(updated), reason, shop_id=shop_id)
        return updated

    def set_wholesale(
        self, user: DashboardUser, item_id: str, price: int, available: bool,
        minimum_order: int, maximum_order: int | None, reason: str,
    ):
        previous = next((item for item in self.wholesale_items() if item.item_id == item_id.casefold()), None)
        result = self.control.set_wholesale_item(
            self.institution_id, item_id, price, administrator_id=int(user.discord_id),
            available=available, reason=reason, minimum_order=minimum_order,
            maximum_order=maximum_order,
        )
        self._audit(user, "DASHBOARD_WHOLESALE_UPDATE", item_id, None if previous is None else asdict(previous), asdict(result), reason)
        return result

    def set_permission(self, user: DashboardUser, shop_id: str, item_id: str, allowed: bool, reason: str) -> None:
        old = next((bool(row["can_purchase"]) for row in self.control.item_permissions(self.institution_id) if row["shop_id"] == shop_id and row["item_id"] == item_id.casefold()), False)
        self.control.set_item_permission(
            self.institution_id, shop_id, item_id, allowed,
            administrator_id=int(user.discord_id),
        )
        self._audit(user, "DASHBOARD_ITEM_PERMISSION", f"{shop_id}/{item_id}", old, allowed, reason, shop_id=shop_id)

    def set_restock_policy(self, user: DashboardUser, shop_id: str, item_id: str, **values: Any):
        old = next((asdict(row["policy"]) for row in self.restock_rows() if row["shop"].shop_id == shop_id and row["policy"].item_id == item_id.casefold()), None)
        reason = str(values.pop("reason", ""))
        policy = self.control.set_restock_policy(self.institution_id, shop_id, item_id, **values)
        self._audit(user, "DASHBOARD_RESTOCK_POLICY", f"{shop_id}/{item_id}", old, asdict(policy), reason, shop_id=shop_id)
        return policy

    def suspend_shop(self, user: DashboardUser, shop_id: str, suspended: bool, reason: str):
        return self.control.set_shop_suspended(
            self.institution_id, shop_id, suspended,
            actor_id=int(user.discord_id), actor_role=user.role.value, reason=reason,
        )

    def pay_license(self, user: DashboardUser, shop_id: str, reason: str):
        previous = self.shop_report(shop_id).shop
        result = self.control.pay_license(
            self.institution_id, shop_id, administrator_id=int(user.discord_id)
        )
        self._audit(user, "DASHBOARD_LICENSE_PAYMENT", shop_id, asdict(previous), asdict(result), reason, shop_id=shop_id)
        return result

    def create_schedule(
        self, user: DashboardUser, item_id: str, quantity: int,
        weekday: int, time_utc: str, reason: str,
    ):
        if item_id.casefold() not in ITEM_CATALOG:
            raise ValueError("Objet inconnu")
        schedule = self.schedules.create(
            self.institution_id, item_id, quantity, weekday, time_utc, user.discord_id
        )
        self._audit(user, "PRODUCTION_SCHEDULE_CREATED", schedule.schedule_id, None, asdict(schedule), reason)
        return schedule

    def update_schedule(
        self, user: DashboardUser, schedule_id: str, *, quantity: int,
        weekday: int, time_utc: str, enabled: bool, reason: str,
    ):
        previous = self.schedules.get(schedule_id)
        updated = self.schedules.update(
            schedule_id, quantity=quantity, weekday=weekday,
            time_utc=time_utc, enabled=enabled,
        )
        self._audit(user, "PRODUCTION_SCHEDULE_UPDATED", schedule_id, None if previous is None else asdict(previous), asdict(updated), reason)
        return updated

    def archive_schedule(self, user: DashboardUser, schedule_id: str, reason: str) -> None:
        previous = self.schedules.get(schedule_id)
        self.schedules.archive(schedule_id)
        self._audit(user, "PRODUCTION_SCHEDULE_ARCHIVED", schedule_id, None if previous is None else asdict(previous), "ARCHIVED", reason)

    def _audit(
        self, user: DashboardUser, action: str, target: str,
        old_value: Any, new_value: Any, reason: str,
        *, shop_id: str | None = None,
    ) -> None:
        self.control.record_admin_action(
            self.institution_id, action, user.discord_id, user.role.value,
            target, old_value, new_value, reason, shop_id=shop_id,
        )
