"""Persistence models for shops, licences, wholesale rules and control events."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .sqlite import open_database


M_CORP_STORE_ID = "M_CORP_STORE"
SHOP_ACTIVE = "ACTIVE"
SHOP_SUSPENDED = "SUSPENDED"
SHOP_LICENSE_EXPIRED = "LICENSE_EXPIRED"
SHOP_CLOSED = "CLOSED"
LICENSE_ACTIVE = "ACTIVE"
LICENSE_EXPIRED = "EXPIRED"
LICENSE_NOT_REQUIRED = "NOT_REQUIRED"
VISIBILITY_PUBLIC = "PUBLIC"
VISIBILITY_PRIVATE = "PRIVATE"
VISIBILITY_HIDDEN = "HIDDEN"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Shop:
    institution_id: str
    shop_id: str
    display_name: str
    owner_type: str
    owner_id: str
    status: str
    created_at: str
    license_required: bool
    license_fee: int
    license_period_days: int
    license_status: str
    next_license_due_at: str | None
    commission_bps: int
    payment_methods: tuple[str, ...]
    auto_restock_allowed: bool
    public_visibility: str
    restock_fee_bps: int
    access_policy: dict[str, Any]


@dataclass(frozen=True)
class WholesaleItem:
    institution_id: str
    item_id: str
    wholesale_price: int
    available: bool
    minimum_order: int
    maximum_order: int | None


@dataclass(frozen=True)
class RestockPolicy:
    institution_id: str
    shop_id: str
    item_id: str
    enabled: bool
    threshold: int
    target_stock: int
    max_unit_price: int
    max_total_spend: int
    spent_in_period: int


@dataclass(frozen=True)
class ControlEvent:
    event_id: str
    institution_id: str
    shop_id: str | None
    event_type: str
    created_at: str
    actor_id: str | None
    status: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ReconciliationSnapshot:
    institution_id: str
    vault_balance: int
    ledger_balance: int
    difference: int
    is_reconciled: bool
    checked_at: str
    checked_by: str | None


class ShopStore:
    """SQLite repository used only through the economy control service."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shops (
                    institution_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    license_required INTEGER NOT NULL CHECK (license_required IN (0, 1)),
                    license_fee INTEGER NOT NULL CHECK (license_fee >= 0),
                    license_period_days INTEGER NOT NULL CHECK (license_period_days > 0),
                    license_status TEXT NOT NULL,
                    next_license_due_at TEXT,
                    commission_bps INTEGER NOT NULL CHECK (commission_bps BETWEEN 0 AND 10000),
                    payment_methods_json TEXT NOT NULL,
                    auto_restock_allowed INTEGER NOT NULL CHECK (auto_restock_allowed IN (0, 1)),
                    public_visibility TEXT NOT NULL,
                    restock_fee_bps INTEGER NOT NULL CHECK (restock_fee_bps BETWEEN 0 AND 10000),
                    access_policy_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (institution_id, shop_id)
                );

                CREATE TABLE IF NOT EXISTS shop_license_stock_rules (
                    institution_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    PRIMARY KEY (institution_id, shop_id, item_id),
                    FOREIGN KEY (institution_id, shop_id)
                        REFERENCES shops(institution_id, shop_id)
                );

                CREATE TABLE IF NOT EXISTS wholesale_items (
                    institution_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    wholesale_price INTEGER NOT NULL CHECK (wholesale_price >= 0),
                    available INTEGER NOT NULL CHECK (available IN (0, 1)),
                    minimum_order INTEGER NOT NULL DEFAULT 1 CHECK (minimum_order > 0),
                    maximum_order INTEGER CHECK (maximum_order IS NULL OR maximum_order > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (institution_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS wholesale_price_history (
                    change_id TEXT PRIMARY KEY,
                    institution_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    old_price INTEGER,
                    new_price INTEGER NOT NULL CHECK (new_price >= 0),
                    changed_at TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    reason TEXT
                );

                CREATE TABLE IF NOT EXISTS shop_item_permissions (
                    institution_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    can_purchase INTEGER NOT NULL CHECK (can_purchase IN (0, 1)),
                    quota_per_period INTEGER CHECK (quota_per_period IS NULL OR quota_per_period > 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (institution_id, shop_id, item_id),
                    FOREIGN KEY (institution_id, shop_id)
                        REFERENCES shops(institution_id, shop_id)
                );

                CREATE TABLE IF NOT EXISTS shop_restock_policies (
                    institution_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    threshold INTEGER NOT NULL CHECK (threshold >= 0),
                    target_stock INTEGER NOT NULL CHECK (target_stock > 0),
                    max_unit_price INTEGER NOT NULL CHECK (max_unit_price >= 0),
                    max_total_spend INTEGER NOT NULL CHECK (max_total_spend >= 0),
                    spent_in_period INTEGER NOT NULL DEFAULT 0 CHECK (spent_in_period >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (institution_id, shop_id, item_id),
                    FOREIGN KEY (institution_id, shop_id)
                        REFERENCES shops(institution_id, shop_id)
                );

                CREATE TABLE IF NOT EXISTS control_events (
                    event_id TEXT PRIMARY KEY,
                    institution_id TEXT NOT NULL,
                    shop_id TEXT,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    actor_id TEXT,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_control_events_institution_created
                    ON control_events(institution_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS reconciliation_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    institution_id TEXT NOT NULL,
                    vault_balance INTEGER NOT NULL CHECK (vault_balance >= 0),
                    ledger_balance INTEGER NOT NULL CHECK (ledger_balance >= 0),
                    difference INTEGER NOT NULL,
                    is_reconciled INTEGER NOT NULL CHECK (is_reconciled IN (0, 1)),
                    checked_at TEXT NOT NULL,
                    checked_by TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_reconciliation_institution_checked
                    ON reconciliation_snapshots(institution_id, checked_at DESC);
                """
            )

    def create_shop(
        self,
        institution_id: str,
        shop_id: str,
        display_name: str,
        owner_type: str,
        owner_id: str,
        *,
        status: str,
        license_status: str,
        next_license_due_at: str | None,
        license_required: bool = True,
        license_fee: int = 0,
        license_period_days: int = 7,
        commission_bps: int = 0,
        payment_methods: tuple[str, ...] = ("BANK",),
        auto_restock_allowed: bool = False,
        visibility: str = VISIBILITY_PRIVATE,
        restock_fee_bps: int = 0,
        access_policy: dict[str, Any] | None = None,
        actor_id: str | None = None,
    ) -> Shop:
        created_at = utc_now().isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO shops (
                    institution_id, shop_id, display_name, owner_type, owner_id, status, created_at,
                    license_required, license_fee, license_period_days, license_status,
                    next_license_due_at, commission_bps, payment_methods_json,
                    auto_restock_allowed, public_visibility, restock_fee_bps, access_policy_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    institution_id, shop_id, display_name, owner_type, owner_id, status, created_at,
                    int(license_required), license_fee, license_period_days, license_status,
                    next_license_due_at,
                    commission_bps, json.dumps(payment_methods), int(auto_restock_allowed),
                    visibility, restock_fee_bps, json.dumps(access_policy or {}, sort_keys=True),
                ),
            )
            self._insert_event(
                connection, institution_id, "SHOP_CREATED", shop_id, actor_id,
                {"display_name": display_name, "owner_type": owner_type, "owner_id": owner_id},
            )
        shop = self.get_shop(institution_id, shop_id)
        assert shop is not None
        return shop

    def get_shop(self, institution_id: str, shop_id: str) -> Shop | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM shops WHERE institution_id = ? AND shop_id = ?",
                (institution_id, shop_id),
            ).fetchone()
        return None if row is None else self._shop_from_row(row)

    def list_shops(self, institution_id: str) -> list[Shop]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM shops WHERE institution_id = ? ORDER BY shop_id", (institution_id,)
            ).fetchall()
        return [self._shop_from_row(row) for row in rows]

    def update_shop(
        self, institution_id: str, shop_id: str, *, display_name: str,
        license_required: bool, license_fee: int, license_period_days: int,
        commission_bps: int, payment_methods: tuple[str, ...],
        auto_restock_allowed: bool, visibility: str, restock_fee_bps: int,
        license_status: str, status: str, next_license_due_at: str | None,
    ) -> Shop:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE shops SET display_name = ?, license_required = ?, license_fee = ?,
                   license_period_days = ?, commission_bps = ?, payment_methods_json = ?,
                   auto_restock_allowed = ?, public_visibility = ?, restock_fee_bps = ?,
                   license_status = ?, status = ?, next_license_due_at = ?
                   WHERE institution_id = ? AND shop_id = ?""",
                (
                    display_name, int(license_required), license_fee, license_period_days,
                    commission_bps, json.dumps(payment_methods), int(auto_restock_allowed),
                    visibility, restock_fee_bps, license_status, status, next_license_due_at,
                    institution_id, shop_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ShopNotFound(shop_id)
        shop = self.get_shop(institution_id, shop_id)
        assert shop is not None
        return shop

    def set_shop_status(self, institution_id: str, shop_id: str, status: str) -> Shop:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE shops SET status = ? WHERE institution_id = ? AND shop_id = ?",
                (status, institution_id, shop_id),
            )
            if cursor.rowcount != 1:
                raise ShopNotFound(shop_id)
        shop = self.get_shop(institution_id, shop_id)
        assert shop is not None
        return shop

    def activate_license(
        self, institution_id: str, shop_id: str, next_due_at: str | None, actor_id: str
    ) -> Shop:
        with self._connection() as connection:
            connection.execute(
                """UPDATE shops SET status = ?, license_status = ?, next_license_due_at = ?
                   WHERE institution_id = ? AND shop_id = ?""",
                (SHOP_ACTIVE, LICENSE_ACTIVE, next_due_at, institution_id, shop_id),
            )
            self._insert_event(
                connection, institution_id, "SHOP_LICENSE_PAID", shop_id, actor_id,
                {"next_license_due_at": next_due_at},
            )
        shop = self.get_shop(institution_id, shop_id)
        if shop is None:
            raise ShopNotFound(shop_id)
        return shop

    def expire_license(self, institution_id: str, shop_id: str) -> Shop:
        with self._connection() as connection:
            connection.execute(
                """UPDATE shops SET status = ?, license_status = ?
                   WHERE institution_id = ? AND shop_id = ?""",
                (SHOP_LICENSE_EXPIRED, LICENSE_EXPIRED, institution_id, shop_id),
            )
            self._insert_event(
                connection, institution_id, "SHOP_LICENSE_EXPIRED", shop_id, None, {}
            )
        shop = self.get_shop(institution_id, shop_id)
        if shop is None:
            raise ShopNotFound(shop_id)
        return shop

    def set_license_stock_rule(
        self, institution_id: str, shop_id: str, item_id: str, quantity: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO shop_license_stock_rules (institution_id, shop_id, item_id, quantity)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(institution_id, shop_id, item_id) DO UPDATE SET quantity = excluded.quantity""",
                (institution_id, shop_id, item_id, quantity),
            )

    def license_stock_rules(self, institution_id: str, shop_id: str) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT item_id, quantity FROM shop_license_stock_rules
                   WHERE institution_id = ? AND shop_id = ?""",
                (institution_id, shop_id),
            ).fetchall()
        return {str(row["item_id"]): int(row["quantity"]) for row in rows}

    def set_wholesale_item(
        self, institution_id: str, item_id: str, price: int, available: bool,
        actor_id: str, reason: str | None = None, minimum_order: int = 1,
        maximum_order: int | None = None,
    ) -> WholesaleItem:
        with self._connection() as connection:
            previous = connection.execute(
                """SELECT wholesale_price FROM wholesale_items
                   WHERE institution_id = ? AND item_id = ?""",
                (institution_id, item_id),
            ).fetchone()
            old_price = None if previous is None else int(previous["wholesale_price"])
            connection.execute(
                """INSERT INTO wholesale_items (
                    institution_id, item_id, wholesale_price, available,
                    minimum_order, maximum_order, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(institution_id, item_id) DO UPDATE SET
                    wholesale_price = excluded.wholesale_price,
                    available = excluded.available,
                    minimum_order = excluded.minimum_order,
                    maximum_order = excluded.maximum_order,
                    updated_at = excluded.updated_at""",
                (institution_id, item_id, price, int(available), minimum_order, maximum_order, utc_now().isoformat()),
            )
            connection.execute(
                """INSERT INTO wholesale_price_history (
                    change_id, institution_id, item_id, old_price, new_price,
                    changed_at, actor_id, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), institution_id, item_id, old_price, price, utc_now().isoformat(), actor_id, reason),
            )
            self._insert_event(
                connection, institution_id, "WHOLESALE_PRICE_CHANGE", None, actor_id,
                {"item_id": item_id, "old_price": old_price, "new_price": price, "reason": reason},
            )
        item = self.get_wholesale_item(institution_id, item_id)
        assert item is not None
        return item

    def get_wholesale_item(self, institution_id: str, item_id: str) -> WholesaleItem | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM wholesale_items WHERE institution_id = ? AND item_id = ?",
                (institution_id, item_id),
            ).fetchone()
        return None if row is None else self._wholesale_from_row(row)

    def list_wholesale_items(self, institution_id: str) -> list[WholesaleItem]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM wholesale_items WHERE institution_id = ? ORDER BY item_id",
                (institution_id,),
            ).fetchall()
        return [self._wholesale_from_row(row) for row in rows]

    def price_history(self, institution_id: str, item_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT old_price, new_price, changed_at, actor_id, reason
                   FROM wholesale_price_history
                   WHERE institution_id = ? AND item_id = ? ORDER BY changed_at""",
                (institution_id, item_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_item_permission(
        self, institution_id: str, shop_id: str, item_id: str, allowed: bool,
        actor_id: str, quota_per_period: int | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO shop_item_permissions (
                    institution_id, shop_id, item_id, can_purchase, quota_per_period, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(institution_id, shop_id, item_id) DO UPDATE SET
                    can_purchase = excluded.can_purchase,
                    quota_per_period = excluded.quota_per_period,
                    updated_at = excluded.updated_at""",
                (institution_id, shop_id, item_id, int(allowed), quota_per_period, utc_now().isoformat()),
            )
            self._insert_event(
                connection, institution_id,
                "SHOP_ITEM_ALLOWED" if allowed else "SHOP_ITEM_DENIED",
                shop_id, actor_id, {"item_id": item_id, "quota_per_period": quota_per_period},
            )

    def item_is_allowed(self, institution_id: str, shop_id: str, item_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT can_purchase FROM shop_item_permissions
                   WHERE institution_id = ? AND shop_id = ? AND item_id = ?""",
                (institution_id, shop_id, item_id),
            ).fetchone()
        return row is not None and bool(row["can_purchase"])

    def list_item_permissions(self, institution_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT shop_id, item_id, can_purchase, quota_per_period, updated_at
                   FROM shop_item_permissions WHERE institution_id = ?
                   ORDER BY item_id, shop_id""",
                (institution_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_restock_policy(self, policy: RestockPolicy) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO shop_restock_policies (
                    institution_id, shop_id, item_id, enabled, threshold, target_stock,
                    max_unit_price, max_total_spend, spent_in_period, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(institution_id, shop_id, item_id) DO UPDATE SET
                    enabled = excluded.enabled, threshold = excluded.threshold,
                    target_stock = excluded.target_stock, max_unit_price = excluded.max_unit_price,
                    max_total_spend = excluded.max_total_spend, updated_at = excluded.updated_at""",
                (
                    policy.institution_id, policy.shop_id, policy.item_id, int(policy.enabled),
                    policy.threshold, policy.target_stock, policy.max_unit_price,
                    policy.max_total_spend, policy.spent_in_period, utc_now().isoformat(),
                ),
            )

    def get_restock_policy(
        self, institution_id: str, shop_id: str, item_id: str
    ) -> RestockPolicy | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM shop_restock_policies
                   WHERE institution_id = ? AND shop_id = ? AND item_id = ?""",
                (institution_id, shop_id, item_id),
            ).fetchone()
        return None if row is None else self._restock_from_row(row)

    def list_restock_policies(self, institution_id: str, shop_id: str) -> list[RestockPolicy]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM shop_restock_policies
                   WHERE institution_id = ? AND shop_id = ? ORDER BY item_id""",
                (institution_id, shop_id),
            ).fetchall()
        return [self._restock_from_row(row) for row in rows]

    def add_restock_spend(self, institution_id: str, shop_id: str, item_id: str, amount: int) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE shop_restock_policies SET spent_in_period = spent_in_period + ?
                   WHERE institution_id = ? AND shop_id = ? AND item_id = ?""",
                (amount, institution_id, shop_id, item_id),
            )

    def record_event(
        self, institution_id: str, event_type: str, *, shop_id: str | None = None,
        actor_id: str | None = None, metadata: dict[str, Any] | None = None,
        status: str = "COMPLETED",
    ) -> None:
        with self._connection() as connection:
            self._insert_event(
                connection, institution_id, event_type, shop_id, actor_id, metadata or {}, status
            )

    def recent_events(self, institution_id: str, limit: int = 100) -> list[ControlEvent]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM control_events WHERE institution_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (institution_id, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def find_events(
        self, institution_id: str, *, actor_id: str | None = None,
        shop_id: str | None = None, event_type: str | None = None,
        status: str | None = None, since: str | None = None,
        user_id: str | None = None, transaction_id: str | None = None,
        limit: int = 200,
    ) -> list[ControlEvent]:
        clauses = ["institution_id = ?"]
        values: list[Any] = [institution_id]
        for column, value in (
            ("actor_id", actor_id), ("shop_id", shop_id),
            ("event_type", event_type), ("status", status),
        ):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        if since:
            clauses.append("created_at >= ?")
            values.append(since)
        if user_id:
            clauses.append("metadata_json LIKE ?")
            values.append(f"%{user_id}%")
        if transaction_id:
            clauses.append("(event_id = ? OR metadata_json LIKE ?)")
            values.extend((transaction_id, f"%{transaction_id}%"))
        values.append(min(max(limit, 1), 500))
        query = (
            "SELECT * FROM control_events WHERE " + " AND ".join(clauses)
            + " ORDER BY created_at DESC LIMIT ?"
        )
        with self._connection() as connection:
            rows = connection.execute(query, values).fetchall()
        return [self._event_from_row(row) for row in rows]

    def record_reconciliation_snapshot(
        self, institution_id: str, vault_balance: int, ledger_balance: int,
        *, checked_by: str | None = None,
    ) -> ReconciliationSnapshot:
        checked_at = utc_now().isoformat()
        difference = vault_balance - ledger_balance
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO reconciliation_snapshots (
                    snapshot_id, institution_id, vault_balance, ledger_balance,
                    difference, is_reconciled, checked_at, checked_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), institution_id, vault_balance, ledger_balance,
                    difference, int(difference == 0), checked_at, checked_by,
                ),
            )
        return ReconciliationSnapshot(
            institution_id, vault_balance, ledger_balance, difference,
            difference == 0, checked_at, checked_by,
        )

    def latest_reconciliation_snapshot(
        self, institution_id: str,
    ) -> ReconciliationSnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM reconciliation_snapshots WHERE institution_id = ?
                   ORDER BY checked_at DESC LIMIT 1""",
                (institution_id,),
            ).fetchone()
        if row is None:
            return None
        return ReconciliationSnapshot(
            str(row["institution_id"]), int(row["vault_balance"]),
            int(row["ledger_balance"]), int(row["difference"]),
            bool(row["is_reconciled"]), str(row["checked_at"]), row["checked_by"],
        )

    def _connection(self) -> sqlite3.Connection:
        return open_database(self.database_path)

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection, institution_id: str, event_type: str,
        shop_id: str | None, actor_id: str | None, metadata: dict[str, Any],
        status: str = "COMPLETED",
    ) -> None:
        connection.execute(
            """INSERT INTO control_events (
                event_id, institution_id, shop_id, event_type, created_at,
                actor_id, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()), institution_id, shop_id, event_type, utc_now().isoformat(),
                actor_id, status, json.dumps(metadata, sort_keys=True),
            ),
        )

    @staticmethod
    def _shop_from_row(row: sqlite3.Row) -> Shop:
        return Shop(
            institution_id=str(row["institution_id"]), shop_id=str(row["shop_id"]),
            display_name=str(row["display_name"]), owner_type=str(row["owner_type"]),
            owner_id=str(row["owner_id"]), status=str(row["status"]),
            created_at=str(row["created_at"]), license_required=bool(row["license_required"]),
            license_fee=int(row["license_fee"]), license_period_days=int(row["license_period_days"]),
            license_status=str(row["license_status"]), next_license_due_at=row["next_license_due_at"],
            commission_bps=int(row["commission_bps"]),
            payment_methods=tuple(json.loads(str(row["payment_methods_json"]))),
            auto_restock_allowed=bool(row["auto_restock_allowed"]),
            public_visibility=str(row["public_visibility"]),
            restock_fee_bps=int(row["restock_fee_bps"]),
            access_policy=json.loads(str(row["access_policy_json"])),
        )

    @staticmethod
    def _wholesale_from_row(row: sqlite3.Row) -> WholesaleItem:
        return WholesaleItem(
            str(row["institution_id"]), str(row["item_id"]), int(row["wholesale_price"]),
            bool(row["available"]), int(row["minimum_order"]),
            None if row["maximum_order"] is None else int(row["maximum_order"]),
        )

    @staticmethod
    def _restock_from_row(row: sqlite3.Row) -> RestockPolicy:
        return RestockPolicy(
            str(row["institution_id"]), str(row["shop_id"]), str(row["item_id"]),
            bool(row["enabled"]), int(row["threshold"]), int(row["target_stock"]),
            int(row["max_unit_price"]), int(row["max_total_spend"]), int(row["spent_in_period"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> ControlEvent:
        return ControlEvent(
            str(row["event_id"]), str(row["institution_id"]), row["shop_id"],
            str(row["event_type"]), str(row["created_at"]), row["actor_id"],
            str(row["status"]), json.loads(str(row["metadata_json"])),
        )


class ShopError(Exception):
    pass


class ShopNotFound(ShopError):
    pass
