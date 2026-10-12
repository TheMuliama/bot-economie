"""Owner types shared by accounts and inventories.

The schema deliberately stores owner types as text, so future COMPANY or NPC
owners can be added without implementing them during the V1 refactor.
"""

from __future__ import annotations

from dataclasses import dataclass


USER_OWNER_TYPE = "USER"
SHOP_OWNER_TYPE = "SHOP"
SYSTEM_OWNER_TYPE = "SYSTEM"


@dataclass(frozen=True)
class InventoryOwner:
    owner_type: str
    owner_id: str

    def __post_init__(self) -> None:
        if not self.owner_type or not self.owner_type.isupper() or not self.owner_id:
            raise ValueError("Inventory owners require an uppercase type and a non-empty id.")
