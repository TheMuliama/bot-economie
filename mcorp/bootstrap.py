"""Explicit construction of the M Corp services shared by Red and FastAPI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .control import EconomyControlService
from .identities import DiscordIdentityStore
from .inventory_store import InventoryStore
from .inventory_service import InventoryService
from .ledger import LedgerStore
from .shop_store import ShopStore


@dataclass(frozen=True)
class CoreServices:
    ledger: LedgerStore
    inventory_store: InventoryStore
    inventory: InventoryService
    shop_store: ShopStore
    control: EconomyControlService
    identities: DiscordIdentityStore


def build_core_services(database_path: Path) -> CoreServices:
    """Build and initialize the persistence-backed services used by both UIs."""
    ledger = LedgerStore(database_path)
    inventory_store = InventoryStore(database_path)
    inventory = InventoryService(inventory_store)
    shop_store = ShopStore(database_path)
    identities = DiscordIdentityStore(database_path)

    ledger.initialize()
    inventory_store.initialize()
    shop_store.initialize()
    identities.initialize()

    control = EconomyControlService(ledger, inventory, shop_store)
    return CoreServices(
        ledger, inventory_store, inventory, shop_store, control, identities
    )
