"""Focused command-level tests without requiring a running Discord connection."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mcorp.mcorp import MCorp


class Member:
    def __init__(self, member_id: int, name: str) -> None:
        self.id = member_id
        self.display_name = name
        self.mention = f"@{name}"


class Context:
    def __init__(self, author: Member) -> None:
        self.author = author
        self.guild = object()
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


class Ledger:
    def get_user_balance(self, institution_id: str, user_id: int) -> int:
        return 300


class Banking:
    def __init__(self) -> None:
        self.cash_calls: list[tuple[object, object, object, int]] = []
        self.treasury_calls: list[tuple[object, object, object, int]] = []
        self.transfer_calls: list[tuple[object, object, object, object, int]] = []

    async def cash_transfer(self, *args):  # type: ignore[no-untyped-def]
        self.cash_calls.append(args)

    async def pay_treasury(self, *args):  # type: ignore[no-untyped-def]
        self.treasury_calls.append(args)
        return (200, 100)

    async def bank_transfer(self, *args):  # type: ignore[no-untyped-def]
        self.transfer_calls.append(args)
        return (200, 400)


class MCorpCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.alice = Member(1, "Alice")
        self.bob = Member(2, "Bob")
        self.ctx = Context(self.alice)
        self.cog = object.__new__(MCorp)
        self.cog.ledger = Ledger()
        self.cog.banking = Banking()

        async def bank_context(ctx):  # type: ignore[no-untyped-def]
            return "mcorp:guild:1", Member(999, "M Corp")

        self.cog._bank_context = bank_context

    async def test_argent_aggregates_red_cash_and_mcorp_account(self) -> None:
        async def get_balance(member):  # type: ignore[no-untyped-def]
            return 800

        async def get_currency_name(guild):  # type: ignore[no-untyped-def]
            return "R"

        with patch("mcorp.mcorp.bank.get_balance", get_balance), patch(
            "mcorp.mcorp.bank.get_currency_name", get_currency_name
        ):
            await MCorp.argent.callback(self.cog, self.ctx)

        self.assertIn("Sur vous : 800 R", self.ctx.sent[0])
        self.assertIn("Compte bancaire : 300 crédits", self.ctx.sent[0])

    async def test_payer_mcorp_uses_banking_treasury_payment(self) -> None:
        await MCorp.payer.callback(self.cog, self.ctx, "mcorp", 100)

        self.assertEqual(len(self.cog.banking.treasury_calls), 1)
        self.assertIn("Paiement de 100 crédits à M Corp", self.ctx.sent[0])

    async def test_payer_member_uses_banking_transfer(self) -> None:
        class Converter:
            async def convert(self, ctx, argument):  # type: ignore[no-untyped-def]
                return self_bob

        self_bob = self.bob
        with patch("mcorp.mcorp.commands.MemberConverter", Converter):
            await MCorp.payer.callback(self.cog, self.ctx, "@Bob", 100)

        self.assertEqual(len(self.cog.banking.transfer_calls), 1)
        self.assertIn("Paiement bancaire de 100 crédits", self.ctx.sent[0])

    async def test_donner_cash_uses_red_bank_transfer_and_not_inventory(self) -> None:
        async def get_currency_name(guild):  # type: ignore[no-untyped-def]
            return "R"

        with patch("mcorp.mcorp.bank.get_currency_name", get_currency_name):
            await MCorp.donner.callback(self.cog, self.ctx, self.bob, "100R", None)

        self.assertEqual(self.cog.banking.cash_calls[0][-1], 100)
        self.assertIn("100 R", self.ctx.sent[0])

    def test_cash_token_parser_is_unambiguous(self) -> None:
        self.assertEqual(MCorp._parse_cash_amount("100R"), 100)
        self.assertEqual(MCorp._parse_cash_amount("100r"), 100)
        self.assertIsNone(MCorp._parse_cash_amount("100"))
        self.assertIsNone(MCorp._parse_cash_amount("immunity"))

    async def test_solde_compatibility_command_redirects_to_argent(self) -> None:
        async def get_balance(member):  # type: ignore[no-untyped-def]
            return 800

        async def get_currency_name(guild):  # type: ignore[no-untyped-def]
            return "R"

        with patch("mcorp.mcorp.bank.get_balance", get_balance), patch(
            "mcorp.mcorp.bank.get_currency_name", get_currency_name
        ):
            await MCorp.solde.callback(self.cog, self.ctx)

        self.assertIn("remplacée par `m!argent`", self.ctx.sent[0])
        self.assertIn("Sur vous : 800 R", self.ctx.sent[1])
