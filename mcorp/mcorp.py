"""Discord commands for the M Corp covered-banking MVP."""

from __future__ import annotations

import re
import uuid
from typing import Any

from redbot.core import bank, commands
from redbot.core.data_manager import cog_data_path

from .bootstrap import build_core_services
from .ledger import InsufficientLedgerFunds, InvalidAmount, SameAccountTransfer
from .catalog import get_item
from .control import (
    AutoRestockDisabled,
    AutoRestockNotAllowed,
    ControlOperationFailed,
    ControlRecoveryRequired,
    InvalidControlValue,
    RestockBudgetExceeded,
    RestockThresholdNotReached,
    ShopAlreadyExists,
    ShopFundsInsufficient,
    ShopItemDenied,
    ShopUnavailable,
    UnitPriceLimitExceeded,
    WarehouseStockInsufficient,
    WholesaleOrderLimit,
    WholesaleUnavailable,
)
from .inventory_store import (
    InsufficientInventory,
    InvalidItemQuantity,
    InventoryCapacityExceeded,
    SameInventoryOwner,
)
from .inventory_service import (
    ItemNotTransferable,
    NonStackableQuantity,
    UnknownItem,
)
from .shop_store import ShopNotFound
from .owners import USER_OWNER_TYPE, InventoryOwner
from .banking_service import (
    BankingOperationError,
    BankingService,
    AlreadyInitialized,
    InitialVaultBalanceRequiresConfirmation,
    InitializationRefused,
    InvariantViolation,
    InstitutionNotInitialized,
    InsufficientCash,
    RecoveryRequired,
)
from .red_bank import RedBankGateway
from .purchase_service import (
    PurchaseError,
    PurchaseInsufficientFunds,
    PurchaseInsufficientStock,
    PurchasePaymentNotAllowed,
    PurchasePriceChanged,
)


class MCorp(commands.Cog):
    """Covered bank accounts backed one-for-one by the M Corp Red vault."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        core = build_core_services(cog_data_path(self) / "ledger.sqlite3")
        self.banking = BankingService(core.ledger, RedBankGateway())
        self.inventory = core.inventory
        self.control = core.control
        self.purchases = core.purchases
        self.identity_store = core.identities

    async def cog_before_invoke(self, ctx: commands.Context) -> None:
        """Remember active command authors without scanning the Discord guild."""
        self._remember_member(ctx.author)

    @commands.command()
    async def mcorp(self, ctx: commands.Context) -> None:
        """Vérifie que le cog M Corp est chargé."""
        await ctx.send("M Corp est opérationnelle.")

    @commands.command()
    @commands.guild_only()
    async def argent(self, ctx: commands.Context) -> None:
        """Affiche votre liquide Red et votre compte bancaire M Corp."""
        await self._send_money_summary(ctx)

    @commands.command()
    @commands.guild_only()
    async def solde(self, ctx: commands.Context) -> None:
        """Compatibilité : utilisez plutôt m!argent."""
        await ctx.send("`m!solde` est remplacée par `m!argent`.")
        await self._send_money_summary(ctx)

    @commands.command()
    @commands.guild_only()
    async def compte(self, ctx: commands.Context) -> None:
        """Compatibilité : utilisez plutôt m!argent."""
        await ctx.send("`m!compte` est remplacée par `m!argent`.")
        await self._send_money_summary(ctx)

    async def _send_money_summary(self, ctx: commands.Context) -> None:
        """Render the user-facing view without exposing storage implementation."""
        institution_id, vault = await self._bank_context(ctx)
        wallet = await bank.get_balance(ctx.author)
        account = self.banking.account_balance(institution_id, ctx.author.id)
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(
            f"**💰 Argent de {ctx.author.display_name}**\n"
            f"Sur vous : {wallet} {currency}\n"
            f"Compte bancaire : {account} crédits"
        )

    @commands.command()
    @commands.guild_only()
    async def depot(self, ctx: commands.Context, montant: int) -> None:
        """Dépose des R-crédits dans votre compte bancaire M Corp."""
        institution_id, vault = await self._bank_context(ctx)
        try:
            new_balance = await self.banking.deposit(institution_id, ctx.author, vault, montant)
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(
            f"Dépôt de {montant} {currency} effectué. "
            f"Votre compte bancaire contient maintenant {new_balance} crédits."
        )

    @commands.command()
    @commands.guild_only()
    async def retrait(self, ctx: commands.Context, montant: int) -> None:
        """Retire des R-crédits de votre compte bancaire M Corp."""
        institution_id, vault = await self._bank_context(ctx)
        try:
            new_balance = await self.banking.withdraw(institution_id, ctx.author, vault, montant)
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(
            f"Retrait de {montant} {currency} effectué. "
            f"Votre compte bancaire contient maintenant {new_balance} crédits."
        )

    @commands.command()
    @commands.guild_only()
    async def donner(
        self,
        ctx: commands.Context,
        membre: commands.MemberConverter,
        cible: str,
        quantite: int | None = None,
    ) -> None:
        """Donne du liquide (`100R`) ou un objet (`immunity [quantité]`)."""
        self._remember_member(membre)
        institution_id, _ = await self._bank_context(ctx)
        cash_amount = self._parse_cash_amount(cible)
        if cash_amount is not None:
            if quantite is not None:
                await ctx.send("Syntaxe liquide : `m!donner @membre 100R`.")
                return
            try:
                await self.banking.cash_transfer(institution_id, ctx.author, membre, cash_amount)
            except Exception as exc:
                await self._send_operation_error(ctx, exc)
                return
            currency = await bank.get_currency_name(ctx.guild)
            await ctx.send(f"Vous avez donné {cash_amount} {currency} à {membre.mention}.")
            return

        quantity = 1 if quantite is None else quantite
        try:
            source_quantity, destination_quantity = self.inventory.transfer_item(
                self._user_owner(ctx.author.id),
                self._user_owner(membre.id),
                cible,
                quantity,
                initiator_id=ctx.author.id,
            )
        except Exception as exc:
            await self._send_inventory_error(ctx, exc)
            return
        item = get_item(cible)
        assert item is not None
        await ctx.send(
            f"Vous avez donné {item.display_name} ×{quantity} à {membre.mention}.\n"
            f"Votre inventaire : {item.display_name} ×{source_quantity}\n"
            f"Inventaire de {membre.display_name} : {item.display_name} ×{destination_quantity}"
        )

    @commands.command()
    @commands.guild_only()
    async def inventaire(self, ctx: commands.Context) -> None:
        """Affiche votre liquide Red et vos objets M Corp."""
        await self._send_inventory(ctx, ctx.author)

    @commands.command()
    @commands.guild_only()
    async def boutique(self, ctx: commands.Context, shop_id: str | None = None) -> None:
        """Consulte le marché joueur et achète un article en une confirmation.

        Syntaxe volontairement courte pour V1 : ``m!boutique`` liste les
        boutiques, ``m!boutique ID`` leur catalogue, puis
        ``m!boutique ID item quantité BANK|CASH`` reste disponible via la
        commande ``m!acheter`` ci-dessous lorsque les composants Discord ne
        sont pas utilisables (DM, client ancien ou interaction expirée).
        """
        institution_id, _ = await self._bank_context(ctx)
        if shop_id is None:
            shops = self.purchases.accessible_shops(institution_id)
            if not shops:
                await ctx.send("Aucune boutique publique n'est disponible actuellement.")
                return
            lines = [f"• **{shop.display_name}** — `{shop.shop_id}`" for shop in shops]
            await ctx.send("**M Corp — Marché Élite R&T**\nChoisissez une boutique :\n" + "\n".join(lines))
            return
        try:
            items = self.purchases.catalog(institution_id, shop_id.upper())
        except PurchaseError:
            await ctx.send("Cette boutique n'est pas disponible.")
            return
        if not items:
            await ctx.send("Cette boutique ne propose aucun article en stock.")
            return
        lines = [f"• `{item.item_id}` — {item.unit_price} RTC — stock ×{item.stock}" for item in items]
        await ctx.send(
            f"**Boutique `{shop_id.upper()}`**\n" + "\n".join(lines)
            + "\n\nAcheter : `m!acheter <boutique> <objet> <quantité> <BANK|CASH>`."
        )

    @commands.command()
    @commands.guild_only()
    async def acheter(
        self, ctx: commands.Context, shop_id: str, item_id: str, quantite: int, paiement: str
    ) -> None:
        """Finalise une vente V1 après toutes les validations côté métier."""
        institution_id, vault = await self._bank_context(ctx)
        method = paiement.upper()
        purchase_id = str(uuid.uuid4())
        try:
            catalog = {item.item_id: item for item in self.purchases.catalog(institution_id, shop_id.upper())}
            displayed = catalog.get(item_id.casefold())
            if displayed is None:
                await ctx.send("Cet article n'est plus disponible dans cette boutique.")
                return
            if method == "BANK":
                result = self.purchases.purchase_bank(
                    institution_id, ctx.author.id, shop_id.upper(), item_id, quantite, purchase_id,
                    expected_unit_price=displayed.unit_price,
                )
            elif method == "CASH":
                total = displayed.unit_price * quantite
                result = await self.banking.cash_purchase(
                    institution_id, ctx.author, vault, total,
                    lambda: self.purchases.commit_cash_purchase(
                        institution_id, ctx.author.id, shop_id.upper(), item_id, quantite, purchase_id,
                        expected_unit_price=displayed.unit_price,
                    ),
                )
            else:
                await ctx.send("Moyen de paiement invalide : utilisez `BANK` ou `CASH`.")
                return
        except Exception as exc:
            await self._send_purchase_error(ctx, exc)
            return
        await ctx.send(
            f"Achat confirmé : `{result.item_id}` ×{result.quantity} pour {result.total} RTC "
            f"par {result.payment_method}. Référence : `{result.purchase_id}`."
        )

    @commands.command()
    @commands.guild_only()
    async def payer(self, ctx: commands.Context, beneficiaire: str, montant: int) -> None:
        """Effectue un paiement bancaire vers un membre ou M Corp."""
        institution_id, vault = await self._bank_context(ctx)
        if beneficiaire.casefold() == "mcorp":
            try:
                player_balance, treasury_balance = await self.banking.pay_treasury(
                    institution_id, ctx.author, vault, montant
                )
            except Exception as exc:
                await self._send_operation_error(ctx, exc)
                return
            await ctx.send(
                f"Paiement de {montant} crédits à M Corp effectué.\n"
                f"Votre compte bancaire : {player_balance} crédits\n"
                f"Trésorerie M Corp : {treasury_balance} crédits"
            )
            return
        try:
            membre = await commands.MemberConverter().convert(ctx, beneficiaire)
        except commands.CommandError:
            await ctx.send("Indiquez un membre valide ou le mot-clé `mcorp`.")
            return
        self._remember_member(membre)
        if membre.id == vault.id:
            await ctx.send("Utilisez `m!depot` pour alimenter le coffre M Corp.")
            return
        try:
            source_balance, _ = await self.banking.bank_transfer(
                institution_id, ctx.author, membre, vault, montant
            )
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        await ctx.send(
            f"Paiement bancaire de {montant} crédits envoyé à {membre.mention}. "
            f"Votre compte bancaire contient maintenant {source_balance} crédits."
        )

    @commands.command()
    @commands.guild_only()
    async def virement(self, ctx: commands.Context, membre: commands.MemberConverter, montant: int) -> None:
        """Compatibilité : utilisez plutôt m!payer @membre montant."""
        self._remember_member(membre)
        await ctx.send("`m!virement` est remplacée par `m!payer @membre montant`.")
        institution_id, vault = await self._bank_context(ctx)
        try:
            source_balance, _ = await self.banking.bank_transfer(
                institution_id, ctx.author, membre, vault, montant
            )
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        await ctx.send(
            f"Virement de {montant} crédits envoyé à {membre.mention}. "
            f"Votre compte bancaire contient maintenant {source_balance} crédits."
        )

    @commands.command()
    @commands.guild_only()
    async def payermcorp(self, ctx: commands.Context, montant: int) -> None:
        """Compatibilité : utilisez plutôt m!payer mcorp montant."""
        await ctx.send("`m!payermcorp` est remplacée par `m!payer mcorp montant`.")
        institution_id, vault = await self._bank_context(ctx)
        try:
            player_balance, treasury_balance = await self.banking.pay_treasury(
                institution_id, ctx.author, vault, montant
            )
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        await ctx.send(
            f"Paiement de {montant} crédits à M Corp effectué.\n"
            f"Votre compte : {player_balance} crédits\n"
            f"Trésorerie M Corp : {treasury_balance} crédits"
        )

    @commands.command()
    @commands.guild_only()
    async def tresorerie(self, ctx: commands.Context) -> None:
        """Affiche la situation couverte de la trésorerie M Corp."""
        institution_id, vault = await self._bank_context(ctx)
        try:
            report = await self.banking.economy_report(institution_id, vault)
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        self.control.record_reconciliation_snapshot(
            institution_id, report.reconciliation.vault_balance,
            checked_by=str(ctx.author.id),
        )
        currency = await bank.get_currency_name(ctx.guild)
        state = "OK" if report.reconciliation.is_balanced else "ANOMALIE"
        await ctx.send(
            "**Trésorerie M Corp**\n"
            f"Trésorerie M Corp : {report.stats.treasury_balance} crédits\n"
            f"Total comptes couverts : {report.reconciliation.ledger_total} crédits\n"
            f"Coffre Red : {report.reconciliation.vault_balance} {currency}\n"
            f"État : **{state}**"
        )

    @commands.command(name="transactions")
    @commands.guild_only()
    async def transactions(self, ctx: commands.Context) -> None:
        """Affiche vos dix dernières opérations M Corp."""
        institution_id, _ = await self._bank_context(ctx)
        entries = self.banking.recent_transactions(institution_id, ctx.author.id)
        if not entries:
            await ctx.send("Aucune transaction M Corp pour le moment.")
            return
        lines = [
            f"• `{entry.transaction_type}` — {entry.amount} crédits — {entry.status}"
            for entry in entries
        ]
        await ctx.send("**Dernières transactions M Corp**\n" + "\n".join(lines))

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def crediter(
        self, ctx: commands.Context, membre: commands.MemberConverter, montant: int
    ) -> None:
        """[Propriétaire] Crédite le portefeuille Red d'un membre, hors ledger."""
        self._remember_member(membre)
        institution_id, vault = await self._bank_context(ctx)
        if membre.id == vault.id:
            await ctx.send("Le coffre ne peut pas être crédité administrativement : utilisez `m!depot`.")
            return
        try:
            new_balance = await self.banking.admin_cash_credit(
                institution_id, ctx.author, membre, montant
            )
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(
            f"Crédit administratif de {montant} {currency} vers {membre.mention}.\n"
            f"Nouveau portefeuille Red : {new_balance} {currency}.\n"
            "Cette opération ne modifie pas le ledger M Corp."
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def debiter(
        self, ctx: commands.Context, membre: commands.MemberConverter, montant: int
    ) -> None:
        """[Propriétaire] Débite le portefeuille Red d'un membre, hors ledger."""
        self._remember_member(membre)
        institution_id, vault = await self._bank_context(ctx)
        if membre.id == vault.id:
            await ctx.send("Le coffre ne peut pas être débité administrativement : utilisez `m!retrait`.")
            return
        try:
            new_balance = await self.banking.admin_cash_debit(
                institution_id, ctx.author, membre, montant
            )
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(
            f"Débit administratif de {montant} {currency} sur {membre.mention}.\n"
            f"Nouveau portefeuille Red : {new_balance} {currency}.\n"
            "Cette opération ne modifie pas le ledger M Corp."
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def prime(
        self, ctx: commands.Context, membre: commands.MemberConverter, montant: int
    ) -> None:
        """[Propriétaire] Verse une prime depuis la trésorerie M Corp."""
        self._remember_member(membre)
        institution_id, vault = await self._bank_context(ctx)
        if membre.id == vault.id:
            await ctx.send("Le coffre ne peut pas recevoir une prime de trésorerie.")
            return
        try:
            treasury_balance, recipient_balance = await self.banking.pay_from_treasury(
                institution_id,
                membre,
                vault,
                montant,
                administrator=ctx.author,
            )
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        await ctx.send(
            f"Prime de {montant} crédits versée à {membre.mention}.\n"
            f"Trésorerie restante : {treasury_balance} crédits\n"
            f"Compte de {membre.display_name} : {recipient_balance} crédits"
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def bankinit(self, ctx: commands.Context, confirmer: str | None = None) -> None:
        """[Propriétaire] Initialise une fois le coffre M Corp.

        With Red's non-zero default balance, rerun this command as
        ``m!bankinit neutraliser`` only after reviewing the displayed warning.
        """
        institution_id, vault = await self._bank_context(ctx)
        try:
            report = await self.banking.initialize_institution(
                institution_id,
                vault,
                default_vault_balance=await self._red_default_balance(vault),
                confirm_default_balance_reset=confirmer == "neutraliser",
            )
        except InitialVaultBalanceRequiresConfirmation:
            await ctx.send(
                "Le ledger est vierge mais le coffre contient exactement le solde par défaut Red. "
                "Après avoir vérifié qu'il ne s'agit pas de fonds légitimes, confirmez explicitement avec "
                "`m!bankinit neutraliser`."
            )
            return
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(
            "Coffre M Corp initialisé et verrouillé contre toute réinitialisation.\n"
            f"Coffre Red : {report.vault_balance} {currency}\n"
            f"Ledger total : {report.ledger_total} crédits\n"
            f"État : **{'OK' if report.is_balanced else 'ANOMALIE'}**"
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def bankcheck(self, ctx: commands.Context) -> None:
        """[Propriétaire] Vérifie que le coffre Red couvre le ledger M Corp."""
        institution_id, vault = await self._bank_context(ctx)
        report = await self.banking.reconcile(institution_id, vault)
        self.control.record_reconciliation_snapshot(
            institution_id, report.vault_balance, checked_by=str(ctx.author.id)
        )
        currency = await bank.get_currency_name(ctx.guild)
        state = "OK" if report.is_balanced else "ANOMALIE"
        await ctx.send(
            "**Réconciliation M Corp**\n"
            f"Coffre Red : {report.vault_balance} {currency}\n"
            f"Ledger total : {report.ledger_total} crédits\n"
            f"Différence : {report.difference} crédits\n\n"
            f"État : **{state}**"
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def economycheck(self, ctx: commands.Context) -> None:
        """[Propriétaire] Affiche les flux cumulés de la trésorerie M Corp."""
        institution_id, vault = await self._bank_context(ctx)
        try:
            report = await self.banking.economy_report(institution_id, vault)
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        self.control.record_reconciliation_snapshot(
            institution_id, report.reconciliation.vault_balance,
            checked_by=str(ctx.author.id),
        )
        currency = await bank.get_currency_name(ctx.guild)
        state = "OK" if report.reconciliation.is_balanced else "ANOMALIE"
        await ctx.send(
            "**Contrôle économique M Corp**\n"
            f"Trésorerie : {report.stats.treasury_balance} crédits\n"
            f"Entrées cumulées : {report.stats.total_received} crédits\n"
            f"Sorties cumulées : {report.stats.total_paid} crédits\n"
            f"Flux net : {report.stats.net_flow:+d} crédits\n"
            f"Opérations institutionnelles : {report.stats.operation_count}\n"
            f"Coffre Red : {report.reconciliation.vault_balance} {currency}\n"
            f"Ledger couvert : {report.reconciliation.ledger_total} crédits\n"
            f"Invariant : **{state}**"
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def itemgrant(
        self, ctx: commands.Context, membre: commands.MemberConverter, item_id: str, quantite: int = 1
    ) -> None:
        """[Propriétaire] Attribue un objet catalogué à un membre."""
        self._remember_member(membre)
        try:
            balance = self.inventory.add_item(
                self._user_owner(membre.id), item_id, quantite, administrator_id=ctx.author.id
            )
        except Exception as exc:
            await self._send_inventory_error(ctx, exc)
            return
        item = get_item(item_id)
        assert item is not None
        await ctx.send(f"{item.display_name} ×{quantite} attribué à {membre.mention}. Total : ×{balance}.")

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def itemremove(
        self, ctx: commands.Context, membre: commands.MemberConverter, item_id: str, quantite: int = 1
    ) -> None:
        """[Propriétaire] Retire un objet catalogué à un membre."""
        self._remember_member(membre)
        try:
            balance = self.inventory.remove_item(
                self._user_owner(membre.id), item_id, quantite, administrator_id=ctx.author.id
            )
        except Exception as exc:
            await self._send_inventory_error(ctx, exc)
            return
        item = get_item(item_id)
        assert item is not None
        await ctx.send(f"{item.display_name} ×{quantite} retiré à {membre.mention}. Total : ×{balance}.")

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def iteminspect(self, ctx: commands.Context, membre: commands.MemberConverter) -> None:
        """[Propriétaire] Consulte l'inventaire d'un membre."""
        await self._send_inventory(ctx, membre)

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shopcreate(
        self,
        ctx: commands.Context,
        shop_id: str,
        proprietaire: commands.MemberConverter,
        *,
        nom: str,
    ) -> None:
        """[Propriétaire] Crée une boutique de test administrable."""
        self._remember_member(proprietaire)
        institution_id, _ = await self._bank_context(ctx)
        try:
            shop = self.control.create_shop(
                institution_id,
                shop_id.upper(),
                nom,
                "USER",
                str(proprietaire.id),
                administrator_id=ctx.author.id,
                license_required=True,
                license_fee=0,
                auto_restock_allowed=True,
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        await ctx.send(
            f"Boutique **{shop.display_name}** créée (`{shop.shop_id}`). "
            f"Statut : `{shop.status}`."
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shopinfo(self, ctx: commands.Context, shop_id: str) -> None:
        """[Propriétaire] Affiche l'état économique d'une boutique."""
        institution_id, _ = await self._bank_context(ctx)
        try:
            report = self.control.shop_report(institution_id, shop_id.upper())
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        stock = ", ".join(f"{item} ×{quantity}" for item, quantity in report.inventory.items())
        await ctx.send(
            f"**Boutique — {report.shop.display_name}** (`{report.shop.shop_id}`)\n"
            f"Statut : `{report.shop.status}`\n"
            f"Licence : `{report.shop.license_status}`\n"
            f"Prochaine échéance : {report.shop.next_license_due_at or 'aucune'}\n"
            f"Compte bancaire : {report.bank_balance} crédits\n"
            f"Commission : {report.shop.commission_bps / 100:.2f} %\n"
            f"Frais de restock : {report.shop.restock_fee_bps / 100:.2f} %\n"
            f"Stock : {stock or 'vide'}\n"
            f"Catalogue autorisé : {', '.join(report.wholesale_items) or 'aucun'}"
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shoplicense(self, ctx: commands.Context, shop_id: str) -> None:
        """[Propriétaire] Déclenche le paiement manuel d'une licence."""
        institution_id, _ = await self._bank_context(ctx)
        try:
            shop = self.control.pay_license(
                institution_id, shop_id.upper(), administrator_id=ctx.author.id
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        await ctx.send(
            f"Licence de `{shop.shop_id}` active. Échéance : "
            f"{shop.next_license_due_at or 'non applicable'}."
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shopfund(self, ctx: commands.Context, shop_id: str, montant: int) -> None:
        """[Propriétaire] Alloue des crédits couverts de la trésorerie à une boutique."""
        institution_id, _ = await self._bank_context(ctx)
        try:
            treasury, shop_balance = self.control.fund_shop(
                institution_id, shop_id.upper(), montant, administrator_id=ctx.author.id
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        await ctx.send(
            f"`{shop_id.upper()}` financée de {montant} crédits. "
            f"Compte boutique : {shop_balance}; trésorerie : {treasury}."
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shopsetprice(
        self, ctx: commands.Context, item_id: str, prix: int, *, raison: str | None = None
    ) -> None:
        """[Propriétaire] Définit un prix grossiste historisé."""
        institution_id, _ = await self._bank_context(ctx)
        try:
            item = self.control.set_wholesale_item(
                institution_id, item_id, prix, administrator_id=ctx.author.id, reason=raison
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        await ctx.send(f"Prix grossiste de `{item.item_id}` : {item.wholesale_price} crédits.")

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shopsetsaleprice(
        self, ctx: commands.Context, shop_id: str, item_id: str, prix: int
    ) -> None:
        """[Propriétaire] Définit le prix joueur d'un article de boutique."""
        institution_id, _ = await self._bank_context(ctx)
        try:
            item = self.control.set_sale_price(
                institution_id, shop_id.upper(), item_id, prix, administrator_id=ctx.author.id
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        await ctx.send(
            f"Prix joueur de `{item.item_id}` dans `{item.shop_id}` : {item.unit_price} crédits."
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shopallowitem(self, ctx: commands.Context, shop_id: str, item_id: str) -> None:
        """[Propriétaire] Autorise un objet grossiste pour une boutique."""
        await self._set_shop_item_permission(ctx, shop_id, item_id, True)

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shopdenyitem(self, ctx: commands.Context, shop_id: str, item_id: str) -> None:
        """[Propriétaire] Interdit un objet grossiste pour une boutique."""
        await self._set_shop_item_permission(ctx, shop_id, item_id, False)

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shopsetrestock(
        self,
        ctx: commands.Context,
        shop_id: str,
        item_id: str,
        seuil: int,
        cible: int,
        prix_max: int,
        budget_max: int,
    ) -> None:
        """[Propriétaire] Configure la protection d'auto-restock d'un objet."""
        institution_id, _ = await self._bank_context(ctx)
        try:
            policy = self.control.set_restock_policy(
                institution_id, shop_id.upper(), item_id, enabled=True,
                threshold=seuil, target_stock=cible, max_unit_price=prix_max,
                max_total_spend=budget_max,
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        await ctx.send(
            f"Restock `{policy.shop_id}/{policy.item_id}` activé : "
            f"seuil {policy.threshold}, cible {policy.target_stock}."
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def shoprestock(self, ctx: commands.Context, shop_id: str, item_id: str) -> None:
        """[Propriétaire] Déclenche manuellement un restock protégé."""
        institution_id, _ = await self._bank_context(ctx)
        try:
            quote = self.control.restock(
                institution_id, shop_id.upper(), item_id, administrator_id=ctx.author.id
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        await ctx.send(
            f"Restock terminé : `{quote.item_id}` ×{quote.quantity}. "
            f"Stock {quote.stock_cost} + frais {quote.fee} = {quote.total_cost} crédits."
        )

    @commands.command()
    @commands.guild_only()
    @commands.is_owner()
    async def produce(self, ctx: commands.Context, item_id: str, quantite: int) -> None:
        """[Propriétaire] Produit un objet dans l'entrepôt M Corp."""
        institution_id, _ = await self._bank_context(ctx)
        try:
            stock = self.control.produce_item(
                institution_id, item_id, quantite, administrator_id=ctx.author.id
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        await ctx.send(f"Production : `{item_id.casefold()}` ×{quantite}. Stock M Corp : ×{stock}.")

    async def _bank_context(self, ctx: commands.Context) -> tuple[str, Any]:
        """Return the Red vault member and ledger institution for this context."""
        if ctx.guild is None or self.bot.user is None:
            raise commands.CommandError("Cette commande doit être utilisée sur un serveur.")
        vault = ctx.guild.me or ctx.guild.get_member(self.bot.user.id)
        if vault is None:
            raise commands.CommandError("Le membre-bot M Corp est introuvable sur ce serveur.")
        if await bank.is_global():
            institution_id = "mcorp:global"
            institution_name = "M Corp (banque Red globale)"
        else:
            institution_id = f"mcorp:guild:{ctx.guild.id}"
            institution_name = f"M Corp — {ctx.guild.name}"
        self.banking.ensure_institution(institution_id, institution_name, vault.id)
        self.control.ensure_official_shop(institution_id)
        return institution_id, vault

    async def _set_shop_item_permission(
        self, ctx: commands.Context, shop_id: str, item_id: str, allowed: bool
    ) -> None:
        institution_id, _ = await self._bank_context(ctx)
        try:
            self.control.set_item_permission(
                institution_id, shop_id.upper(), item_id, allowed,
                administrator_id=ctx.author.id,
            )
        except Exception as exc:
            await self._send_control_error(ctx, exc)
            return
        state = "autorisé" if allowed else "interdit"
        await ctx.send(f"Objet `{item_id.casefold()}` {state} pour `{shop_id.upper()}`.")

    async def _send_inventory(self, ctx: commands.Context, member: Any) -> None:
        """Combine the Red cash display with the distinct inventory source."""
        self._remember_member(member)
        wallet = await bank.get_balance(member)
        currency = await bank.get_currency_name(ctx.guild)
        entries = self.inventory.get_inventory(self._user_owner(member.id))
        if entries:
            lines = []
            for entry in entries:
                item = get_item(entry.item_id)
                display_name = entry.item_id if item is None else item.display_name
                lines.append(f"- {display_name} ×{entry.quantity}")
            objects = "\n".join(lines)
        else:
            objects = "Aucun objet."
        await ctx.send(
            f"**🎒 Inventaire de {member.display_name}**\n\n"
            f"💵 Liquide : {wallet} {currency}\n"
            "*(Le liquide reste stocké dans Red Bank.)*\n\n"
            f"**Objets :**\n{objects}"
        )

    def _remember_member(self, member: Any) -> None:
        """Persist a member name already available to an M Corp command."""
        discord_id = getattr(member, "id", None)
        display_name = getattr(member, "display_name", None)
        if discord_id is None or not isinstance(display_name, str):
            return
        self.identity_store.remember_known_nickname(str(discord_id), display_name)

    @staticmethod
    def _parse_cash_amount(token: str) -> int | None:
        """Parse the unambiguous `100R` direct-cash syntax, or return None."""
        match = re.fullmatch(r"([0-9]+)[rR]", token)
        return None if match is None else int(match.group(1))

    @staticmethod
    def _user_owner(user_id: int) -> InventoryOwner:
        return InventoryOwner(USER_OWNER_TYPE, str(user_id))

    @staticmethod
    async def _red_default_balance(vault: Any) -> int:
        """Read Red's public configured default for this vault's bank scope."""
        if await bank.is_global():
            return await bank.get_default_balance()
        return await bank.get_default_balance(vault.guild)

    @staticmethod
    async def _send_operation_error(ctx: commands.Context, exc: Exception) -> None:
        if isinstance(exc, InvalidAmount):
            await ctx.send("Le montant doit être strictement supérieur à 0.")
        elif isinstance(exc, InsufficientCash):
            await ctx.send("Votre portefeuille Red ne contient pas assez de crédits.")
        elif isinstance(exc, InsufficientLedgerFunds):
            await ctx.send(f"Votre compte bancaire ne contient que {exc.balance} crédits.")
        elif isinstance(exc, SameAccountTransfer):
            await ctx.send("Vous ne pouvez pas effectuer un paiement ou virement vers vous-même.")
        elif isinstance(exc, RecoveryRequired):
            await ctx.send(
                "Opération interrompue : une compensation a échoué. "
                "Un propriétaire doit exécuter `m!bankcheck`."
            )
        elif isinstance(exc, InvariantViolation):
            await ctx.send(
                "Opération refusée : le coffre M Corp et le ledger sont déséquilibrés "
                f"({exc.report.difference:+d} crédits). Un propriétaire doit exécuter `m!bankcheck`."
            )
        elif isinstance(exc, InstitutionNotInitialized):
            await ctx.send(
                "Opération bancaire couverte refusée : M Corp n'est pas initialisée. "
                "Un propriétaire doit d'abord exécuter `m!bankinit`."
            )
        elif isinstance(exc, AlreadyInitialized):
            await ctx.send("Initialisation refusée : cette institution M Corp est déjà initialisée.")
        elif isinstance(exc, InitializationRefused):
            await ctx.send(
                "Initialisation refusée : le coffre ou le ledger ne permet pas de prouver "
                "qu'une remise à zéro est sûre. Exécutez `m!bankcheck` et corrigez manuellement."
            )
        elif isinstance(exc, BankingOperationError):
            await ctx.send("Opération annulée : aucun changement comptable n'a été confirmé.")
        else:
            raise exc

    @staticmethod
    async def _send_inventory_error(ctx: commands.Context, exc: Exception) -> None:
        if isinstance(exc, UnknownItem):
            await ctx.send("Objet inconnu : utilisez un identifiant du catalogue M Corp.")
        elif isinstance(exc, ItemNotTransferable):
            await ctx.send("Cet objet ne peut pas être transféré.")
        elif isinstance(exc, InvalidItemQuantity):
            await ctx.send("La quantité d'objet doit être strictement supérieure à 0.")
        elif isinstance(exc, NonStackableQuantity):
            await ctx.send("Cet objet ne peut être manipulé qu'à l'unité.")
        elif isinstance(exc, InventoryCapacityExceeded):
            await ctx.send("Cet objet non empilable est déjà présent dans cet inventaire.")
        elif isinstance(exc, InsufficientInventory):
            await ctx.send(f"Vous ne possédez que {exc.available} exemplaire(s) de cet objet.")
        elif isinstance(exc, SameInventoryOwner):
            await ctx.send("Vous ne pouvez pas transférer un objet vers le même inventaire.")
        else:
            raise exc

    @staticmethod
    async def _send_purchase_error(ctx: commands.Context, exc: Exception) -> None:
        if isinstance(exc, PurchaseInsufficientFunds):
            await ctx.send("Votre compte M Bank ne contient pas assez de RTC.")
        elif isinstance(exc, PurchaseInsufficientStock):
            await ctx.send("Le stock a changé : quantité insuffisante.")
        elif isinstance(exc, PurchasePriceChanged):
            await ctx.send("Le prix a changé : consultez de nouveau la boutique avant de confirmer.")
        elif isinstance(exc, PurchasePaymentNotAllowed):
            await ctx.send("Ce moyen de paiement n'est pas accepté par la boutique.")
        elif isinstance(exc, PurchaseError):
            await ctx.send("Cet achat n'est plus disponible.")
        else:
            await MCorp._send_operation_error(ctx, exc)

    @staticmethod
    async def _send_control_error(ctx: commands.Context, exc: Exception) -> None:
        if isinstance(exc, ShopNotFound):
            await ctx.send("Boutique introuvable.")
        elif isinstance(exc, ShopAlreadyExists):
            await ctx.send("Une boutique utilise déjà cet identifiant.")
        elif isinstance(exc, InvalidControlValue):
            await ctx.send("Paramètre économique invalide.")
        elif isinstance(exc, ShopUnavailable):
            await ctx.send("La boutique n'est pas active ; vérifiez sa licence et son statut.")
        elif isinstance(exc, ShopItemDenied):
            await ctx.send("Cet objet n'est pas autorisé pour cette boutique.")
        elif isinstance(exc, (AutoRestockNotAllowed, AutoRestockDisabled)):
            await ctx.send("Le réapprovisionnement automatique n'est pas actif pour cette règle.")
        elif isinstance(exc, RestockThresholdNotReached):
            await ctx.send("Le stock est encore supérieur au seuil de réapprovisionnement.")
        elif isinstance(exc, WholesaleUnavailable):
            await ctx.send("Cet objet n'est pas disponible au catalogue grossiste.")
        elif isinstance(exc, WholesaleOrderLimit):
            await ctx.send("La quantité calculée ne respecte pas les limites grossistes.")
        elif isinstance(exc, UnitPriceLimitExceeded):
            await ctx.send("Le prix grossiste dépasse le prix unitaire maximal autorisé.")
        elif isinstance(exc, RestockBudgetExceeded):
            await ctx.send("Le budget de réapprovisionnement de la période serait dépassé.")
        elif isinstance(exc, ShopFundsInsufficient):
            await ctx.send("Le compte bancaire de la boutique est insuffisant.")
        elif isinstance(exc, InsufficientLedgerFunds):
            await ctx.send(f"Le compte bancaire concerné ne contient que {exc.balance} crédits.")
        elif isinstance(exc, WarehouseStockInsufficient):
            await ctx.send(
                f"Stock M Corp insuffisant pour `{exc.item_id}` : "
                f"{exc.available} disponible(s), {exc.requested} requis."
            )
        elif isinstance(exc, ControlRecoveryRequired):
            await ctx.send("Compensation incomplète : intervention et audit administrateur requis.")
        elif isinstance(exc, ControlOperationFailed):
            await ctx.send("Opération annulée et compensée après une erreur interne.")
        else:
            await MCorp._send_inventory_error(ctx, exc)
