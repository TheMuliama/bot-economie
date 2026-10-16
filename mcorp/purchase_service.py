"""Player-shop sales, kept deliberately small and recoverable.

The ledger, stock movement and purchase record share one SQLite database.  A
single ``BEGIN IMMEDIATE`` therefore makes a BANK sale indivisible and also
serializes competing attempts to buy the last item.  CASH first moves Red
credits to the vault; its database leg then creates exactly the same covered
liabilities.  The caller compensates that Red movement if this leg fails.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from .catalog import get_item
from .control import EconomyControlService
from .ledger import (
    ACTIVE_ACCOUNT_STATUS, CHECKING_ACCOUNT_TYPE, TREASURY_ACCOUNT_TYPE,
    TREASURY_OWNER_ID, LedgerStore,
)
from .owners import SHOP_OWNER_TYPE, SYSTEM_OWNER_TYPE, USER_OWNER_TYPE
from .shop_store import SHOP_ACTIVE, VISIBILITY_PUBLIC, ShopStore
from .sqlite import open_database


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SaleItem:
    item_id: str
    unit_price: int
    stock: int


@dataclass(frozen=True)
class PurchaseResult:
    purchase_id: str
    buyer_id: int
    shop_id: str
    item_id: str
    quantity: int
    unit_price: int
    total: int
    payment_method: str
    commission: int
    status: str


class PurchaseError(Exception):
    pass


class PurchaseUnavailable(PurchaseError):
    pass


class PurchasePriceChanged(PurchaseError):
    pass


class PurchaseInsufficientStock(PurchaseError):
    pass


class PurchasePaymentNotAllowed(PurchaseError):
    pass


class PurchaseInvalidQuantity(PurchaseError):
    pass


class PurchaseService:
    """The transactional purchase boundary used by Discord and tests."""

    def __init__(self, ledger: LedgerStore, shops: ShopStore) -> None:
        self.ledger = ledger
        self.shops = shops
        self.database_path = ledger.database_path

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS player_purchases (
                    purchase_id TEXT PRIMARY KEY,
                    institution_id TEXT NOT NULL,
                    buyer_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    unit_price INTEGER NOT NULL CHECK (unit_price > 0),
                    total INTEGER NOT NULL CHECK (total > 0),
                    payment_method TEXT NOT NULL CHECK (payment_method IN ('BANK', 'CASH')),
                    commission_amount INTEGER NOT NULL CHECK (commission_amount >= 0),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (institution_id, purchase_id)
                );
                CREATE INDEX IF NOT EXISTS idx_player_purchases_buyer_created
                    ON player_purchases(institution_id, buyer_id, created_at DESC);
                """
            )

    def accessible_shops(self, institution_id: str):
        return [
            shop for shop in self.shops.list_shops(institution_id)
            if shop.public_visibility == VISIBILITY_PUBLIC and shop.status == SHOP_ACTIVE
        ]

    def catalog(self, institution_id: str, shop_id: str) -> list[SaleItem]:
        shop = self._require_shop(institution_id, shop_id)
        if shop.status != SHOP_ACTIVE:
            raise PurchaseUnavailable(shop.status)
        items: list[SaleItem] = []
        with self._connection() as connection:
            for price in self.shops.list_sale_items(institution_id, shop_id):
                if not self.shops.item_is_allowed(institution_id, shop_id, price.item_id):
                    continue
                stock = self._stock(connection, shop_id, price.item_id)
                if stock:
                    items.append(SaleItem(price.item_id, price.unit_price, stock))
        return items

    def purchase_bank(
        self, institution_id: str, buyer_id: int, shop_id: str, item_id: str,
        quantity: int, purchase_id: str, *, expected_unit_price: int | None = None,
    ) -> PurchaseResult:
        return self._purchase(
            institution_id, buyer_id, shop_id, item_id, quantity, purchase_id, "BANK",
            expected_unit_price=expected_unit_price,
        )

    def commit_cash_purchase(
        self, institution_id: str, buyer_id: int, shop_id: str, item_id: str,
        quantity: int, purchase_id: str, *, expected_unit_price: int | None = None,
    ) -> PurchaseResult:
        """Commit CASH's DB leg after Red cash reached the vault."""
        return self._purchase(
            institution_id, buyer_id, shop_id, item_id, quantity, purchase_id, "CASH",
            expected_unit_price=expected_unit_price,
        )

    def _purchase(
        self, institution_id: str, buyer_id: int, shop_id: str, item_id: str,
        quantity: int, purchase_id: str, payment_method: str, *, expected_unit_price: int | None,
    ) -> PurchaseResult:
        if not isinstance(quantity, int) or quantity <= 0:
            raise PurchaseInvalidQuantity()
        item = get_item(item_id)
        if item is None:
            raise PurchaseUnavailable("unknown item")
        if not item.stackable and quantity != 1:
            raise PurchaseInvalidQuantity()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM player_purchases WHERE purchase_id = ?", (purchase_id,)
            ).fetchone()
            if existing is not None:
                return self._result(existing)
            shop = connection.execute(
                "SELECT * FROM shops WHERE institution_id = ? AND shop_id = ?",
                (institution_id, shop_id),
            ).fetchone()
            if shop is None or str(shop["status"]) != SHOP_ACTIVE:
                raise PurchaseUnavailable()
            methods = tuple(json.loads(str(shop["payment_methods_json"])))
            if payment_method not in methods:
                raise PurchasePaymentNotAllowed()
            price = connection.execute(
                """SELECT unit_price FROM shop_sale_items
                   WHERE institution_id = ? AND shop_id = ? AND item_id = ?""",
                (institution_id, shop_id, item.item_id),
            ).fetchone()
            allowed = connection.execute(
                """SELECT can_purchase FROM shop_item_permissions
                   WHERE institution_id = ? AND shop_id = ? AND item_id = ?""",
                (institution_id, shop_id, item.item_id),
            ).fetchone()
            if price is None or allowed is None or not bool(allowed["can_purchase"]):
                raise PurchaseUnavailable()
            unit_price = int(price["unit_price"])
            if expected_unit_price is not None and unit_price != expected_unit_price:
                raise PurchasePriceChanged()
            stock = self._stock(connection, shop_id, item.item_id)
            if stock < quantity:
                raise PurchaseInsufficientStock()
            total = unit_price * quantity
            commission = total * int(shop["commission_bps"]) // 10_000
            shop_amount = total - commission
            buyer = self._account(connection, institution_id, USER_OWNER_TYPE, str(buyer_id), CHECKING_ACCOUNT_TYPE)
            shop_account = self._account(connection, institution_id, SHOP_OWNER_TYPE, shop_id, CHECKING_ACCOUNT_TYPE)
            treasury = self._account(connection, institution_id, SYSTEM_OWNER_TYPE, TREASURY_OWNER_ID, TREASURY_ACCOUNT_TYPE)
            if payment_method == "BANK":
                if buyer is None or int(buyer["balance"]) < total:
                    raise PurchaseInsufficientFunds()
                connection.execute("UPDATE accounts SET balance = balance - ? WHERE account_id = ?", (total, buyer["account_id"]))
            if shop_account is None or treasury is None:
                raise PurchaseUnavailable("missing covered account")
            connection.execute("UPDATE accounts SET balance = balance + ? WHERE account_id = ?", (shop_amount, shop_account["account_id"]))
            if commission:
                connection.execute("UPDATE accounts SET balance = balance + ? WHERE account_id = ?", (commission, treasury["account_id"]))
            metadata = json.dumps({"purchase_id": purchase_id, "shop_id": shop_id, "item_id": item.item_id, "payment_method": payment_method}, sort_keys=True)
            transaction_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO ledger_transactions (transaction_id, institution_id, transaction_type,
                   amount, source_account_id, destination_account_id, created_at, status, metadata_json)
                   VALUES (?, ?, 'SHOP_SALE_SPLIT', ?, ?, ?, ?, 'COMPLETED', ?)""",
                (transaction_id, institution_id, total,
                 None if payment_method == "CASH" else buyer["account_id"], shop_account["account_id"], _now(), metadata),
            )
            connection.executemany("INSERT INTO transaction_participants (transaction_id, owner_id) VALUES (?, ?)",
                [(transaction_id, str(buyer_id)), (transaction_id, f"SHOP:{shop_id}"), (transaction_id, TREASURY_OWNER_ID)])
            connection.execute(
                "UPDATE inventory_items SET quantity = quantity - ?, updated_at = ? WHERE owner_type = 'SHOP' AND owner_id = ? AND item_id = ?",
                (quantity, _now(), shop_id, item.item_id),
            )
            connection.execute(
                """INSERT INTO inventory_items (owner_type, owner_id, item_id, quantity, updated_at)
                   VALUES ('USER', ?, ?, ?, ?)
                   ON CONFLICT(owner_type, owner_id, item_id) DO UPDATE SET quantity = quantity + excluded.quantity, updated_at = excluded.updated_at""",
                (str(buyer_id), item.item_id, quantity, _now()),
            )
            connection.execute(
                """INSERT INTO inventory_transactions (transaction_id, transaction_type, item_id, quantity,
                   source_owner_type, source_owner_id, destination_owner_type, destination_owner_id,
                   created_at, status, metadata_json) VALUES (?, 'ITEM_SHOP_SALE', ?, ?, 'SHOP', ?, 'USER', ?, ?, 'COMPLETED', ?)""",
                (str(uuid.uuid4()), item.item_id, quantity, shop_id, str(buyer_id), _now(), metadata),
            )
            row = (purchase_id, institution_id, str(buyer_id), shop_id, item.item_id, quantity,
                   unit_price, total, payment_method, commission, "COMPLETED", _now(), _now())
            connection.execute(
                """INSERT INTO player_purchases (purchase_id, institution_id, buyer_id, shop_id, item_id,
                   quantity, unit_price, total, payment_method, commission_amount, status, created_at, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", row,
            )
            connection.execute(
                """INSERT INTO control_events (event_id, institution_id, shop_id, event_type, created_at,
                   actor_id, status, metadata_json) VALUES (?, ?, ?, 'PLAYER_PURCHASE', ?, ?, 'COMPLETED', ?)""",
                (str(uuid.uuid4()), institution_id, shop_id, _now(), str(buyer_id), metadata),
            )
            return PurchaseResult(purchase_id, buyer_id, shop_id, item.item_id, quantity, unit_price,
                                  total, payment_method, commission, "COMPLETED")

    def _require_shop(self, institution_id: str, shop_id: str):
        shop = self.shops.get_shop(institution_id, shop_id)
        if shop is None:
            raise PurchaseUnavailable()
        return shop

    @staticmethod
    def _account(connection: sqlite3.Connection, institution_id: str, owner_type: str, owner_id: str, account_type: str):
        return connection.execute(
            """SELECT * FROM accounts WHERE institution_id = ? AND owner_type = ? AND owner_id = ?
               AND account_type = ? AND status = ?""",
            (institution_id, owner_type, owner_id, account_type, ACTIVE_ACCOUNT_STATUS),
        ).fetchone()

    @staticmethod
    def _stock(connection: sqlite3.Connection, shop_id: str, item_id: str) -> int:
        row = connection.execute(
            "SELECT quantity FROM inventory_items WHERE owner_type = 'SHOP' AND owner_id = ? AND item_id = ?",
            (shop_id, item_id),
        ).fetchone()
        return 0 if row is None else int(row["quantity"])

    @staticmethod
    def _result(row: sqlite3.Row) -> PurchaseResult:
        return PurchaseResult(str(row["purchase_id"]), int(row["buyer_id"]), str(row["shop_id"]),
            str(row["item_id"]), int(row["quantity"]), int(row["unit_price"]), int(row["total"]),
            str(row["payment_method"]), int(row["commission_amount"]), str(row["status"]))

    def _connection(self) -> sqlite3.Connection:
        return open_database(self.database_path)


class PurchaseInsufficientFunds(PurchaseError):
    pass
