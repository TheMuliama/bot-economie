"""Command tests for the small HTTP-only MCorp cog."""

from __future__ import annotations

import unittest

from mcorp.api_client import MCorpApiError, SHOP_OWNER
from mcorp import setup
from mcorp.mcorp import MCorp


class Member:
    def __init__(self, member_id: int, name: str) -> None:
        self.id, self.display_name, self.mention = member_id, name, f"@{name}"


class Context:
    def __init__(self, author: Member) -> None:
        self.author, self.guild, self.sent = author, object(), []

    async def send(self, message: str) -> None:
        self.sent.append(message)


class Api:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.error: Exception | None = None

    async def wallet(self, user_id: str):
        self.calls.append(("wallet", (user_id,)))
        if self.error: raise self.error
        return {"balance": 400}

    async def inventory(self, owner):
        self.calls.append(("inventory", (owner,)))
        if self.error: raise self.error
        return {"items": [{"item_id": "potion", "quantity": 2}]}

    async def transfer(self, *args):
        self.calls.append(("transfer", args))
        if self.error: raise self.error
        return {"source_balance": 300, "destination_balance": 100}

    async def exchange(self, *args):
        self.calls.append(("exchange", args))
        if self.error: raise self.error
        return {"buyer_balance": 200, "buyer_quantity": 1}

    async def consume(self, *args):
        self.calls.append(("consume", args))
        if self.error: raise self.error
        return {"quantity": 0}


class MCorpCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.alice, self.bob = Member(1, "Alice"), Member(2, "Bob")
        self.ctx, self.api = Context(self.alice), Api()
        self.cog = object.__new__(MCorp)
        self.cog.api, self.cog.configuration_error = self.api, None

    async def test_wallet_and_inventory_use_discord_ids(self) -> None:
        await MCorp.argent.callback(self.cog, self.ctx, self.bob)
        await MCorp.inventaire.callback(self.cog, self.ctx, self.bob)
        self.assertEqual(self.api.calls[0], ("wallet", ("2",)))
        self.assertEqual(self.api.calls[1][1][0]["owner_id"], "2")

    async def test_transfer_rejects_bad_amount_and_self_then_uses_api(self) -> None:
        await MCorp.donner.callback(self.cog, self.ctx, self.bob, 0)
        await MCorp.donner.callback(self.cog, self.ctx, self.alice, 5)
        await MCorp.donner.callback(self.cog, self.ctx, self.bob, 100)
        call = self.api.calls[-1]
        self.assertEqual(call[0], "transfer")
        self.assertEqual(call[1][:3], ("1", "2", 100))
        self.assertTrue(str(call[1][3]).startswith("discord-transfer-"))

    async def test_shop_purchase_and_consumption(self) -> None:
        await MCorp.boutique.callback(self.cog, self.ctx)
        await MCorp.acheter.callback(self.cog, self.ctx, "potion", 1)
        await MCorp.utiliser.callback(self.cog, self.ctx, "potion")
        self.assertEqual(self.api.calls[0], ("inventory", (SHOP_OWNER,)))
        self.assertEqual(self.api.calls[1][0], "exchange")
        self.assertEqual(self.api.calls[1][1][0:4], ("1", "potion", 1, 200))
        self.assertEqual(self.api.calls[2][0], "consume")

    async def test_api_error_is_user_facing(self) -> None:
        self.api.error = MCorpApiError("Opération refusée : fonds ou stock insuffisant.")
        await MCorp.acheter.callback(self.cog, self.ctx, "potion", 1)
        self.assertIn("fonds ou stock insuffisant", self.ctx.sent[-1])

    def test_only_mvp_commands_are_registered(self) -> None:
        names = {command.name for command in MCorp.__cog_commands__}
        self.assertEqual(names, {"argent", "donner", "inventaire", "boutique", "acheter", "utiliser"})

    async def test_setup_loads_the_cog_without_legacy_services(self) -> None:
        class Bot:
            def __init__(self) -> None:
                self.cog = None

            async def add_cog(self, cog) -> None:
                self.cog = cog

        bot = Bot()
        await setup(bot)
        self.assertIsInstance(bot.cog, MCorp)
        self.assertFalse(hasattr(bot.cog, "banking"))
        self.assertFalse(hasattr(bot.cog, "control"))
