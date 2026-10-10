"""Integrity tests for the M Corp banking application service."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mcorp.ledger import InsufficientLedgerFunds, InvalidAmount, LedgerStore, SameAccountTransfer
from mcorp.service import BankingOperationError, BankingService, InvariantViolation, InsufficientCash


INSTITUTION_ID = "mcorp:guild:1"


class Member:
    def __init__(self, member_id: int) -> None:
        self.id = member_id


class FakeBank:
    def __init__(self, balances: dict[int, int]) -> None:
        self.balances = balances.copy()
        self.fail_deposits_for: set[int] = set()

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

    async def test_operations_refuse_a_preexisting_vault_anomaly(self) -> None:
        self.bank.balances[self.vault.id] = 10

        with self.assertRaises(InvariantViolation):
            await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 100)

        self.assertEqual(self.bank.balances[self.alice.id], 1_000)
        self.assertEqual(self.ledger.total_balance(INSTITUTION_ID), 0)

    async def test_transactions_are_visible_to_the_relevant_user_only(self) -> None:
        await self.service.deposit(INSTITUTION_ID, self.alice, self.vault, 300)
        await self.service.bank_transfer(INSTITUTION_ID, self.alice, self.bob, self.vault, 100)

        alice_entries = self.ledger.recent_transactions(INSTITUTION_ID, self.alice.id)
        bob_entries = self.ledger.recent_transactions(INSTITUTION_ID, self.bob.id)

        self.assertEqual([entry.transaction_type for entry in alice_entries], ["BANK_TRANSFER", "CASH_DEPOSIT"])
        self.assertEqual([entry.transaction_type for entry in bob_entries], ["BANK_TRANSFER"])

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
        failing_ledger.record_deposit(INSTITUTION_ID, self.alice.id, 300)
        service = BankingService(failing_ledger, self.bank)

        with self.assertRaises(BankingOperationError):
            await service.withdraw(INSTITUTION_ID, self.alice, self.vault, 100)

        self.assertEqual(self.bank.balances[self.alice.id], 700)
        self.assertEqual(self.bank.balances[self.vault.id], 300)
        self.assertEqual(failing_ledger.get_user_balance(INSTITUTION_ID, self.alice.id), 300)
