"""Business rules for catalogue-backed inventory operations."""

from __future__ import annotations

from .catalog import ItemDefinition, get_item
from .inventory import InventoryEntry, InventoryOwner, InventoryStore, InvalidItemQuantity


class UnknownItem(Exception):
    pass


class ItemNotTransferable(Exception):
    pass


class NonStackableQuantity(Exception):
    pass


class InventoryService:
    """Applies catalogue rules before delegating atomic persistence to the store."""

    def __init__(self, store: InventoryStore) -> None:
        self.store = store

    def get_inventory(self, owner: InventoryOwner) -> list[InventoryEntry]:
        return self.store.get_inventory(owner)

    def get_quantity(self, owner: InventoryOwner, item_id: str) -> int:
        return self.store.get_quantity(owner, self._item(item_id).item_id)

    def add_item(self, owner: InventoryOwner, item_id: str, quantity: int, *, administrator_id: int) -> int:
        item = self._item(item_id)
        self._require_quantity_for_item(item, quantity)
        return self.store.add_item(
            owner, item.item_id, quantity, transaction_type="ITEM_ADMIN_GRANT",
            metadata={"administrator_id": administrator_id}, max_quantity=None if item.stackable else 1,
        )

    def remove_item(self, owner: InventoryOwner, item_id: str, quantity: int, *, administrator_id: int) -> int:
        item = self._item(item_id)
        self._require_quantity_for_item(item, quantity)
        return self.store.remove_item(
            owner, item.item_id, quantity, transaction_type="ITEM_ADMIN_REMOVE",
            metadata={"administrator_id": administrator_id},
        )

    def transfer_item(
        self, source: InventoryOwner, destination: InventoryOwner, item_id: str, quantity: int,
        *, initiator_id: int | None = None, transaction_type: str = "ITEM_TRANSFER",
    ) -> tuple[int, int]:
        item = self._item(item_id)
        self._require_quantity_for_item(item, quantity)
        if not item.transferable:
            raise ItemNotTransferable(item.item_id)
        return self.store.transfer_item(
            source, destination, item.item_id, quantity,
            transaction_type=transaction_type,
            metadata={} if initiator_id is None else {"initiator_id": initiator_id},
            destination_max_quantity=None if item.stackable else 1,
        )

    def produce_item(
        self, producer: InventoryOwner, item_id: str, quantity: int, *, administrator_id: int
    ) -> int:
        item = self._item(item_id)
        self._require_quantity_for_item(item, quantity)
        return self.store.add_item(
            producer, item.item_id, quantity, transaction_type="ITEM_PRODUCTION",
            metadata={"administrator_id": administrator_id},
            max_quantity=None if item.stackable else 1,
        )

    @staticmethod
    def _require_quantity_for_item(item: ItemDefinition, quantity: int) -> None:
        if not isinstance(quantity, int) or quantity <= 0:
            raise InvalidItemQuantity()
        if not item.stackable and quantity != 1:
            raise NonStackableQuantity(item.item_id)

    @staticmethod
    def _item(item_id: str) -> ItemDefinition:
        item = get_item(item_id)
        if item is None:
            raise UnknownItem(item_id)
        return item
