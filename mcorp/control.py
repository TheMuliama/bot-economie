"""Administrative economy API for Discord commands and a future web dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .catalog import get_item
from .inventory import (
    SHOP_OWNER_TYPE,
    SYSTEM_OWNER_TYPE,
    USER_OWNER_TYPE,
    InventoryOwner,
)
from .inventory_service import InventoryService, UnknownItem
from .ledger import LedgerStore
from .shops import (
    LICENSE_ACTIVE,
    LICENSE_NOT_REQUIRED,
    M_CORP_STORE_ID,
    SHOP_ACTIVE,
    VISIBILITY_PUBLIC,
    RestockPolicy,
    Shop,
    ShopNotFound,
    ShopStore,
    WholesaleItem,
    utc_now,
)


M_CORP_OWNER_ID = "M_CORP"


@dataclass(frozen=True)
class SaleSplit:
    gross_amount: int
    shop_amount: int
    commission_amount: int


@dataclass(frozen=True)
class RestockQuote:
    item_id: str
    quantity: int
    unit_price: int
    stock_cost: int
    fee: int
    total_cost: int


@dataclass(frozen=True)
class EconomyMetrics:
    total_cash_backed: int
    treasury_balance: int
    player_balance: int
    shop_balance: int


@dataclass(frozen=True)
class ItemMetrics:
    item_id: str
    produced_total: int
    mcorp_stock: int
    shop_stock: int
    player_stock: int


@dataclass(frozen=True)
class ShopReport:
    shop: Shop
    bank_balance: int
    inventory: dict[str, int]
    wholesale_items: tuple[str, ...]
    restock_policies: tuple[RestockPolicy, ...]


class EconomyControlService:
    """Stable business façade; callers never need direct SQLite access."""

    def __init__(
        self, ledger: LedgerStore, inventory: InventoryService, shops: ShopStore
    ) -> None:
        self.ledger = ledger
        self.inventory = inventory
        self.shops = shops

    def ensure_official_shop(self, institution_id: str) -> Shop:
        shop = self.shops.get_shop(institution_id, M_CORP_STORE_ID)
        if shop is None:
            self.ledger.get_or_create_shop_account(institution_id, M_CORP_STORE_ID)
            shop = self.shops.create_shop(
                institution_id,
                M_CORP_STORE_ID,
                "Boutique officielle M Corp",
                SYSTEM_OWNER_TYPE,
                M_CORP_OWNER_ID,
                license_required=False,
                license_fee=0,
                commission_bps=0,
                payment_methods=("BANK", "CASH"),
                auto_restock_allowed=True,
                visibility=VISIBILITY_PUBLIC,
                restock_fee_bps=0,
            )
        return shop

    def create_shop(
        self,
        institution_id: str,
        shop_id: str,
        display_name: str,
        owner_type: str,
        owner_id: str,
        *,
        administrator_id: int,
        license_required: bool = True,
        license_fee: int = 0,
        license_period_days: int = 7,
        commission_bps: int = 0,
        payment_methods: tuple[str, ...] = ("BANK",),
        auto_restock_allowed: bool = False,
        visibility: str = "PRIVATE",
        restock_fee_bps: int = 0,
        access_policy: dict[str, Any] | None = None,
    ) -> Shop:
        self._validate_shop_id(shop_id)
        self._validate_non_negative(license_fee)
        self._validate_bps(commission_bps)
        self._validate_bps(restock_fee_bps)
        if license_period_days <= 0:
            raise InvalidControlValue("license_period_days")
        if not payment_methods or not set(payment_methods) <= {"BANK", "CASH"}:
            raise InvalidControlValue("payment_methods")
        self.ledger.get_or_create_shop_account(institution_id, shop_id)
        return self.shops.create_shop(
            institution_id, shop_id, display_name, owner_type, owner_id,
            license_required=license_required, license_fee=license_fee,
            license_period_days=license_period_days, commission_bps=commission_bps,
            payment_methods=payment_methods, auto_restock_allowed=auto_restock_allowed,
            visibility=visibility, restock_fee_bps=restock_fee_bps,
            access_policy=access_policy, actor_id=str(administrator_id),
        )

    @staticmethod
    def calculate_sale_split(gross_amount: int, commission_bps: int) -> SaleSplit:
        if gross_amount <= 0:
            raise InvalidControlValue("gross_amount")
        EconomyControlService._validate_bps(commission_bps)
        commission = gross_amount * commission_bps // 10_000
        return SaleSplit(gross_amount, gross_amount - commission, commission)

    def set_license_stock_rule(
        self, institution_id: str, shop_id: str, item_id: str, quantity: int
    ) -> None:
        self._shop(institution_id, shop_id)
        self._item(item_id)
        if quantity <= 0:
            raise InvalidControlValue("quantity")
        self.shops.set_license_stock_rule(institution_id, shop_id, item_id.casefold(), quantity)

    def pay_license(
        self, institution_id: str, shop_id: str, *, administrator_id: int,
        paid_at: datetime | None = None,
    ) -> Shop:
        shop = self._shop(institution_id, shop_id)
        if not shop.license_required:
            return shop
        rules = self.shops.license_stock_rules(institution_id, shop_id)
        warehouse = self._mcorp_inventory()
        destination = self._shop_inventory(shop_id)
        for item_id, quantity in rules.items():
            if not self.shops.item_is_allowed(institution_id, shop_id, item_id):
                raise ShopItemDenied(item_id)
            available = self.inventory.get_quantity(warehouse, item_id)
            if available < quantity:
                raise WarehouseStockInsufficient(item_id, available, quantity)

        if shop.license_fee:
            self.ledger.record_shop_to_treasury(
                institution_id, shop_id, shop.license_fee,
                {"operation": "SHOP_LICENSE", "administrator_id": administrator_id},
            )

        moved: list[tuple[str, int]] = []
        try:
            for item_id, quantity in rules.items():
                self.inventory.transfer_item(
                    warehouse, destination, item_id, quantity,
                    initiator_id=administrator_id, transaction_type="ITEM_RESTOCK",
                )
                moved.append((item_id, quantity))
        except Exception as original_error:
            try:
                for item_id, quantity in reversed(moved):
                    self.inventory.transfer_item(
                        destination, warehouse, item_id, quantity,
                        initiator_id=administrator_id, transaction_type="ITEM_RESTOCK_ROLLBACK",
                    )
                if shop.license_fee:
                    self.ledger.record_treasury_to_shop(
                        institution_id, shop_id, shop.license_fee,
                        {"operation": "SHOP_LICENSE_ROLLBACK"},
                    )
            except Exception as compensation_error:
                self.shops.record_event(
                    institution_id, "SHOP_LICENSE_PAYMENT", shop_id=shop_id,
                    actor_id=str(administrator_id), status="RECOVERY_REQUIRED",
                    metadata={"error": type(original_error).__name__},
                )
                raise ControlRecoveryRequired() from compensation_error
            raise ControlOperationFailed() from original_error

        paid_at = paid_at or utc_now()
        due_at = paid_at + timedelta(days=shop.license_period_days)
        return self.shops.activate_license(
            institution_id, shop_id, due_at.isoformat(), str(administrator_id)
        )

    def refresh_license_status(
        self, institution_id: str, shop_id: str, *, now: datetime | None = None
    ) -> Shop:
        shop = self._shop(institution_id, shop_id)
        if (
            shop.license_required
            and shop.license_status == LICENSE_ACTIVE
            and shop.next_license_due_at is not None
            and datetime.fromisoformat(shop.next_license_due_at) <= (now or utc_now())
        ):
            return self.shops.expire_license(institution_id, shop_id)
        return shop

    def produce_item(
        self, institution_id: str, item_id: str, quantity: int, *, administrator_id: int
    ) -> int:
        balance = self.inventory.produce_item(
            self._mcorp_inventory(), item_id, quantity, administrator_id=administrator_id
        )
        self.shops.record_event(
            institution_id, "ITEM_PRODUCTION", actor_id=str(administrator_id),
            metadata={"item_id": item_id.casefold(), "quantity": quantity},
        )
        return balance

    def set_wholesale_item(
        self, institution_id: str, item_id: str, price: int, *, administrator_id: int,
        available: bool = True, reason: str | None = None,
        minimum_order: int = 1, maximum_order: int | None = None,
    ) -> WholesaleItem:
        self._item(item_id)
        self._validate_non_negative(price)
        if minimum_order <= 0 or (maximum_order is not None and maximum_order < minimum_order):
            raise InvalidControlValue("order_limits")
        return self.shops.set_wholesale_item(
            institution_id, item_id.casefold(), price, available, str(administrator_id),
            reason, minimum_order, maximum_order,
        )

    def set_item_permission(
        self, institution_id: str, shop_id: str, item_id: str, allowed: bool,
        *, administrator_id: int,
    ) -> None:
        self._shop(institution_id, shop_id)
        self._item(item_id)
        self.shops.set_item_permission(
            institution_id, shop_id, item_id.casefold(), allowed, str(administrator_id)
        )

    def set_restock_policy(
        self, institution_id: str, shop_id: str, item_id: str, *, enabled: bool,
        threshold: int, target_stock: int, max_unit_price: int, max_total_spend: int,
    ) -> RestockPolicy:
        shop = self._shop(institution_id, shop_id)
        self._item(item_id)
        if not shop.auto_restock_allowed:
            raise AutoRestockNotAllowed(shop_id)
        if min(threshold, max_unit_price, max_total_spend) < 0 or target_stock <= threshold:
            raise InvalidControlValue("restock_policy")
        existing = self.shops.get_restock_policy(institution_id, shop_id, item_id.casefold())
        policy = RestockPolicy(
            institution_id, shop_id, item_id.casefold(), enabled, threshold, target_stock,
            max_unit_price, max_total_spend, 0 if existing is None else existing.spent_in_period,
        )
        self.shops.set_restock_policy(policy)
        return policy

    def calculate_restock_quote(
        self, institution_id: str, shop_id: str, item_id: str
    ) -> RestockQuote:
        shop = self.refresh_license_status(institution_id, shop_id)
        if shop.status != SHOP_ACTIVE:
            raise ShopUnavailable(shop.status)
        if not shop.auto_restock_allowed:
            raise AutoRestockNotAllowed(shop_id)
        policy = self.shops.get_restock_policy(institution_id, shop_id, item_id.casefold())
        if policy is None or not policy.enabled:
            raise AutoRestockDisabled(item_id)
        current = self.inventory.get_quantity(self._shop_inventory(shop_id), item_id)
        if current > policy.threshold:
            raise RestockThresholdNotReached(current, policy.threshold)
        wholesale = self.shops.get_wholesale_item(institution_id, item_id.casefold())
        if wholesale is None or not wholesale.available:
            raise WholesaleUnavailable(item_id)
        if not self.shops.item_is_allowed(institution_id, shop_id, item_id.casefold()):
            raise ShopItemDenied(item_id)
        quantity = policy.target_stock - current
        if quantity < wholesale.minimum_order or (
            wholesale.maximum_order is not None and quantity > wholesale.maximum_order
        ):
            raise WholesaleOrderLimit()
        if wholesale.wholesale_price > policy.max_unit_price:
            raise UnitPriceLimitExceeded()
        stock_cost = quantity * wholesale.wholesale_price
        fee = (stock_cost * shop.restock_fee_bps + 9_999) // 10_000
        total = stock_cost + fee
        if policy.spent_in_period + total > policy.max_total_spend:
            raise RestockBudgetExceeded()
        if self.ledger.get_owner_balance(institution_id, SHOP_OWNER_TYPE, shop_id) < total:
            raise ShopFundsInsufficient()
        available = self.inventory.get_quantity(self._mcorp_inventory(), item_id)
        if available < quantity:
            raise WarehouseStockInsufficient(item_id, available, quantity)
        return RestockQuote(item_id.casefold(), quantity, wholesale.wholesale_price, stock_cost, fee, total)

    def restock(
        self, institution_id: str, shop_id: str, item_id: str, *, administrator_id: int
    ) -> RestockQuote:
        quote = self.calculate_restock_quote(institution_id, shop_id, item_id)
        self.ledger.record_shop_to_treasury(
            institution_id, shop_id, quote.total_cost,
            {"operation": "WHOLESALE_PURCHASE", "item_id": quote.item_id,
             "quantity": quote.quantity, "fee": quote.fee},
        )
        try:
            self.inventory.transfer_item(
                self._mcorp_inventory(), self._shop_inventory(shop_id), quote.item_id,
                quote.quantity, initiator_id=administrator_id, transaction_type="ITEM_RESTOCK",
            )
        except Exception as original_error:
            try:
                self.ledger.record_treasury_to_shop(
                    institution_id, shop_id, quote.total_cost,
                    {"operation": "WHOLESALE_PURCHASE_ROLLBACK", "item_id": quote.item_id},
                )
            except Exception as compensation_error:
                self.shops.record_event(
                    institution_id, "WHOLESALE_PURCHASE", shop_id=shop_id,
                    actor_id=str(administrator_id), status="RECOVERY_REQUIRED",
                    metadata={"item_id": quote.item_id, "total_cost": quote.total_cost},
                )
                raise ControlRecoveryRequired() from compensation_error
            raise ControlOperationFailed() from original_error
        self.shops.add_restock_spend(institution_id, shop_id, quote.item_id, quote.total_cost)
        self.shops.record_event(
            institution_id, "WHOLESALE_PURCHASE", shop_id=shop_id,
            actor_id=str(administrator_id),
            metadata={
                "item_id": quote.item_id, "quantity": quote.quantity,
                "stock_cost": quote.stock_cost, "fee": quote.fee,
                "total_cost": quote.total_cost,
            },
        )
        return quote

    def economy_metrics(self, institution_id: str) -> EconomyMetrics:
        totals = self.ledger.balance_totals_by_owner_type(institution_id)
        return EconomyMetrics(
            self.ledger.total_cash_backed_balance(institution_id),
            self.ledger.get_treasury_balance(institution_id),
            totals.get(USER_OWNER_TYPE, 0), totals.get(SHOP_OWNER_TYPE, 0),
        )

    def item_metrics(self) -> list[ItemMetrics]:
        current = self.inventory.store.totals_by_item_and_owner_type()
        produced = self.inventory.store.production_totals()
        item_ids = sorted(set(current) | set(produced))
        warehouse = self._mcorp_inventory()
        return [
            ItemMetrics(
                item_id, produced.get(item_id, 0),
                self.inventory.store.get_quantity(warehouse, item_id),
                current.get(item_id, {}).get(SHOP_OWNER_TYPE, 0),
                current.get(item_id, {}).get(USER_OWNER_TYPE, 0),
            )
            for item_id in item_ids
        ]

    def shop_report(self, institution_id: str, shop_id: str) -> ShopReport:
        shop = self.refresh_license_status(institution_id, shop_id)
        inventory = {
            entry.item_id: entry.quantity
            for entry in self.inventory.get_inventory(self._shop_inventory(shop_id))
        }
        accessible = tuple(
            item.item_id for item in self.shops.list_wholesale_items(institution_id)
            if item.available and self.shops.item_is_allowed(institution_id, shop_id, item.item_id)
        )
        return ShopReport(
            shop,
            self.ledger.get_owner_balance(institution_id, SHOP_OWNER_TYPE, shop_id),
            inventory,
            accessible,
            tuple(self.shops.list_restock_policies(institution_id, shop_id)),
        )

    def _shop(self, institution_id: str, shop_id: str) -> Shop:
        shop = self.shops.get_shop(institution_id, shop_id)
        if shop is None:
            raise ShopNotFound(shop_id)
        return shop

    @staticmethod
    def _item(item_id: str) -> None:
        if get_item(item_id) is None:
            raise UnknownItem(item_id)

    @staticmethod
    def _mcorp_inventory() -> InventoryOwner:
        return InventoryOwner(SYSTEM_OWNER_TYPE, M_CORP_OWNER_ID)

    @staticmethod
    def _shop_inventory(shop_id: str) -> InventoryOwner:
        return InventoryOwner(SHOP_OWNER_TYPE, shop_id)

    @staticmethod
    def _validate_shop_id(shop_id: str) -> None:
        if not shop_id or not shop_id.replace("_", "").isalnum() or shop_id != shop_id.upper():
            raise InvalidControlValue("shop_id")

    @staticmethod
    def _validate_non_negative(value: int) -> None:
        if not isinstance(value, int) or value < 0:
            raise InvalidControlValue("non_negative_value")

    @staticmethod
    def _validate_bps(value: int) -> None:
        if not isinstance(value, int) or not 0 <= value <= 10_000:
            raise InvalidControlValue("basis_points")


class ControlError(Exception):
    pass


class InvalidControlValue(ControlError):
    pass


class ShopUnavailable(ControlError):
    pass


class ShopItemDenied(ControlError):
    pass


class AutoRestockNotAllowed(ControlError):
    pass


class AutoRestockDisabled(ControlError):
    pass


class RestockThresholdNotReached(ControlError):
    pass


class WholesaleUnavailable(ControlError):
    pass


class WholesaleOrderLimit(ControlError):
    pass


class UnitPriceLimitExceeded(ControlError):
    pass


class RestockBudgetExceeded(ControlError):
    pass


class ShopFundsInsufficient(ControlError):
    pass


class WarehouseStockInsufficient(ControlError):
    def __init__(self, item_id: str, available: int, requested: int) -> None:
        self.item_id = item_id
        self.available = available
        self.requested = requested
        super().__init__(item_id)


class ControlOperationFailed(ControlError):
    pass


class ControlRecoveryRequired(ControlError):
    pass
