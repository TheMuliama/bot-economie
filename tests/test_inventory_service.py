"""Tests for the catalogue-backed generic inventory service."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcorp.inventory import (
    SHOP_OWNER_TYPE,
    SYSTEM_OWNER_TYPE,
    USER_OWNER_TYPE,
    InsufficientInventory,
    InvalidItemQuantity,
    InventoryCapacityExceeded,
    InventoryOwner,
    InventoryStore,
)
from mcorp.inventory_service import (
    InventoryService,
    ItemNotTransferable,
    NonStackableQuantity,
    UnknownItem,
)


class FailingJournalStore(InventoryStore):
    @staticmethod
    def _insert_transaction(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated journal failure")


class InventoryServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "ledger.sqlite3"
        self.store = InventoryStore(self.path)
        self.store.initialize()
        self.service = InventoryService(self.store)
        self.alice = InventoryOwner(USER_OWNER_TYPE, "1")
        self.bob = InventoryOwner(USER_OWNER_TYPE, "2")
        self.corp = InventoryOwner(SYSTEM_OWNER_TYPE, "M_CORP")
        self.shop = InventoryOwner(SHOP_OWNER_TYPE, "GENERAL_STORE")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_empty_inventory(self) -> None:
        self.assertEqual(self.service.get_inventory(self.alice), [])

    def test_known_catalogue_item_can_be_added(self) -> None:
        self.assertEqual(self.service.add_item(self.alice, "immunity", 2, administrator_id=99), 2)
        self.assertEqual(self.service.get_quantity(self.alice, "immunity"), 2)

    def test_unknown_item_is_refused(self) -> None:
        with self.assertRaises(UnknownItem):
            self.service.add_item(self.alice, "unknown", 1, administrator_id=99)

    def test_non_transferable_item_is_refused(self) -> None:
        self.service.add_item(self.alice, "rename", 1, administrator_id=99)
        with self.assertRaises(ItemNotTransferable):
            self.service.transfer_item(self.alice, self.bob, "rename", 1, initiator_id=1)

    def test_non_stackable_items_are_limited_to_one_per_owner(self) -> None:
        self.service.add_item(self.alice, "rename", 1, administrator_id=99)
        with self.assertRaises(NonStackableQuantity):
            self.service.add_item(self.bob, "rename", 2, administrator_id=99)
        with self.assertRaises(InventoryCapacityExceeded):
            self.service.add_item(self.alice, "rename", 1, administrator_id=99)

    def test_remove_item(self) -> None:
        self.service.add_item(self.alice, "immunity", 3, administrator_id=99)
        self.assertEqual(self.service.remove_item(self.alice, "immunity", 1, administrator_id=99), 2)

    def test_insufficient_item_is_refused(self) -> None:
        with self.assertRaises(InsufficientInventory):
            self.service.remove_item(self.alice, "immunity", 1, administrator_id=99)

    def test_zero_and_negative_quantities_are_refused(self) -> None:
        for quantity in (0, -1):
            with self.assertRaises(InvalidItemQuantity):
                self.service.add_item(self.alice, "immunity", quantity, administrator_id=99)
            with self.assertRaises(InvalidItemQuantity):
                self.service.transfer_item(self.alice, self.bob, "immunity", quantity, initiator_id=1)

    def test_user_to_user_transfer_conserves_quantity(self) -> None:
        self.service.add_item(self.alice, "immunity", 3, administrator_id=99)
        source, destination = self.service.transfer_item(
            self.alice, self.bob, "immunity", 2, initiator_id=1
        )
        self.assertEqual((source, destination), (1, 2))
        self.assertEqual(
            self.service.get_quantity(self.alice, "immunity") + self.service.get_quantity(self.bob, "immunity"), 3
        )

    def test_transfer_is_rolled_back_when_journal_fails(self) -> None:
        store = FailingJournalStore(self.path)
        service = InventoryService(store)
        self.store.add_item(self.alice, "immunity", 2, transaction_type="ITEM_ADMIN_GRANT")
        with self.assertRaises(RuntimeError):
            service.transfer_item(self.alice, self.bob, "immunity", 1, initiator_id=1)
        self.assertEqual(self.store.get_quantity(self.alice, "immunity"), 2)
        self.assertEqual(self.store.get_quantity(self.bob, "immunity"), 0)

    def test_inventory_persists_after_reopen(self) -> None:
        self.service.add_item(self.alice, "maro", 2, administrator_id=99)
        reopened = InventoryService(InventoryStore(self.path))
        reopened.store.initialize()
        self.assertEqual(reopened.get_quantity(self.alice, "maro"), 2)

    def test_system_inventory(self) -> None:
        self.service.add_item(self.corp, "immunity", 100, administrator_id=99)
        self.assertEqual(self.service.get_quantity(self.corp, "immunity"), 100)

    def test_shop_inventory(self) -> None:
        self.service.add_item(self.shop, "maro", 2, administrator_id=99)
        self.assertEqual(self.service.get_quantity(self.shop, "maro"), 2)

    def test_system_to_shop_transfer(self) -> None:
        self.service.add_item(self.corp, "immunity", 10, administrator_id=99)
        self.service.transfer_item(self.corp, self.shop, "immunity", 10)
        self.assertEqual(self.service.get_quantity(self.shop, "immunity"), 10)

    def test_shop_to_user_transfer_is_supported_by_service(self) -> None:
        self.service.add_item(self.shop, "maro", 1, administrator_id=99)
        self.service.transfer_item(self.shop, self.alice, "maro", 1)
        self.assertEqual(self.service.get_quantity(self.alice, "maro"), 1)

    def test_transaction_is_journaled(self) -> None:
        self.service.add_item(self.alice, "immunity", 1, administrator_id=99)
        self.service.transfer_item(self.alice, self.bob, "immunity", 1, initiator_id=1)
        transaction = self.store.recent_transactions(self.alice)[0]
        self.assertEqual(transaction.transaction_type, "ITEM_TRANSFER")
        self.assertEqual(transaction.item_id, "immunity")
        self.assertEqual(transaction.destination_owner, self.bob)
