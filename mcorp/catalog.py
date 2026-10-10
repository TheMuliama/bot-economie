"""Versioned definitions for the items known by M Corp.

The catalogue answers which item types exist.  It deliberately contains no
stock nor owner information: those belong to :mod:`mcorp.inventory`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ItemDefinition:
    item_id: str
    display_name: str
    description: str
    transferable: bool
    stackable: bool
    consumable: bool


ITEM_CATALOG: dict[str, ItemDefinition] = {
    "ticket_silence": ItemDefinition(
        "ticket_silence", "Ticket silence", "Ticket sans effet gameplay pour le moment.", True, True, True
    ),
    "ticket_vote": ItemDefinition(
        "ticket_vote", "Ticket de vote", "Ticket sans effet gameplay pour le moment.", True, True, True
    ),
    "immunity": ItemDefinition(
        "immunity", "Immunité", "Immunité sans effet gameplay pour le moment.", True, True, True
    ),
    "maro": ItemDefinition(
        "maro", "Maro", "Objet M Corp sans effet gameplay pour le moment.", True, True, False
    ),
    "rename": ItemDefinition(
        "rename", "Renommage", "Objet personnel non transférable, sans effet pour le moment.", False, False, True
    ),
}


def get_item(item_id: str) -> ItemDefinition | None:
    """Return a definition using the canonical, case-insensitive item id."""
    return ITEM_CATALOG.get(item_id.casefold())
