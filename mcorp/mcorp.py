"""Discord commands for the M Corp covered-banking MVP."""

from __future__ import annotations

from typing import Any

from redbot.core import bank, commands
from redbot.core.data_manager import cog_data_path

from .ledger import InsufficientLedgerFunds, InvalidAmount, LedgerStore, SameAccountTransfer
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

    async def cog_load(self) -> None:
        # The schema creation is small and happens once at cog load. Keeping it
        # synchronous also avoids executor lifecycle issues during Red startup.
        self.ledger.initialize()

    @commands.command()
    async def mcorp(self, ctx: commands.Context) -> None:
        """Vérifie que le cog M Corp est chargé."""
        await ctx.send("M Corp est opérationnelle.")

    @commands.command()
    async def solde(self, ctx: commands.Context) -> None:
        """Affiche le portefeuille Red Bank de l'utilisateur."""
        balance = await bank.get_balance(ctx.author)
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(f"{ctx.author.mention}, votre portefeuille Red contient {balance} {currency}.")

    @commands.command()
    @commands.guild_only()
    async def compte(self, ctx: commands.Context) -> None:
        """Affiche le portefeuille Red et le compte bancaire M Corp."""
        institution_id, vault = await self._bank_context(ctx)
        wallet = await bank.get_balance(ctx.author)
        account = self.ledger.get_user_balance(institution_id, ctx.author.id)
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(
            f"**Compte M Corp — {ctx.author.display_name}**\n"
            f"Portefeuille Red : {wallet} {currency}\n"
            f"Compte bancaire : {account} crédits\n"
            f"Coffre M Corp : {vault.display_name}"
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
    async def payer(self, ctx: commands.Context, membre: commands.MemberConverter, montant: int) -> None:
        """Envoie des R-crédits directement à un membre via Red Bank."""
        institution_id, vault = await self._bank_context(ctx)
        if membre.id == vault.id:
            await ctx.send("Utilisez `m!depot` pour alimenter le coffre M Corp.")
            return
        try:
            await self.banking.cash_transfer(institution_id, ctx.author, membre, montant)
        except Exception as exc:
            await self._send_operation_error(ctx, exc)
            return
        currency = await bank.get_currency_name(ctx.guild)
        await ctx.send(f"{montant} {currency} envoyés à {membre.mention}.")

    @commands.command()
    @commands.guild_only()
    async def virement(self, ctx: commands.Context, membre: commands.MemberConverter, montant: int) -> None:
        """Transfère des crédits entre deux comptes bancaires M Corp."""
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
        """Transfère des crédits de votre compte bancaire vers M Corp."""
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
