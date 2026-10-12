"""SQLite persistence for generic, multi-owner M Corp inventories."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .owners import InventoryOwner
from .sqlite import open_database

def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class InventoryEntry:
    owner: InventoryOwner
    item_id: str
    quantity: int


@dataclass(frozen=True)
class InventoryTransaction:
    transaction_id: str
    transaction_type: str
    item_id: str
    quantity: int
    source_owner: InventoryOwner | None
    destination_owner: InventoryOwner | None
    created_at: str
    status: str
    metadata: dict[str, Any]


class InventoryStore:
    """Owns inventory tables, balances and immutable movement records."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inventory_items (
                    owner_type TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_type, owner_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS inventory_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    transaction_type TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    source_owner_type TEXT,
                    source_owner_id TEXT,
                    destination_owner_type TEXT,
                    destination_owner_id TEXT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_inventory_items_owner
                    ON inventory_items(owner_type, owner_id);
                CREATE INDEX IF NOT EXISTS idx_inventory_transactions_created
                    ON inventory_transactions(created_at DESC);
                """
            )

    def get_inventory(self, owner: InventoryOwner) -> list[InventoryEntry]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT item_id, quantity FROM inventory_items
                   WHERE owner_type = ? AND owner_id = ? AND quantity > 0
                   ORDER BY item_id""",
                (owner.owner_type, owner.owner_id),
            ).fetchall()
        return [InventoryEntry(owner, str(row["item_id"]), int(row["quantity"])) for row in rows]

    def get_quantity(self, owner: InventoryOwner, item_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """SELECT quantity FROM inventory_items
                   WHERE owner_type = ? AND owner_id = ? AND item_id = ?""",
                (owner.owner_type, owner.owner_id, item_id),
            ).fetchone()
        return 0 if row is None else int(row["quantity"])

    def totals_by_item_and_owner_type(self) -> dict[str, dict[str, int]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT item_id, owner_type, SUM(quantity) AS total
                   FROM inventory_items WHERE quantity > 0
                   GROUP BY item_id, owner_type"""
            ).fetchall()
        totals: dict[str, dict[str, int]] = {}
        for row in rows:
            totals.setdefault(str(row["item_id"]), {})[str(row["owner_type"])] = int(row["total"])
        return totals

    def inventory_distribution(self) -> list[InventoryEntry]:
        """Return every positive stock position for administrative reporting."""
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT owner_type, owner_id, item_id, quantity
                   FROM inventory_items WHERE quantity > 0
                   ORDER BY item_id, owner_type, owner_id"""
            ).fetchall()
        return [
            InventoryEntry(
                InventoryOwner(str(row["owner_type"]), str(row["owner_id"])),
                str(row["item_id"]), int(row["quantity"]),
            )
            for row in rows
        ]

    def production_totals(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT item_id, SUM(quantity) AS total FROM inventory_transactions
                   WHERE transaction_type = 'ITEM_PRODUCTION' AND status = 'COMPLETED'
                   GROUP BY item_id"""
            ).fetchall()
        return {str(row["item_id"]): int(row["total"]) for row in rows}

    def add_item(
        self, owner: InventoryOwner, item_id: str, quantity: int, *, transaction_type: str,
        metadata: dict[str, Any] | None = None, max_quantity: int | None = None,
    ) -> int:
        self._require_positive_quantity(quantity)
        with self._connection() as connection:
            current = self._quantity_in_connection(connection, owner, item_id)
            if max_quantity is not None and current + quantity > max_quantity:
                raise InventoryCapacityExceeded(max_quantity)
            self._change_quantity(connection, owner, item_id, quantity)
            self._insert_transaction(
                connection, transaction_type, item_id, quantity, None, owner, metadata
            )
            return self._quantity_in_connection(connection, owner, item_id)

    def remove_item(
        self, owner: InventoryOwner, item_id: str, quantity: int, *, transaction_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        self._require_positive_quantity(quantity)
        with self._connection() as connection:
            current = self._quantity_in_connection(connection, owner, item_id)
            if current < quantity:
                raise InsufficientInventory(current, quantity)
            self._change_quantity(connection, owner, item_id, -quantity)
            self._insert_transaction(
                connection, transaction_type, item_id, quantity, owner, None, metadata
            )
            return current - quantity

    def transfer_item(
        self, source: InventoryOwner, destination: InventoryOwner, item_id: str, quantity: int,
        *, transaction_type: str = "ITEM_TRANSFER", metadata: dict[str, Any] | None = None,
        destination_max_quantity: int | None = None,
    ) -> tuple[int, int]:
        self._require_positive_quantity(quantity)
        if source == destination:
            raise SameInventoryOwner()
        with self._connection() as connection:
            source_quantity = self._quantity_in_connection(connection, source, item_id)
            if source_quantity < quantity:
                raise InsufficientInventory(source_quantity, quantity)
            destination_quantity = self._quantity_in_connection(connection, destination, item_id)
            if (
                destination_max_quantity is not None
                and destination_quantity + quantity > destination_max_quantity
            ):
                raise InventoryCapacityExceeded(destination_max_quantity)
            self._change_quantity(connection, source, item_id, -quantity)
            self._change_quantity(connection, destination, item_id, quantity)
            self._insert_transaction(
                connection, transaction_type, item_id, quantity, source, destination, metadata
            )
            return source_quantity - quantity, destination_quantity + quantity

    def recent_transactions(self, owner: InventoryOwner, limit: int = 10) -> list[InventoryTransaction]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM inventory_transactions
                   WHERE (source_owner_type = ? AND source_owner_id = ?)
                      OR (destination_owner_type = ? AND destination_owner_id = ?)
                   ORDER BY created_at DESC LIMIT ?""",
                (owner.owner_type, owner.owner_id, owner.owner_type, owner.owner_id, limit),
            ).fetchall()
        return [self._transaction_from_row(row) for row in rows]

    def audit_transactions(self, limit: int = 200) -> list[InventoryTransaction]:
        """Return recent inventory movements for the read-only administrative audit."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM inventory_transactions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._transaction_from_row(row) for row in rows]

    def _connection(self) -> sqlite3.Connection:
        return open_database(self.database_path)

    @staticmethod
    def _require_positive_quantity(quantity: int) -> None:
        if not isinstance(quantity, int) or quantity <= 0:
            raise InvalidItemQuantity()

    @staticmethod
    def _quantity_in_connection(connection: sqlite3.Connection, owner: InventoryOwner, item_id: str) -> int:
        row = connection.execute(
            """SELECT quantity FROM inventory_items
               WHERE owner_type = ? AND owner_id = ? AND item_id = ?""",
            (owner.owner_type, owner.owner_id, item_id),
        ).fetchone()
        return 0 if row is None else int(row["quantity"])

    @staticmethod
    def _change_quantity(
        connection: sqlite3.Connection, owner: InventoryOwner, item_id: str, delta: int
    ) -> None:
        if delta < 0:
            connection.execute(
                """UPDATE inventory_items SET quantity = quantity + ?, updated_at = ?
                   WHERE owner_type = ? AND owner_id = ? AND item_id = ?""",
                (delta, _utc_now(), owner.owner_type, owner.owner_id, item_id),
            )
            return
        connection.execute(
            """INSERT INTO inventory_items (owner_type, owner_id, item_id, quantity, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(owner_type, owner_id, item_id) DO UPDATE SET
                   quantity = quantity + excluded.quantity, updated_at = excluded.updated_at""",
            (owner.owner_type, owner.owner_id, item_id, delta, _utc_now()),
        )

    @staticmethod
    def _insert_transaction(
        connection: sqlite3.Connection, transaction_type: str, item_id: str, quantity: int,
        source: InventoryOwner | None, destination: InventoryOwner | None, metadata: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            """INSERT INTO inventory_transactions (
                transaction_id, transaction_type, item_id, quantity,
                source_owner_type, source_owner_id, destination_owner_type, destination_owner_id,
                created_at, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'COMPLETED', ?)""",
            (
                str(uuid.uuid4()), transaction_type, item_id, quantity,
                None if source is None else source.owner_type,
                None if source is None else source.owner_id,
                None if destination is None else destination.owner_type,
                None if destination is None else destination.owner_id,
                _utc_now(), json.dumps(metadata or {}, sort_keys=True),
            ),
        )

    @staticmethod
    def _transaction_from_row(row: sqlite3.Row) -> InventoryTransaction:
        source = None if row["source_owner_type"] is None else InventoryOwner(
            str(row["source_owner_type"]), str(row["source_owner_id"])
        )
        destination = None if row["destination_owner_type"] is None else InventoryOwner(
            str(row["destination_owner_type"]), str(row["destination_owner_id"])
        )
        return InventoryTransaction(
            str(row["transaction_id"]), str(row["transaction_type"]), str(row["item_id"]),
            int(row["quantity"]), source, destination, str(row["created_at"]),
            str(row["status"]), json.loads(str(row["metadata_json"])),
        )


class InventoryError(Exception):
    """Base class for expected inventory-domain errors."""


class InvalidItemQuantity(InventoryError):
    pass


class InsufficientInventory(InventoryError):
    def __init__(self, available: int, requested: int) -> None:
        self.available = available
        self.requested = requested
        super().__init__(f"Insufficient inventory: {requested} requested, {available} available")


class SameInventoryOwner(InventoryError):
    pass


class InventoryCapacityExceeded(InventoryError):
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        super().__init__(f"Inventory item capacity exceeded: {maximum}")
