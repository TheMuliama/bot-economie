"""Application service coordinating Red Bank cash and the M Corp ledger."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from .ledger import (
    EconomyStats,
    InsufficientLedgerFunds,
    InvalidAmount,
    LedgerStore,
    LedgerTransaction,
    SameAccountTransfer,
)


class BankGateway(Protocol):
    async def get_balance(self, member: Any) -> int: ...

    async def withdraw(self, member: Any, amount: int) -> int: ...

    async def deposit(self, member: Any, amount: int) -> int: ...

    async def set_balance(self, member: Any, amount: int) -> int: ...


@dataclass(frozen=True)
class Reconciliation:
    vault_balance: int
    ledger_total: int

    @property
    def difference(self) -> int:
        return self.vault_balance - self.ledger_total

    @property
    def is_balanced(self) -> bool:
        return self.difference == 0


@dataclass(frozen=True)
class EconomyReport:
    stats: EconomyStats
    reconciliation: Reconciliation


class BankingOperationError(Exception):
    """An expected banking operation could not be completed."""


class InsufficientCash(BankingOperationError):
    pass


class RecoveryRequired(BankingOperationError):
    """Compensation failed; an administrator must reconcile the vault."""


class InvariantViolation(BankingOperationError):
    """The Red vault does not cover the ledger before an operation starts."""

    def __init__(self, report: Reconciliation) -> None:
        self.report = report
        super().__init__("The M Corp vault and ledger are not balanced.")


class InstitutionNotInitialized(BankingOperationError):
    """Covered banking is disabled until the vault is explicitly initialized."""


class InitializationRefused(BankingOperationError):
    """The vault state cannot be proved safe to initialize."""


class AlreadyInitialized(InitializationRefused):
    pass


class InitialVaultBalanceRequiresConfirmation(InitializationRefused):
    """An exact Red default balance requires an explicit owner confirmation."""


class InitializationRecoveryRequired(RecoveryRequired):
    """The vault reset could not be compensated after persisting failed."""


class BankingService:
    """Serializes and compensates operations spanning Red Bank and SQLite.

    Red Bank and SQLite cannot form a distributed transaction. Each operation
    validates first, performs the cash movement, then commits its ledger entry.
    If the final ledger write fails, it attempts the inverse cash movement. A
    failed compensation is explicitly journaled and surfaced to the caller.
    """

    def __init__(self, ledger: LedgerStore, bank: BankGateway) -> None:
        self.ledger = ledger
        self.bank = bank
        self._operation_lock = asyncio.Lock()

    def ensure_institution(
        self, institution_id: str, display_name: str, vault_owner_id: int
    ) -> None:
        """Ensure the ledger scope used by the banking interface exists."""
        self.ledger.ensure_institution(institution_id, display_name, vault_owner_id)

    def account_balance(self, institution_id: str, user_id: int) -> int:
        return self.ledger.get_user_balance(institution_id, user_id)

    def recent_transactions(
        self, institution_id: str, user_id: int, limit: int = 10
    ) -> list[LedgerTransaction]:
        return self.ledger.recent_banking_transactions(institution_id, user_id, limit)

    async def deposit(
        self, institution_id: str, player: Any, vault: Any, amount: int
    ) -> int:
        self._require_positive_amount(amount)
        async with self._operation_lock:
            self._require_initialized(institution_id)
            await self._require_invariant(institution_id, vault)
            await self._require_cash(player, amount)
            await self._move_cash(player, vault, amount)
            try:
                return self.ledger.record_deposit(
                    institution_id,
                    player.id,
                    amount,
                    {"cash_source": "red_bank", "vault_owner_id": vault.id},
                )
            except Exception as exc:
                await self._compensate_or_raise(
                    institution_id,
                    "CASH_DEPOSIT",
                    amount,
                    (player.id,),
                    source=vault,
                    destination=player,
                    original_error=exc,
                )
                raise BankingOperationError("Le dépôt a été annulé après une erreur interne.") from exc

    async def withdraw(
        self, institution_id: str, player: Any, vault: Any, amount: int
    ) -> int:
        self._require_positive_amount(amount)
        async with self._operation_lock:
            self._require_initialized(institution_id)
            await self._require_invariant(institution_id, vault)
            ledger_balance = self.ledger.get_user_balance(institution_id, player.id)
            if ledger_balance < amount:
                raise InsufficientLedgerFunds(ledger_balance, amount)
            await self._require_cash(vault, amount)
            await self._move_cash(vault, player, amount)
            try:
                return self.ledger.record_withdrawal(
                    institution_id,
                    player.id,
                    amount,
                    {"cash_destination": "red_bank", "vault_owner_id": vault.id},
                )
            except Exception as exc:
                await self._compensate_or_raise(
                    institution_id,
                    "CASH_WITHDRAWAL",
                    amount,
                    (player.id,),
                    source=player,
                    destination=vault,
                    original_error=exc,
                )
                raise BankingOperationError("Le retrait a été annulé après une erreur interne.") from exc

    async def bank_transfer(
        self, institution_id: str, source: Any, destination: Any, vault: Any, amount: int
    ) -> tuple[int, int]:
        self._require_positive_amount(amount)
        async with self._operation_lock:
            self._require_initialized(institution_id)
            await self._require_invariant(institution_id, vault)
            return self.ledger.record_transfer(
                institution_id,
                source.id,
                destination.id,
                amount,
                {"initiator_id": source.id},
            )

    async def pay_treasury(
        self, institution_id: str, player: Any, vault: Any, amount: int
    ) -> tuple[int, int]:
        """Move covered credits from a player account to M Corp treasury."""
        self._require_positive_amount(amount)
        async with self._operation_lock:
            self._require_initialized(institution_id)
            await self._require_invariant(institution_id, vault)
            return self.ledger.record_player_to_treasury(
                institution_id,
                player.id,
                amount,
                {"initiator_id": player.id},
            )

    async def pay_from_treasury(
        self, institution_id: str, recipient: Any, vault: Any, amount: int, *, administrator: Any
    ) -> tuple[int, int]:
        """Move covered treasury credits to a player without touching Red Bank."""
        self._require_positive_amount(amount)
        async with self._operation_lock:
            self._require_initialized(institution_id)
            await self._require_invariant(institution_id, vault)
            return self.ledger.record_treasury_to_player(
                institution_id,
                recipient.id,
                amount,
                {"administrator_id": administrator.id, "recipient_id": recipient.id},
            )

    async def cash_transfer(
        self, institution_id: str, source: Any, destination: Any, amount: int
    ) -> None:
        self._require_positive_amount(amount)
        if source.id == destination.id:
            raise SameAccountTransfer()
        async with self._operation_lock:
            await self._require_cash(source, amount)
            await self._move_cash(source, destination, amount)
            try:
                self.ledger.record_cash_event(
                    institution_id,
                    "PLAYER_CASH_TRANSFER",
                    amount,
                    (source.id, destination.id),
                    {"source_owner_id": source.id, "destination_owner_id": destination.id},
                )
            except Exception as exc:
                await self._compensate_or_raise(
                    institution_id,
                    "PLAYER_CASH_TRANSFER",
                    amount,
                    (source.id, destination.id),
                    source=destination,
                    destination=source,
                    original_error=exc,
                )
                raise BankingOperationError("Le paiement a été annulé après une erreur interne.") from exc

    async def admin_cash_credit(
        self, institution_id: str, administrator: Any, member: Any, amount: int
    ) -> int:
        self._require_positive_amount(amount)
        async with self._operation_lock:
            balance = await self.bank.deposit(member, amount)
            try:
                self.ledger.record_cash_event(
                    institution_id,
                    "ADMIN_CREDIT",
                    amount,
                    (administrator.id, member.id),
                    {"administrator_id": administrator.id, "member_id": member.id},
                )
            except Exception as exc:
                await self._compensate_admin_or_raise(
                    institution_id,
                    "ADMIN_CREDIT",
                    amount,
                    (administrator.id, member.id),
                    member,
                    reverse_withdrawal=True,
                    original_error=exc,
                )
                raise BankingOperationError("Le crédit administratif a été annulé.") from exc
            return balance

    async def admin_cash_debit(
        self, institution_id: str, administrator: Any, member: Any, amount: int
    ) -> int:
        self._require_positive_amount(amount)
        async with self._operation_lock:
            await self._require_cash(member, amount)
            balance = await self.bank.withdraw(member, amount)
            try:
                self.ledger.record_cash_event(
                    institution_id,
                    "ADMIN_DEBIT",
                    amount,
                    (administrator.id, member.id),
                    {"administrator_id": administrator.id, "member_id": member.id},
                )
            except Exception as exc:
                await self._compensate_admin_or_raise(
                    institution_id,
                    "ADMIN_DEBIT",
                    amount,
                    (administrator.id, member.id),
                    member,
                    reverse_withdrawal=False,
                    original_error=exc,
                )
                raise BankingOperationError("Le débit administratif a été annulé.") from exc
            return balance

    async def reconcile(self, institution_id: str, vault: Any) -> Reconciliation:
        async with self._operation_lock:
            return Reconciliation(
                vault_balance=await self.bank.get_balance(vault),
                ledger_total=self.ledger.total_cash_backed_balance(institution_id),
            )

    async def economy_report(self, institution_id: str, vault: Any) -> EconomyReport:
        async with self._operation_lock:
            self._require_initialized(institution_id)
            reconciliation = Reconciliation(
                vault_balance=await self.bank.get_balance(vault),
                ledger_total=self.ledger.total_cash_backed_balance(institution_id),
            )
            return EconomyReport(
                stats=self.ledger.economy_stats(institution_id),
                reconciliation=reconciliation,
            )

    async def initialize_institution(
        self,
        institution_id: str,
        vault: Any,
        *,
        default_vault_balance: int,
        confirm_default_balance_reset: bool = False,
    ) -> Reconciliation:
        """Safely establish a zero-backed M Corp institution exactly once.

        Red creates/accesses accounts with its configured default balance.  We
        only clear that balance if the ledger is entirely virgin, it exactly
        equals Red's current default, and the owner explicitly confirms it.
        """
        async with self._operation_lock:
            if self.ledger.get_initialization(institution_id) is not None:
                raise AlreadyInitialized()
            ledger_total = self.ledger.total_cash_backed_balance(institution_id)
            if ledger_total != 0:
                raise InitializationRefused("Le ledger contient déjà des soldes.")
            if self.ledger.transaction_count(institution_id) != 0:
                raise InitializationRefused("Le ledger contient déjà des opérations.")

            initial_vault_balance = await self.bank.get_balance(vault)
            if initial_vault_balance != 0:
                if initial_vault_balance != default_vault_balance:
                    raise InitializationRefused(
                        "Le coffre ne correspond pas au solde par défaut Red actuel."
                    )
                if not confirm_default_balance_reset:
                    raise InitialVaultBalanceRequiresConfirmation()
                try:
                    result = await self.bank.set_balance(vault, 0)
                    confirmed_balance = await self.bank.get_balance(vault)
                except Exception as exc:
                    raise InitializationRecoveryRequired(
                        "Red Bank n'a pas confirmé la neutralisation du coffre."
                    ) from exc
                if result != 0 or confirmed_balance != 0:
                    raise InitializationRefused("Red Bank n'a pas confirmé la remise à zéro du coffre.")

            try:
                self.ledger.mark_initialized(institution_id, vault.id, initial_vault_balance)
            except Exception as persistence_error:
                if initial_vault_balance:
                    try:
                        await self.bank.set_balance(vault, initial_vault_balance)
                    except Exception as compensation_error:
                        raise InitializationRecoveryRequired(
                            "La neutralisation du coffre n'a pas pu être compensée."
                        ) from compensation_error
                raise BankingOperationError(
                    "L'initialisation a été annulée avant sa confirmation durable."
                ) from persistence_error

            return Reconciliation(
                vault_balance=await self.bank.get_balance(vault),
                ledger_total=self.ledger.total_cash_backed_balance(institution_id),
            )

    @staticmethod
    def _require_positive_amount(amount: int) -> None:
        if not isinstance(amount, int) or amount <= 0:
            raise InvalidAmount()

    async def _require_cash(self, member: Any, amount: int) -> None:
        if await self.bank.get_balance(member) < amount:
            raise InsufficientCash()

    async def _require_invariant(self, institution_id: str, vault: Any) -> None:
        report = Reconciliation(
            vault_balance=await self.bank.get_balance(vault),
            ledger_total=self.ledger.total_cash_backed_balance(institution_id),
        )
        if not report.is_balanced:
            raise InvariantViolation(report)

    def _require_initialized(self, institution_id: str) -> None:
        if self.ledger.get_initialization(institution_id) is None:
            raise InstitutionNotInitialized()

    async def _move_cash(self, source: Any, destination: Any, amount: int) -> None:
        """Move Red credits and reverse the debit if destination crediting fails."""
        await self.bank.withdraw(source, amount)
        try:
            await self.bank.deposit(destination, amount)
        except Exception as destination_error:
            try:
                await self.bank.deposit(source, amount)
            except Exception as compensation_error:
                raise RecoveryRequired(
                    "Le transfert Red est partiel et exige une réconciliation manuelle."
                ) from compensation_error
            raise BankingOperationError("Le transfert Red a été annulé.") from destination_error

    async def _compensate_or_raise(
        self,
        institution_id: str,
        transaction_type: str,
        amount: int,
        participants: tuple[int, ...],
        *,
        source: Any,
        destination: Any,
        original_error: Exception,
    ) -> None:
        try:
            await self._move_cash(source, destination, amount)
        except Exception as compensation_error:
            try:
                self.ledger.record_recovery_required(
                    institution_id,
                    transaction_type,
                    amount,
                    participants,
                    {
                        "original_error": type(original_error).__name__,
                        "compensation_error": type(compensation_error).__name__,
                    },
                )
            finally:
                raise RecoveryRequired(
                    "Une compensation a échoué : exécutez m!bankcheck et corrigez manuellement."
                ) from compensation_error

    async def _compensate_admin_or_raise(
        self,
        institution_id: str,
        transaction_type: str,
        amount: int,
        participants: tuple[int, ...],
        member: Any,
        *,
        reverse_withdrawal: bool,
        original_error: Exception,
    ) -> None:
        """Reverse a standalone Red cash mint/burn after journal failure."""
        try:
            if reverse_withdrawal:
                await self.bank.withdraw(member, amount)
            else:
                await self.bank.deposit(member, amount)
        except Exception as compensation_error:
            try:
                self.ledger.record_recovery_required(
                    institution_id,
                    transaction_type,
                    amount,
                    participants,
                    {
                        "original_error": type(original_error).__name__,
                        "compensation_error": type(compensation_error).__name__,
                    },
                )
            finally:
                raise RecoveryRequired(
                    "Une compensation administrative a échoué : vérifiez les journaux Red Bank."
                ) from compensation_error
