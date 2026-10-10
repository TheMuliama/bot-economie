"""Discord commands for the M Corp covered-banking MVP."""

from __future__ import annotations

import re
from typing import Any

from redbot.core import bank, commands
from redbot.core.data_manager import cog_data_path

from .ledger import InsufficientLedgerFunds, InvalidAmount, LedgerStore, SameAccountTransfer
from .catalog import get_item
from .inventory import (
    InsufficientInventory,
    InvalidItemQuantity,
    InventoryCapacityExceeded,
    InventoryOwner,
    InventoryStore,
    SameInventoryOwner,
    USER_OWNER_TYPE,
)
from .inventory_service import (
    InventoryService,
    ItemNotTransferable,
    NonStackableQuantity,
    UnknownItem,
)
from .service import (
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


class RedBankGateway:
    """Thin adapter around Red's public Bank API."""

    async def get_balance(self, member: Any) -> int:
        return await bank.get_balance(member)

    async def withdraw(self, member: Any, amount: int) -> int:
        return await bank.withdraw_credits(member, amount)

    async def deposit(self, member: Any, amount: int) -> int:
        return await bank.deposit_credits(member, amount)

    async def set_balance(self, member: Any, amount: int) -> int:
        return await bank.set_balance(member, amount)


class MCorp(commands.Cog):
    """Covered bank accounts backed one-for-one by the M Corp Red vault."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.ledger = LedgerStore(cog_data_path(self) / "ledger.sqlite3")
        self.banking = BankingService(self.ledger, RedBankGateway())
        self.inventory_store = InventoryStore(self.ledger.database_path)
        self.inventory = InventoryService(self.inventory_store)

    async def cog_load(self) -> None:
        # The schema creation is small and happens once at cog load. Keeping it
        # synchronous also avoids executor lifecycle issues during Red startup.
        self.ledger.initialize()
        self.inventory_store.initialize()

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
        account = self.ledger.get_user_balance(institution_id, ctx.author.id)
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
        entries = self.ledger.recent_transactions(institution_id, ctx.author.id)
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
        self.ledger.ensure_institution(institution_id, institution_name, vault.id)
        return institution_id, vault

    async def _send_inventory(self, ctx: commands.Context, member: Any) -> None:
        """Combine the Red cash display with the distinct inventory source."""
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
