"""Minimal Discord interface for the HTTP-only MCorp MVP."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from redbot.core import commands

from .api_client import (
    MCorpApiClient,
    MCorpApiConfig,
    MCorpApiConfigurationError,
    MCorpApiError,
    SHOP_OWNER,
)


# Retail pricing is presentation configuration only. The API remains the sole
# authority for balances, stock and the atomic exchange.
SHOP_CATALOG: dict[str, dict[str, Any]] = {
    "potion": {"name": "Potion", "price": 200, "description": "Potion consommable."},
}


class MCorp(commands.Cog):
    """A thin Red cog: Discord input in, MCorp API result out."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.api: MCorpApiClient | None = None
        self.configuration_error: MCorpApiConfigurationError | None = None
        try:
            self.api = MCorpApiClient(MCorpApiConfig.from_environment())
        except MCorpApiConfigurationError as exc:
            self.configuration_error = exc

    def cog_unload(self) -> None:
        if self.api is not None:
            asyncio.create_task(self.api.aclose())

    @commands.command()
    @commands.guild_only()
    async def argent(self, ctx: commands.Context, membre: commands.MemberConverter | None = None) -> None:
        """Affiche le portefeuille RTC d'un membre."""
        target = membre or ctx.author
        try:
            wallet = await self._api().wallet(str(target.id))
        except MCorpApiError as exc:
            await self._send_api_error(ctx, exc)
            return
        await ctx.send(f"**Portefeuille de {target.display_name}** : {wallet['balance']} RTC")

    @commands.command()
    @commands.guild_only()
    async def donner(self, ctx: commands.Context, membre: commands.MemberConverter, montant: int) -> None:
        """Transfère des RTC de votre portefeuille API vers un autre membre."""
        if montant <= 0:
            await ctx.send("Le montant doit être strictement positif.")
            return
        if membre.id == ctx.author.id:
            await ctx.send("Vous ne pouvez pas vous transférer des RTC à vous-même.")
            return
        operation_id = self._operation_id("transfer")
        try:
            result = await self._api().transfer(str(ctx.author.id), str(membre.id), montant, operation_id)
        except MCorpApiError as exc:
            await self._send_api_error(ctx, exc)
            return
        await ctx.send(
            f"Transfert de {montant} RTC vers {membre.mention} confirmé. "
            f"Votre solde : {result['source_balance']} RTC."
        )

    @commands.command()
    @commands.guild_only()
    async def inventaire(self, ctx: commands.Context, membre: commands.MemberConverter | None = None) -> None:
        """Affiche l'inventaire API d'un membre."""
        target = membre or ctx.author
        try:
            inventory = await self._api().inventory({"owner_type": "USER", "owner_id": str(target.id)})
        except MCorpApiError as exc:
            await self._send_api_error(ctx, exc)
            return
        items = inventory.get("items", [])
        text = "\n".join(f"• `{entry['item_id']}` ×{entry['quantity']}" for entry in items) or "Aucun objet."
        await ctx.send(f"**Inventaire de {target.display_name}**\n{text}")

    @commands.command()
    @commands.guild_only()
    async def boutique(self, ctx: commands.Context) -> None:
        """Affiche les articles réellement disponibles de la boutique officielle."""
        try:
            inventory = await self._api().inventory(SHOP_OWNER)
        except MCorpApiError as exc:
            await self._send_api_error(ctx, exc)
            return
        stock = {str(entry["item_id"]): int(entry["quantity"]) for entry in inventory.get("items", [])}
        lines = [
            f"• **{entry['name']}** (`{item_id}`) — {entry['price']} RTC — stock ×{stock[item_id]}"
            for item_id, entry in SHOP_CATALOG.items() if stock.get(item_id, 0) > 0
        ]
        await ctx.send("**Boutique officielle M Corp**\n" + ("\n".join(lines) or "Aucun article disponible."))

    @commands.command()
    @commands.guild_only()
    async def acheter(self, ctx: commands.Context, objet: str, quantite: int = 1) -> None:
        """Achète un article de la boutique officielle par échange API atomique."""
        item_id = objet.casefold()
        item = SHOP_CATALOG.get(item_id)
        if item is None:
            await ctx.send("Cet article n'est pas vendu par la boutique officielle.")
            return
        if quantite <= 0:
            await ctx.send("La quantité doit être strictement positive.")
            return
        operation_id = self._operation_id("exchange")
        try:
            result = await self._api().exchange(
                str(ctx.author.id), item_id, quantite, int(item["price"]) * quantite, operation_id
            )
        except MCorpApiError as exc:
            await self._send_api_error(ctx, exc)
            return
        await ctx.send(
            f"Achat confirmé : {item['name']} ×{quantite} pour {int(item['price']) * quantite} RTC. "
            f"Solde : {result['buyer_balance']} RTC ; inventaire : ×{result['buyer_quantity']}."
        )

    @commands.command()
    @commands.guild_only()
    async def utiliser(self, ctx: commands.Context, objet: str) -> None:
        """Consomme un objet après confirmation atomique de l'API."""
        item_id = objet.casefold()
        operation_id = self._operation_id("consume")
        try:
            result = await self._api().consume(str(ctx.author.id), item_id, 1, operation_id)
        except MCorpApiError as exc:
            await self._send_api_error(ctx, exc)
            return
        display_name = SHOP_CATALOG.get(item_id, {}).get("name", item_id)
        await ctx.send(f"{display_name} consommé. Quantité restante : ×{result['quantity']}.")

    def _api(self) -> MCorpApiClient:
        if self.api is None:
            assert self.configuration_error is not None
            raise self.configuration_error
        return self.api

    @staticmethod
    def _operation_id(kind: str) -> str:
        return f"discord-{kind}-{uuid.uuid4()}"

    @staticmethod
    async def _send_api_error(ctx: commands.Context, exc: MCorpApiError) -> None:
        await ctx.send(str(exc))
