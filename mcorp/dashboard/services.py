"""Application service used by every dashboard route."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..bootstrap import build_core_services
from ..catalog import ITEM_CATALOG
from ..control import ControlError, EconomyControlService
from ..shops import LICENSE_EXPIRED, SHOP_ACTIVE
from .permissions import DashboardUser
from ..identities import DiscordIdentityStore
from .scheduler import ProductionScheduleStore


@dataclass(frozen=True)
class UnifiedAuditEvent:
    created_at: str
    event_type: str
    actor_id: str | None
    shop_id: str | None
    status: str
    metadata: dict[str, Any]
    source: str
    transaction_id: str | None = None
    user_ids: tuple[str, ...] = ()


class DashboardService:
    """Dashboard façade: routes never touch a store or SQLite directly."""

    def __init__(
        self, control: EconomyControlService, schedules: ProductionScheduleStore,
        institution_id: str, identities: DiscordIdentityStore,
    ) -> None:
        self.control = control
        self.schedules = schedules
        self.institution_id = institution_id
        self.identities = identities

    @classmethod
    def from_database(cls, database_path: Path, institution_id: str) -> "DashboardService":
        core = build_core_services(database_path)
        schedules = ProductionScheduleStore(database_path)
        schedules.initialize()
        core.control.ensure_official_shop(institution_id)
        return cls(core.control, schedules, institution_id, core.identities)

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
            "alerts": self.alerts(),
        }

    def shop_reports(self):
        return self.control.list_shop_reports(self.institution_id)

    def shop_report(self, shop_id: str):
        return self.control.shop_report(self.institution_id, shop_id)

    def license_stock_rules(self, shop_id: str) -> dict[str, int]:
        return self.control.license_stock_rules(self.institution_id, shop_id)

    def shop_detail_data(self, shop_id: str) -> dict[str, Any]:
        return {
            "report": self.shop_report(shop_id),
            "license_stock": self.license_stock_rules(shop_id),
            "supply_rows": self.shop_supply_rows(shop_id),
            "catalog": tuple(ITEM_CATALOG.values()),
        }

    def stock_data(self) -> dict[str, Any]:
        shops = {report.shop.shop_id: report.shop.display_name for report in self.shop_reports()}
        distribution = []
        for entry in self.control.inventory_distribution():
            if entry.owner.owner_type == "USER":
                display_owner = self.identities.known_nickname(entry.owner.owner_id)
                display_owner = display_owner or f"Discord {entry.owner.owner_id}"
            elif entry.owner.owner_type == "SHOP":
                display_owner = shops.get(entry.owner.owner_id, entry.owner.owner_id)
            elif entry.owner.owner_type == "SYSTEM" and entry.owner.owner_id == "M_CORP":
                display_owner = "ENTREPÔT M CORP"
            else:
                display_owner = entry.owner.owner_id
            distribution.append({
                "owner": entry.owner, "item_id": entry.item_id,
                "quantity": entry.quantity, "display_owner": display_owner,
            })
        return {
            "metrics": self.control.item_metrics(),
            "distribution": distribution,
        }

    def remember_discord_identity(self, discord_id: str, nickname: str) -> None:
        self.identities.remember_known_nickname(discord_id, nickname)

    def wholesale_items(self):
        return self.control.wholesale_catalog(self.institution_id)

    def warehouse_rows(self) -> list[dict[str, Any]]:
        metrics = {metric.item_id: metric for metric in self.control.item_metrics()}
        return [
            {
                "item": item,
                "metric": metrics.get(item.item_id),
            }
            for item in ITEM_CATALOG.values()
        ]

    def shop_supply_rows(self, shop_id: str) -> list[dict[str, Any]]:
        report = self.shop_report(shop_id)
        permissions = {
            row["item_id"]: bool(row["can_purchase"])
            for row in self.control.item_permissions(self.institution_id)
            if row["shop_id"] == shop_id
        }
        policies = {policy.item_id: policy for policy in report.restock_policies}
        wholesale = {item.item_id: item for item in self.wholesale_items()}
        rows: list[dict[str, Any]] = []
        for item in ITEM_CATALOG.values():
            policy = policies.get(item.item_id)
            state, reason = "non configuré", None
            if policy is not None:
                try:
                    self.control.calculate_restock_quote(
                        self.institution_id, shop_id, item.item_id
                    )
                    state = "prêt"
                except ControlError as exc:
                    state, reason = "bloqué", type(exc).__name__
            rows.append({
                "item": item,
                "allowed": permissions.get(item.item_id, False),
                "policy": policy,
                "stock": report.inventory.get(item.item_id, 0),
                "wholesale": wholesale.get(item.item_id),
                "state": state,
                "reason": reason,
            })
        return rows

    def wholesale_data(self, selected_shop_id: str | None = None) -> dict[str, Any]:
        reports = self.shop_reports()
        selected = next(
            (report for report in reports if report.shop.shop_id == selected_shop_id),
            reports[0] if reports else None,
        )
        return {
            "warehouse_rows": self.warehouse_rows(),
            "items": self.wholesale_items(),
            "catalog": tuple(ITEM_CATALOG.values()),
            "schedules": self.schedules.list(self.institution_id),
            "shops": [report.shop for report in reports],
            "selected_shop": None if selected is None else selected.shop,
            "supply_rows": [] if selected is None else self.shop_supply_rows(selected.shop.shop_id),
            "history": [
                event for event in self.control.audit_events(self.institution_id, limit=100)
                if event.event_type in {
                    "DASHBOARD_PRODUCTION", "SCHEDULED_PRODUCTION",
                    "WHOLESALE_PURCHASE", "WHOLESALE_PRICE_CHANGE",
                    "DASHBOARD_WHOLESALE_UPDATE",
                }
            ][:20],
        }

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

    def audit(self, **filters: Any) -> list[UnifiedAuditEvent]:
        """Merge existing control, ledger and inventory journals for read-only display."""
        events = [
            UnifiedAuditEvent(
                event.created_at, event.event_type, event.actor_id, event.shop_id,
                event.status, event.metadata, "Contrôle",
                str(event.metadata.get("transaction_id"))
                if event.metadata.get("transaction_id") is not None else None,
                tuple(str(event.metadata.get(key)) for key in ("user_id", "buyer_user_id") if event.metadata.get(key) is not None),
            )
            for event in self.control.audit_events(self.institution_id, limit=500)
        ]
        for transaction, participants in self.control.ledger.audit_transactions(
            self.institution_id, limit=500
        ):
            actor = transaction.metadata.get("administrator_id") or transaction.metadata.get("initiator_id")
            events.append(UnifiedAuditEvent(
                transaction.created_at, transaction.transaction_type,
                None if actor is None else str(actor),
                None if transaction.metadata.get("shop_id") is None else str(transaction.metadata["shop_id"]),
                transaction.status,
                {**transaction.metadata, "amount": transaction.amount, "participants": participants},
                "Banque", transaction.transaction_id, participants,
            ))
        for transaction in self.control.inventory.store.audit_transactions(limit=500):
            actor = transaction.metadata.get("administrator_id") or transaction.metadata.get("initiator_id")
            owners = tuple(
                owner.owner_id for owner in (transaction.source_owner, transaction.destination_owner)
                if owner is not None and owner.owner_type == "USER"
            )
            shop = next((
                owner.owner_id for owner in (transaction.source_owner, transaction.destination_owner)
                if owner is not None and owner.owner_type == "SHOP"
            ), None)
            events.append(UnifiedAuditEvent(
                transaction.created_at, transaction.transaction_type,
                None if actor is None else str(actor), shop, transaction.status,
                {
                    **transaction.metadata, "item_id": transaction.item_id,
                    "quantity": transaction.quantity,
                    "source": None if transaction.source_owner is None else asdict(transaction.source_owner),
                    "destination": None if transaction.destination_owner is None else asdict(transaction.destination_owner),
                },
                "Objets", transaction.transaction_id, owners,
            ))

        actor_id = filters.get("actor_id")
        shop_id = filters.get("shop_id")
        event_type = filters.get("event_type")
        event_status = filters.get("status")
        since = filters.get("since")
        user_id = filters.get("user_id")
        transaction_id = filters.get("transaction_id")
        if actor_id:
            events = [event for event in events if event.actor_id == actor_id]
        if shop_id:
            events = [event for event in events if event.shop_id == shop_id]
        if event_type:
            events = [event for event in events if event.event_type == event_type]
        if event_status:
            events = [event for event in events if event.status == event_status]
        if since:
            events = [event for event in events if event.created_at >= since]
        if user_id:
            events = [event for event in events if user_id in event.user_ids]
        if transaction_id:
            events = [event for event in events if event.transaction_id == transaction_id]
        return sorted(events, key=lambda event: event.created_at, reverse=True)[:200]

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
