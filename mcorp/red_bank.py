"""Infrastructure adapter for Red-DiscordBot's public Bank API."""

from __future__ import annotations

from typing import Any

from redbot.core import bank


class RedBankGateway:
    """Expose Red Bank through the protocol consumed by BankingService."""

    async def get_balance(self, member: Any) -> int:
        return await bank.get_balance(member)

    async def withdraw(self, member: Any, amount: int) -> int:
        return await bank.withdraw_credits(member, amount)

    async def deposit(self, member: Any, amount: int) -> int:
        return await bank.deposit_credits(member, amount)

    async def set_balance(self, member: Any, amount: int) -> int:
        return await bank.set_balance(member, amount)
