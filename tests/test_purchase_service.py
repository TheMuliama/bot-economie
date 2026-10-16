"""Deterministic transactional tests for player-shop purchases."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcorp.banking_service import BankingService
from mcorp.bootstrap import build_core_services
from mcorp.control import M_CORP_OWNER_ID
from mcorp.owners import InventoryOwner, SHOP_OWNER_TYPE, SYSTEM_OWNER_TYPE
from mcorp.purchase_service import PurchaseInsufficientStock


class Member:
    def __init__(self, member_id: int) -> None:
        self.id = member_id


class FakeBank:
    def __init__(self, balances: dict[int, int]) -> None:
        self.balances = balances

    async def get_balance(self, member: Member) -> int:
        return self.balances.get(member.id, 0)

    async def withdraw(self, member: Member, amount: int) -> int:
        self.balances[member.id] -= amount
        return self.balances[member.id]

    async def deposit(self, member: Member, amount: int) -> int:
        self.balances[member.id] = self.balances.get(member.id, 0) + amount
        return self.balances[member.id]

    async def set_balance(self, member: Member, amount: int) -> int:
        self.balances[member.id] = amount
        return amount


class PurchaseServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.core = build_core_services(Path(self.tempdir.name) / "ledger.sqlite3")
        self.institution = "mcorp:test"
        self.vault = Member(999)
        self.alice = Member(1)
        self.core.ledger.ensure_institution(self.institution, "test", self.vault.id)
        self.core.ledger.mark_initialized(self.institution, self.vault.id, 0)
        self.core.control.ensure_official_shop(self.institution)
        self.core.control.set_item_permission(self.institution, "M_CORP_STORE", "immunity", True, administrator_id=1)
        self.core.control.set_sale_price(self.institution, "M_CORP_STORE", "immunity", 100, administrator_id=1)
        warehouse = InventoryOwner(SYSTEM_OWNER_TYPE, M_CORP_OWNER_ID)
        shop = InventoryOwner(SHOP_OWNER_TYPE, "M_CORP_STORE")
        self.core.inventory.produce_item(warehouse, "immunity", 2, administrator_id=1)
        self.core.inventory.transfer_item(warehouse, shop, "immunity", 2)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_bank_sale_is_idempotent_and_conserves_stock(self) -> None:
        self.core.ledger.record_deposit(self.institution, self.alice.id, 200)
        first = self.core.purchases.purchase_bank(
            self.institution, self.alice.id, "M_CORP_STORE", "immunity", 1, "same-id"
        )
        second = self.core.purchases.purchase_bank(
            self.institution, self.alice.id, "M_CORP_STORE", "immunity", 1, "same-id"
        )
        self.assertEqual(first, second)
        self.assertEqual(self.core.ledger.get_user_balance(self.institution, self.alice.id), 100)
        self.assertEqual(self.core.inventory.get_quantity(InventoryOwner(SHOP_OWNER_TYPE, "M_CORP_STORE"), "immunity"), 1)

    async def test_cash_sale_moves_money_to_vault_and_creates_covered_credit(self) -> None:
        bank = FakeBank({self.alice.id: 150, self.vault.id: 0})
        banking = BankingService(self.core.ledger, bank)
        result = await banking.cash_purchase(
            self.institution, self.alice, self.vault, 100,
            lambda: self.core.purchases.commit_cash_purchase(
                self.institution, self.alice.id, "M_CORP_STORE", "immunity", 1, "cash-id"
            ),
        )
        self.assertEqual(result.payment_method, "CASH")
        self.assertEqual(bank.balances[self.alice.id], 50)
        self.assertEqual(bank.balances[self.vault.id], 100)
        self.assertTrue((await banking.reconcile(self.institution, self.vault)).is_balanced)

    async def test_last_item_cannot_be_sold_twice(self) -> None:
        self.core.ledger.record_deposit(self.institution, self.alice.id, 200)
        self.core.purchases.purchase_bank(self.institution, self.alice.id, "M_CORP_STORE", "immunity", 2, "all")
        with self.assertRaises(PurchaseInsufficientStock):
            self.core.purchases.purchase_bank(self.institution, self.alice.id, "M_CORP_STORE", "immunity", 1, "late")
