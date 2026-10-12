"""Integrity tests for the M Corp banking application service."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcorp.ledger import (
    TREASURY_ACCOUNT_TYPE,
    InsufficientLedgerFunds,
    InvalidAmount,
    LedgerStore,
    SameAccountTransfer,
)
from mcorp.owners import SYSTEM_OWNER_TYPE
from mcorp.banking_service import (
    AlreadyInitialized,
    BankingOperationError,
    BankingService,
    InitialVaultBalanceRequiresConfirmation,
    InitializationRefused,
    InitializationRecoveryRequired,
    InstitutionNotInitialized,
    InvariantViolation,
    InsufficientCash,
)


INSTITUTION_ID = "mcorp:guild:1"


class Member:
    def __init__(self, member_id: int) -> None:
        self.id = member_id


class FakeBank:
    def __init__(self, balances: dict[int, int]) -> None:
        self.balances = balances.copy()
        self.fail_deposits_for: set[int] = set()
        self.fail_set_balance_for: set[int] = set()

    async def get_balance(self, member: Member) -> int:
        return self.balances.get(member.id, 0)

    async def withdraw(self, member: Member, amount: int) -> int:
        balance = await self.get_balance(member)
        if amount > balance:
            raise ValueError("insufficient cash")
        self.balances[member.id] = balance - amount
        return self.balances[member.id]

    async def deposit(self, member: Member, amount: int) -> int:
        if member.id in self.fail_deposits_for:
            raise RuntimeError("simulated deposit failure")
        self.balances[member.id] = await self.get_balance(member) + amount
        return self.balances[member.id]

    async def set_balance(self, member: Member, amount: int) -> int:
        if member.id in self.fail_set_balance_for:
            raise RuntimeError("simulated set-balance failure")
        self.balances[member.id] = amount
        return amount


class FailingDepositLedger(LedgerStore):
    def record_deposit(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated SQLite failure")


class FailingWithdrawalLedger(LedgerStore):
    def record_withdrawal(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated SQLite failure")


class BankingServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.ledger = LedgerStore(Path(self.tempdir.name) / "ledger.sqlite3")
        self.ledger.initialize()
        self.vault = Member(999)
        self.alice = Member(1)
        self.bob = Member(2)
        self.ledger.ensure_institution(INSTITUTION_ID, "M Corp test", self.vault.id)
        self.bank = FakeBank({self.alice.id: 1_000, self.bob.id: 200, self.vault.id: 0})
        self.service = BankingService(self.ledger, self.bank)
        self.ledger.mark_initialized(INSTITUTION_ID, self.vault.id, 0)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_valid_deposit_preserves_vault_invariant(self) -> None:
        balance = await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 400)

        self.assertEqual(balance, 400)
        self.assertEqual(self.bank.balances[self.alice.id], 600)
        self.assertEqual(self.bank.balances[self.vault.id], 400)
        report = await self.service.reconcile(INSTITUTION_ID, self.vault)
        self.assertTrue(report.is_balanced)
        self.assertEqual(report.ledger_total, 400)

    async def test_deposit_rejects_insufficient_cash(self) -> None:
        with self.assertRaises(InsufficientCash):
            await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 1_001)

        self.assertEqual(self.ledger.total_balance(INSTITUTION_ID), 0)
        self.assertEqual(self.bank.balances[self.alice.id], 1_000)

    async def test_deposit_rejects_zero_and_negative_amounts(self) -> None:
        for amount in (0, -1):
            with self.assertRaises(InvalidAmount):
                await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, amount)

    async def test_valid_withdrawal_preserves_vault_invariant(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 400)
        balance = await self.service.withdraw(INSTITUTION_ID, self.alice, self.vault, 150)

        self.assertEqual(balance, 250)
        self.assertEqual(self.bank.balances[self.alice.id], 750)
        self.assertEqual(self.bank.balances[self.vault.id], 250)
        self.assertTrue((await self.service.reconcile(INSTITUTION_ID, self.vault)).is_balanced)

    async def test_withdrawal_rejects_amount_above_ledger_balance(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 100)

        with self.assertRaises(InsufficientLedgerFunds):
            await self.service.withdraw(INSTITUTION_ID, self.alice, self.vault, 101)

        self.assertEqual(self.ledger.get_user_balance(INSTITUTION_ID, self.alice.id), 100)
        self.assertEqual(self.bank.balances[self.vault.id], 100)

    async def test_cash_payment_to_self_is_rejected(self) -> None:
        with self.assertRaises(SameAccountTransfer):
            await self.service.cash_transfer(INSTITUTION_ID, self.alice, self.alice, 10)

    async def test_bank_transfer_conserves_ledger_total(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 500)
        total_before = self.ledger.total_balance(INSTITUTION_ID)

        source_balance, destination_balance = await self.service.bank_transfer(
            INSTITUTION_ID, self.alice, self.bob, self.vault, 125
        )

        self.assertEqual((source_balance, destination_balance), (375, 125))
        self.assertEqual(self.ledger.total_balance(INSTITUTION_ID), total_before)
        self.assertEqual(self.bank.balances[self.vault.id], total_before)
        self.assertTrue((await self.service.reconcile(INSTITUTION_ID, self.vault)).is_balanced)

    async def test_shop_account_transfers_preserve_vault_invariant(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 500)
        self.ledger.record_player_to_treasury(INSTITUTION_ID, self.alice.id, 500)
        self.ledger.record_treasury_to_shop(INSTITUTION_ID, "TEST_STORE", 300)
        self.ledger.record_shop_to_treasury(INSTITUTION_ID, "TEST_STORE", 100)

        report = await self.service.reconcile(INSTITUTION_ID, self.vault)

        self.assertTrue(report.is_balanced)
        self.assertEqual(report.vault_balance, 500)
        self.assertEqual(report.ledger_total, 500)

    async def test_initialized_institution_has_a_zero_covered_treasury(self) -> None:
        treasury = self.ledger.get_or_create_treasury_account(INSTITUTION_ID)

        self.assertEqual(treasury.balance, 0)
        self.assertEqual(treasury.owner_type, SYSTEM_OWNER_TYPE)
        self.assertEqual(treasury.account_type, TREASURY_ACCOUNT_TYPE)
        self.assertTrue(treasury.is_cash_backed)

    async def test_player_payment_to_treasury_conserves_vault_and_ledger(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 300)
        vault_before = self.bank.balances[self.vault.id]
        total_before = self.ledger.total_cash_backed_balance(INSTITUTION_ID)

        player_balance, treasury_balance = await self.service.pay_treasury(
            INSTITUTION_ID, self.alice, self.vault, 50
        )

        self.assertEqual((player_balance, treasury_balance), (250, 50))
        self.assertEqual(self.bank.balances[self.vault.id], vault_before)
        self.assertEqual(self.ledger.total_cash_backed_balance(INSTITUTION_ID), total_before)
        self.assertTrue((await self.service.reconcile(INSTITUTION_ID, self.vault)).is_balanced)
        entries = self.ledger.recent_transactions(INSTITUTION_ID, self.alice.id)
        self.assertEqual(entries[0].transaction_type, "PLAYER_TO_TREASURY")
        self.assertIsNotNone(entries[0].source_account_id)
        self.assertIsNotNone(entries[0].destination_account_id)

    async def test_player_payment_above_balance_is_refused(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 30)

        with self.assertRaises(InsufficientLedgerFunds):
            await self.service.pay_treasury(INSTITUTION_ID, self.alice, self.vault, 31)

        self.assertEqual(self.ledger.get_treasury_balance(INSTITUTION_ID), 0)

    async def test_treasury_payment_to_player_conserves_vault_and_ledger(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 300)
        await self.service.pay_treasury(INSTITUTION_ID, self.alice, self.vault, 100)
        vault_before = self.bank.balances[self.vault.id]
        total_before = self.ledger.total_cash_backed_balance(INSTITUTION_ID)

        treasury_balance, player_balance = await self.service.pay_from_treasury(
            INSTITUTION_ID, self.bob, self.vault, 40, administrator=self.alice
        )

        self.assertEqual((treasury_balance, player_balance), (60, 40))
        self.assertEqual(self.bank.balances[self.vault.id], vault_before)
        self.assertEqual(self.ledger.total_cash_backed_balance(INSTITUTION_ID), total_before)
        self.assertTrue((await self.service.reconcile(INSTITUTION_ID, self.vault)).is_balanced)
        entries = self.ledger.recent_transactions(INSTITUTION_ID, self.bob.id)
        self.assertEqual(entries[0].transaction_type, "TREASURY_TO_PLAYER")

    async def test_treasury_cannot_pay_above_its_balance(self) -> None:
        with self.assertRaises(InsufficientLedgerFunds):
            await self.service.pay_from_treasury(
                INSTITUTION_ID, self.alice, self.vault, 1, administrator=self.bob
            )

    async def test_institutional_payments_reject_zero_and_negative_amounts(self) -> None:
        for amount in (0, -1):
            with self.assertRaises(InvalidAmount):
                await self.service.pay_treasury(INSTITUTION_ID, self.alice, self.vault, amount)
            with self.assertRaises(InvalidAmount):
                await self.service.pay_from_treasury(
                    INSTITUTION_ID, self.alice, self.vault, amount, administrator=self.bob
                )

    async def test_economy_statistics_track_completed_institutional_flows(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 300)
        await self.service.pay_treasury(INSTITUTION_ID, self.alice, self.vault, 100)
        await self.service.pay_from_treasury(
            INSTITUTION_ID, self.bob, self.vault, 40, administrator=self.alice
        )

        report = await self.service.economy_report(INSTITUTION_ID, self.vault)

        self.assertEqual(report.stats.treasury_balance, 60)
        self.assertEqual(report.stats.total_received, 100)
        self.assertEqual(report.stats.total_paid, 40)
        self.assertEqual(report.stats.net_flow, 60)
        self.assertEqual(report.stats.operation_count, 2)
        self.assertTrue(report.reconciliation.is_balanced)

    async def test_institutional_payment_refuses_uninitialized_institution(self) -> None:
        institution_id = "mcorp:guild:uninitialized-treasury"
        self.ledger.ensure_institution(institution_id, "New M Corp", self.vault.id)

        with self.assertRaises(InstitutionNotInitialized):
            await self.service.pay_treasury(institution_id, self.alice, self.vault, 1)

    async def test_operations_refuse_a_preexisting_vault_anomaly(self) -> None:
        self.bank.balances[self.vault.id] = 10

        with self.assertRaises(InvariantViolation):
            await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 100)

        self.assertEqual(self.bank.balances[self.alice.id], 1_000)
        self.assertEqual(self.ledger.total_balance(INSTITUTION_ID), 0)

    async def test_covered_operations_refuse_an_uninitialized_institution(self) -> None:
        institution_id = "mcorp:guild:uninitialized"
        self.ledger.ensure_institution(institution_id, "New M Corp", self.vault.id)

        with self.assertRaises(InstitutionNotInitialized):
            await self.service.deposit(institution_id, self.alice, self.vault, 10)

    async def test_initial_default_balance_requires_explicit_confirmation(self) -> None:
        institution_id = "mcorp:guild:initial-default"
        self.ledger.ensure_institution(institution_id, "New M Corp", self.vault.id)
        self.bank.balances[self.vault.id] = 100

        with self.assertRaises(InitialVaultBalanceRequiresConfirmation):
            await self.service.initialize_institution(
                institution_id, self.vault, default_vault_balance=100
            )
        self.assertEqual(self.bank.balances[self.vault.id], 100)
        self.assertIsNone(self.ledger.get_initialization(institution_id))

    async def test_initialization_neutralizes_confirmed_default_and_reconciles(self) -> None:
        institution_id = "mcorp:guild:initialization"
        self.ledger.ensure_institution(institution_id, "New M Corp", self.vault.id)
        self.bank.balances[self.vault.id] = 100

        report = await self.service.initialize_institution(
            institution_id,
            self.vault,
            default_vault_balance=100,
            confirm_default_balance_reset=True,
        )

        self.assertTrue(report.is_balanced)
        self.assertEqual(self.bank.balances[self.vault.id], 0)
        self.assertEqual(self.ledger.get_initialization(institution_id).initial_vault_balance, 100)
        await self.service.deposit(institution_id, self.alice, self.vault, 20)
        self.assertTrue((await self.service.reconcile(institution_id, self.vault)).is_balanced)

    async def test_initialization_cannot_be_repeated_or_reset_on_restart(self) -> None:
        self.ledger = LedgerStore(Path(self.tempdir.name) / "restart.sqlite3")
        self.ledger.initialize()
        self.ledger.ensure_institution(INSTITUTION_ID, "M Corp test", self.vault.id)
        service = BankingService(self.ledger, self.bank)
        await service.initialize_institution(INSTITUTION_ID, self.vault, default_vault_balance=0)
        self.bank.balances[self.vault.id] = 25

        self.ledger.initialize()  # Represents a subsequent cog load.
        self.assertEqual(self.bank.balances[self.vault.id], 25)
        with self.assertRaises(AlreadyInitialized):
            await service.initialize_institution(INSTITUTION_ID, self.vault, default_vault_balance=0)

    async def test_initialization_refuses_existing_balance_or_transactions(self) -> None:
        institution_id = "mcorp:guild:history"
        self.ledger.ensure_institution(institution_id, "New M Corp", self.vault.id)
        self.ledger.record_deposit(institution_id, self.alice.id, 1)
        with self.assertRaises(InitializationRefused):
            await self.service.initialize_institution(institution_id, self.vault, default_vault_balance=0)

    async def test_initialization_refuses_unexplained_vault_balance(self) -> None:
        institution_id = "mcorp:guild:unexplained"
        self.ledger.ensure_institution(institution_id, "New M Corp", self.vault.id)
        self.bank.balances[self.vault.id] = 101
        with self.assertRaises(InitializationRefused):
            await self.service.initialize_institution(
                institution_id, self.vault, default_vault_balance=100, confirm_default_balance_reset=True
            )

    async def test_initialization_does_not_mark_when_red_reset_fails(self) -> None:
        institution_id = "mcorp:guild:red-failure"
        self.ledger.ensure_institution(institution_id, "New M Corp", self.vault.id)
        self.bank.balances[self.vault.id] = 100
        self.bank.fail_set_balance_for.add(self.vault.id)
        with self.assertRaises(InitializationRecoveryRequired):
            await self.service.initialize_institution(
                institution_id, self.vault, default_vault_balance=100, confirm_default_balance_reset=True
            )
        self.assertEqual(self.bank.balances[self.vault.id], 100)
        self.assertIsNone(self.ledger.get_initialization(institution_id))

    def test_existing_v1_database_receives_additive_initialization_migration(self) -> None:
        legacy = LedgerStore(Path(self.tempdir.name) / "legacy.sqlite3")
        legacy.database_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3

        with sqlite3.connect(legacy.database_path) as connection:
            connection.executescript("""
                CREATE TABLE institutions (institution_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    vault_owner_id TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE accounts (account_id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
                    owner_type TEXT NOT NULL, institution_id TEXT NOT NULL, account_type TEXT NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE');
                CREATE TABLE ledger_transactions (transaction_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL,
                    transaction_type TEXT NOT NULL, amount INTEGER NOT NULL, source_account_id INTEGER,
                    destination_account_id INTEGER, created_at TEXT NOT NULL, status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE transaction_participants (transaction_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    PRIMARY KEY (transaction_id, owner_id));
            """)
        legacy.initialize()
        self.assertIsNone(legacy.get_initialization(INSTITUTION_ID))

    def test_initialized_v1_accounts_are_migrated_as_cash_backed_without_data_loss(self) -> None:
        import sqlite3

        legacy = LedgerStore(Path(self.tempdir.name) / "phase1.sqlite3")
        with sqlite3.connect(legacy.database_path) as connection:
            connection.executescript("""
                CREATE TABLE institutions (institution_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    vault_owner_id TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE accounts (account_id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
                    owner_type TEXT NOT NULL, institution_id TEXT NOT NULL, account_type TEXT NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE');
                CREATE TABLE ledger_transactions (transaction_id TEXT PRIMARY KEY, institution_id TEXT NOT NULL,
                    transaction_type TEXT NOT NULL, amount INTEGER NOT NULL, source_account_id INTEGER,
                    destination_account_id INTEGER, created_at TEXT NOT NULL, status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE transaction_participants (transaction_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    PRIMARY KEY (transaction_id, owner_id));
                CREATE TABLE institution_initializations (institution_id TEXT PRIMARY KEY,
                    vault_owner_id TEXT NOT NULL, initial_vault_balance INTEGER NOT NULL, initialized_at TEXT NOT NULL);
                INSERT INTO institutions VALUES ('mcorp:guild:1', 'M Corp test', '999', 'now');
                INSERT INTO accounts (owner_id, owner_type, institution_id, account_type, balance, created_at, status)
                    VALUES ('1', 'USER', 'mcorp:guild:1', 'CHECKING', 300, 'now', 'ACTIVE');
                INSERT INTO institution_initializations VALUES ('mcorp:guild:1', '999', 0, 'now');
            """)

        legacy.initialize()

        account = legacy.get_or_create_user_account(INSTITUTION_ID, self.alice.id)
        self.assertEqual(account.balance, 300)
        self.assertTrue(account.is_cash_backed)
        self.assertEqual(legacy.total_cash_backed_balance(INSTITUTION_ID), 300)
        self.assertEqual(legacy.get_treasury_balance(INSTITUTION_ID), 0)

    def test_treasury_persists_after_ledger_reopen(self) -> None:
        self.ledger.record_deposit(INSTITUTION_ID, self.alice.id, 100)
        self.ledger.record_player_to_treasury(INSTITUTION_ID, self.alice.id, 25)

        reopened = LedgerStore(self.ledger.database_path)
        reopened.initialize()

        self.assertEqual(reopened.get_treasury_balance(INSTITUTION_ID), 25)
        self.assertEqual(reopened.total_cash_backed_balance(INSTITUTION_ID), 100)

    async def test_transactions_are_visible_to_the_relevant_user_only(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 300)
        await self.service.bank_transfer(INSTITUTION_ID, self.alice, self.bob, self.vault, 100)

        alice_entries = self.ledger.recent_transactions(INSTITUTION_ID, self.alice.id)
        bob_entries = self.ledger.recent_transactions(INSTITUTION_ID, self.bob.id)

        self.assertEqual([entry.transaction_type for entry in alice_entries], ["BANK_TRANSFER", "CASH_DEPOSIT"])
        self.assertEqual([entry.transaction_type for entry in bob_entries], ["BANK_TRANSFER"])

    async def test_direct_cash_audit_is_excluded_from_banking_history(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 100)
        await self.service.cash_transfer(INSTITUTION_ID, self.alice, self.bob, 20)

        audit_entries = self.ledger.recent_transactions(INSTITUTION_ID, self.alice.id)
        banking_entries = self.ledger.recent_banking_transactions(INSTITUTION_ID, self.alice.id)

        self.assertEqual(audit_entries[0].transaction_type, "PLAYER_CASH_TRANSFER")
        self.assertEqual([entry.transaction_type for entry in banking_entries], ["CASH_DEPOSIT"])

    async def test_cash_transfer_destination_failure_is_compensated(self) -> None:
        self.bank.fail_deposits_for.add(self.bob.id)

        with self.assertRaises(BankingOperationError):
            await self.service.cash_transfer(INSTITUTION_ID, self.alice, self.bob, 50)

        self.assertEqual(self.bank.balances[self.alice.id], 1_000)
        self.assertEqual(self.bank.balances[self.bob.id], 200)

    async def test_ledger_failure_after_cash_deposit_is_compensated(self) -> None:
        failing_ledger = FailingDepositLedger(Path(self.tempdir.name) / "failing.sqlite3")
        failing_ledger.initialize()
        failing_ledger.ensure_institution(INSTITUTION_ID, "M Corp test", self.vault.id)
        failing_ledger.mark_initialized(INSTITUTION_ID, self.vault.id, 0)
        service = BankingService(failing_ledger, self.bank)

        with self.assertRaises(BankingOperationError):
            await service.deposit(INSTITUTION_ID, self.alice, self.vault, 300)

        self.assertEqual(self.bank.balances[self.alice.id], 1_000)
        self.assertEqual(self.bank.balances[self.vault.id], 0)

    async def test_ledger_failure_after_cash_withdrawal_is_compensated(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 300)
        failing_ledger = FailingWithdrawalLedger(Path(self.tempdir.name) / "failing.sqlite3")
        failing_ledger.initialize()
        failing_ledger.ensure_institution(INSTITUTION_ID, "M Corp test", self.vault.id)
        failing_ledger.mark_initialized(INSTITUTION_ID, self.vault.id, 0)
        failing_ledger.record_deposit(INSTITUTION_ID, self.alice.id, 300)
        service = BankingService(failing_ledger, self.bank)

        with self.assertRaises(BankingOperationError):
            await service.withdraw(INSTITUTION_ID, self.alice, self.vault, 100)

        self.assertEqual(self.bank.balances[self.alice.id], 700)
        self.assertEqual(self.bank.balances[self.vault.id], 300)
        self.assertEqual(failing_ledger.get_user_balance(INSTITUTION_ID, self.alice.id), 300)
