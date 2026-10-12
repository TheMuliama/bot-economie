"""Tests for the Phase 3.5 shop economy control plane."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from mcorp.control import (
    AutoRestockDisabled,
    EconomyControlService,
    RestockBudgetExceeded,
    RestockThresholdNotReached,
    ShopFundsInsufficient,
    ShopAlreadyExists,
    ShopItemDenied,
    UnitPriceLimitExceeded,
    WarehouseStockInsufficient,
)
from mcorp.inventory_store import InventoryStore
from mcorp.inventory_service import InventoryService
from mcorp.ledger import LedgerStore
from mcorp.owners import SHOP_OWNER_TYPE, SYSTEM_OWNER_TYPE, InventoryOwner
from mcorp.shops import (
    LICENSE_EXPIRED,
    LICENSE_NOT_REQUIRED,
    M_CORP_STORE_ID,
    SHOP_ACTIVE,
    SHOP_LICENSE_EXPIRED,
    RestockPolicy,
    ShopStore,
    utc_now,
)


INSTITUTION = "mcorp:guild:1"
SHOP_ID = "LUCAS_STORE"


class EconomyControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "ledger.sqlite3"
        self.ledger = LedgerStore(self.path)
        self.ledger.initialize()
        self.ledger.ensure_institution(INSTITUTION, "M Corp", 999)
        self.ledger.mark_initialized(INSTITUTION, 999, 0)
        self.inventory_store = InventoryStore(self.path)
        self.inventory_store.initialize()
        self.inventory = InventoryService(self.inventory_store)
        self.shop_store = ShopStore(self.path)
        self.shop_store.initialize()
        self.control = EconomyControlService(self.ledger, self.inventory, self.shop_store)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def create_shop(
        self, *, fee: int = 0, commission_bps: int = 1_000,
        auto_restock: bool = True, restock_fee_bps: int = 500,
    ):
        return self.control.create_shop(
            INSTITUTION, SHOP_ID, "Boutique Lucas", "USER", "1",
            administrator_id=99, license_required=True, license_fee=fee,
            commission_bps=commission_bps, auto_restock_allowed=auto_restock,
            restock_fee_bps=restock_fee_bps,
        )

    def fund_shop(self, amount: int) -> None:
        self.ledger.record_deposit(INSTITUTION, 1, amount)
        self.ledger.record_player_to_treasury(INSTITUTION, 1, amount)
        self.ledger.record_treasury_to_shop(INSTITUTION, SHOP_ID, amount)

    def configure_restock(
        self, *, price: int = 100, threshold: int = 2, target: int = 10,
        max_price: int = 200, budget: int = 2_000,
    ) -> None:
        self.control.set_wholesale_item(INSTITUTION, "immunity", price, administrator_id=99)
        self.control.set_item_permission(INSTITUTION, SHOP_ID, "immunity", True, administrator_id=99)
        self.control.set_restock_policy(
            INSTITUTION, SHOP_ID, "immunity", enabled=True, threshold=threshold,
            target_stock=target, max_unit_price=max_price, max_total_spend=budget,
        )

    def test_shop_creation_and_persistence(self) -> None:
        created = self.create_shop()
        reopened = ShopStore(self.path)
        reopened.initialize()
        persisted = reopened.get_shop(INSTITUTION, SHOP_ID)
        self.assertEqual(created.shop_id, SHOP_ID)
        self.assertEqual(persisted.display_name, "Boutique Lucas")
        self.assertEqual(persisted.status, SHOP_LICENSE_EXPIRED)

    def test_duplicate_shop_id_is_refused_cleanly(self) -> None:
        self.create_shop()
        with self.assertRaises(ShopAlreadyExists):
            self.create_shop()

    def test_shop_has_covered_checking_account(self) -> None:
        self.create_shop()
        account = self.ledger.get_or_create_shop_account(INSTITUTION, SHOP_ID)
        self.assertEqual(account.owner_type, SHOP_OWNER_TYPE)
        self.assertTrue(account.is_cash_backed)

    def test_shop_inventory_is_generic_inventory(self) -> None:
        self.create_shop()
        owner = InventoryOwner(SHOP_OWNER_TYPE, SHOP_ID)
        self.inventory.add_item(owner, "maro", 2, administrator_id=99)
        self.assertEqual(self.inventory.get_quantity(owner, "maro"), 2)

    def test_official_mcorp_shop_uses_same_model(self) -> None:
        shop = self.control.ensure_official_shop(INSTITUTION)
        self.assertEqual(shop.shop_id, M_CORP_STORE_ID)
        self.assertFalse(shop.license_required)
        self.assertEqual(shop.license_status, LICENSE_NOT_REQUIRED)
        self.assertEqual(shop.commission_bps, 0)
        self.assertEqual(shop.restock_fee_bps, 0)

    def test_free_required_license_can_be_activated(self) -> None:
        self.create_shop(fee=0)
        shop = self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.assertEqual(shop.status, SHOP_ACTIVE)
        self.assertIsNotNone(shop.next_license_due_at)

    def test_paid_license_moves_covered_money_to_treasury(self) -> None:
        self.create_shop(fee=200)
        self.fund_shop(500)
        total_before = self.ledger.total_cash_backed_balance(INSTITUTION)
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.assertEqual(self.ledger.get_owner_balance(INSTITUTION, SHOP_OWNER_TYPE, SHOP_ID), 300)
        self.assertEqual(self.ledger.get_treasury_balance(INSTITUTION), 200)
        self.assertEqual(self.ledger.total_cash_backed_balance(INSTITUTION), total_before)

    def test_shop_funding_uses_existing_treasury_and_preserves_total(self) -> None:
        self.create_shop()
        self.ledger.record_deposit(INSTITUTION, 1, 500)
        self.ledger.record_player_to_treasury(INSTITUTION, 1, 500)
        total_before = self.ledger.total_cash_backed_balance(INSTITUTION)

        treasury, shop = self.control.fund_shop(
            INSTITUTION, SHOP_ID, 200, administrator_id=99
        )

        self.assertEqual((treasury, shop), (300, 200))
        self.assertEqual(self.ledger.total_cash_backed_balance(INSTITUTION), total_before)

    def test_license_expiration_transition(self) -> None:
        self.create_shop()
        self.control.pay_license(
            INSTITUTION, SHOP_ID, administrator_id=99, paid_at=utc_now() - timedelta(days=8)
        )
        shop = self.control.refresh_license_status(INSTITUTION, SHOP_ID)
        self.assertEqual(shop.license_status, LICENSE_EXPIRED)
        self.assertEqual(shop.status, SHOP_LICENSE_EXPIRED)

    def test_license_stock_comes_from_mcorp_warehouse(self) -> None:
        self.create_shop()
        self.control.set_item_permission(INSTITUTION, SHOP_ID, "immunity", True, administrator_id=99)
        self.control.set_license_stock_rule(INSTITUTION, SHOP_ID, "immunity", 5)
        self.control.produce_item(INSTITUTION, "immunity", 10, administrator_id=99)
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.assertEqual(self.inventory.get_quantity(InventoryOwner(SYSTEM_OWNER_TYPE, "M_CORP"), "immunity"), 5)
        self.assertEqual(self.inventory.get_quantity(InventoryOwner(SHOP_OWNER_TYPE, SHOP_ID), "immunity"), 5)

    def test_license_stock_requires_permission(self) -> None:
        self.create_shop()
        self.control.set_license_stock_rule(INSTITUTION, SHOP_ID, "immunity", 1)
        self.control.produce_item(INSTITUTION, "immunity", 1, administrator_id=99)
        with self.assertRaises(ShopItemDenied):
            self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)

    def test_production_is_journaled_separately_from_admin_grant(self) -> None:
        self.control.produce_item(INSTITUTION, "ticket_vote", 10, administrator_id=99)
        transactions = self.inventory_store.recent_transactions(
            InventoryOwner(SYSTEM_OWNER_TYPE, "M_CORP")
        )
        self.assertEqual(transactions[0].transaction_type, "ITEM_PRODUCTION")
        self.assertEqual(self.inventory_store.production_totals()["ticket_vote"], 10)

    def test_wholesale_price_and_history(self) -> None:
        self.control.set_wholesale_item(INSTITUTION, "immunity", 1_500, administrator_id=99)
        self.control.set_wholesale_item(
            INSTITUTION, "immunity", 1_700, administrator_id=99, reason="régulation"
        )
        item = self.shop_store.get_wholesale_item(INSTITUTION, "immunity")
        history = self.shop_store.price_history(INSTITUTION, "immunity")
        self.assertEqual(item.wholesale_price, 1_700)
        self.assertEqual([(row["old_price"], row["new_price"]) for row in history], [(None, 1_500), (1_500, 1_700)])

    def test_shop_item_permission_can_allow_and_deny(self) -> None:
        self.create_shop()
        self.control.set_item_permission(INSTITUTION, SHOP_ID, "immunity", True, administrator_id=99)
        self.assertTrue(self.shop_store.item_is_allowed(INSTITUTION, SHOP_ID, "immunity"))
        self.control.set_item_permission(INSTITUTION, SHOP_ID, "immunity", False, administrator_id=99)
        self.assertFalse(self.shop_store.item_is_allowed(INSTITUTION, SHOP_ID, "immunity"))

    def test_sale_split_uses_integer_basis_points(self) -> None:
        split = self.control.calculate_sale_split(1_000, 1_000)
        self.assertEqual((split.shop_amount, split.commission_amount), (900, 100))

    def test_sale_split_is_atomic_and_preserves_covered_total(self) -> None:
        self.create_shop(commission_bps=1_000)
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.ledger.record_deposit(INSTITUTION, 42, 1_000)
        total_before = self.ledger.total_cash_backed_balance(INSTITUTION)

        split, balances = self.control.apply_sale_split(
            INSTITUTION, SHOP_ID, 42, 1_000, reference="future-order"
        )

        self.assertEqual((split.shop_amount, split.commission_amount), (900, 100))
        self.assertEqual(balances, (0, 900, 100))
        self.assertEqual(self.ledger.total_cash_backed_balance(INSTITUTION), total_before)

    def test_restock_fee_and_total_cost(self) -> None:
        self.create_shop(restock_fee_bps=500)
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.fund_shop(2_000)
        self.control.produce_item(INSTITUTION, "immunity", 20, administrator_id=99)
        self.configure_restock(price=100)
        quote = self.control.calculate_restock_quote(INSTITUTION, SHOP_ID, "immunity")
        self.assertEqual((quote.stock_cost, quote.fee, quote.total_cost), (1_000, 50, 1_050))

    def test_disabled_auto_restock_is_refused(self) -> None:
        self.create_shop()
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.configure_restock()
        policy = self.shop_store.get_restock_policy(INSTITUTION, SHOP_ID, "immunity")
        assert policy is not None
        disabled = RestockPolicy(
            policy.institution_id, policy.shop_id, policy.item_id, False, policy.threshold,
            policy.target_stock, policy.max_unit_price, policy.max_total_spend, policy.spent_in_period,
        )
        self.shop_store.set_restock_policy(disabled)
        with self.assertRaises(AutoRestockDisabled):
            self.control.calculate_restock_quote(INSTITUTION, SHOP_ID, "immunity")

    def test_restock_threshold_prevents_unneeded_purchase(self) -> None:
        self.create_shop()
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.configure_restock()
        self.inventory.add_item(InventoryOwner(SHOP_OWNER_TYPE, SHOP_ID), "immunity", 3, administrator_id=99)
        with self.assertRaises(RestockThresholdNotReached):
            self.control.calculate_restock_quote(INSTITUTION, SHOP_ID, "immunity")

    def test_restock_respects_max_unit_price(self) -> None:
        self.create_shop()
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.fund_shop(5_000)
        self.control.produce_item(INSTITUTION, "immunity", 10, administrator_id=99)
        self.configure_restock(price=300, max_price=200, budget=5_000)
        with self.assertRaises(UnitPriceLimitExceeded):
            self.control.calculate_restock_quote(INSTITUTION, SHOP_ID, "immunity")

    def test_restock_respects_period_budget(self) -> None:
        self.create_shop()
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.fund_shop(5_000)
        self.control.produce_item(INSTITUTION, "immunity", 10, administrator_id=99)
        self.configure_restock(price=100, budget=500)
        with self.assertRaises(RestockBudgetExceeded):
            self.control.calculate_restock_quote(INSTITUTION, SHOP_ID, "immunity")

    def test_restock_requires_shop_funds(self) -> None:
        self.create_shop()
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.control.produce_item(INSTITUTION, "immunity", 10, administrator_id=99)
        self.configure_restock()
        with self.assertRaises(ShopFundsInsufficient):
            self.control.calculate_restock_quote(INSTITUTION, SHOP_ID, "immunity")

    def test_restock_requires_mcorp_stock(self) -> None:
        self.create_shop()
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.fund_shop(2_000)
        self.configure_restock()
        with self.assertRaises(WarehouseStockInsufficient):
            self.control.calculate_restock_quote(INSTITUTION, SHOP_ID, "immunity")

    def test_restock_moves_stock_and_preserves_ledger_and_item_totals(self) -> None:
        self.create_shop()
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.fund_shop(2_000)
        self.control.produce_item(INSTITUTION, "immunity", 20, administrator_id=99)
        self.configure_restock()
        ledger_before = self.ledger.total_cash_backed_balance(INSTITUTION)
        item_before = 20
        quote = self.control.restock(INSTITUTION, SHOP_ID, "immunity", administrator_id=99)
        shop_stock = self.inventory.get_quantity(InventoryOwner(SHOP_OWNER_TYPE, SHOP_ID), "immunity")
        warehouse_stock = self.inventory.get_quantity(InventoryOwner(SYSTEM_OWNER_TYPE, "M_CORP"), "immunity")
        self.assertEqual(shop_stock, quote.quantity)
        self.assertEqual(shop_stock + warehouse_stock, item_before)
        self.assertEqual(self.ledger.total_cash_backed_balance(INSTITUTION), ledger_before)

    def test_restock_compensates_money_when_inventory_fails(self) -> None:
        self.create_shop()
        self.control.pay_license(INSTITUTION, SHOP_ID, administrator_id=99)
        self.fund_shop(2_000)
        self.control.produce_item(INSTITUTION, "immunity", 20, administrator_id=99)
        self.configure_restock()
        shop_balance = self.ledger.get_owner_balance(INSTITUTION, SHOP_OWNER_TYPE, SHOP_ID)
        with patch.object(self.inventory, "transfer_item", side_effect=RuntimeError("failure")):
            with self.assertRaises(Exception):
                self.control.restock(INSTITUTION, SHOP_ID, "immunity", administrator_id=99)
        self.assertEqual(self.ledger.get_owner_balance(INSTITUTION, SHOP_OWNER_TYPE, SHOP_ID), shop_balance)

    def test_control_metrics_cover_money_items_and_shop(self) -> None:
        self.create_shop()
        self.fund_shop(500)
        self.control.produce_item(INSTITUTION, "immunity", 4, administrator_id=99)
        metrics = self.control.economy_metrics(INSTITUTION)
        item = self.control.item_metrics()[0]
        report = self.control.shop_report(INSTITUTION, SHOP_ID)
        self.assertEqual(metrics.shop_balance, 500)
        self.assertEqual(metrics.total_cash_backed, 500)
        self.assertEqual(item.produced_total, 4)
        self.assertEqual(report.bank_balance, 500)

    def test_phase3_database_migration_is_additive_and_idempotent(self) -> None:
        legacy_path = Path(self.tempdir.name) / "phase3.sqlite3"
        legacy_inventory = InventoryStore(legacy_path)
        legacy_inventory.initialize()
        legacy_inventory.add_item(
            InventoryOwner("USER", "42"), "maro", 2, transaction_type="ITEM_ADMIN_GRANT"
        )
        migrated = ShopStore(legacy_path)
        migrated.initialize()
        migrated.initialize()
        with sqlite3.connect(legacy_path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("shops", tables)
        self.assertEqual(legacy_inventory.get_quantity(InventoryOwner("USER", "42"), "maro"), 2)
