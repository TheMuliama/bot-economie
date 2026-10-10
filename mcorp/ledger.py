"""Persistence layer for M Corp's covered banking ledger.

Red Bank remains the source of truth for R-credits.  This module only stores
M Corp liabilities (bank-account balances) and an audit trail of operations.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


DEFAULT_INSTITUTION_ID = "mcorp"
USER_OWNER_TYPE = "USER"
CHECKING_ACCOUNT_TYPE = "CHECKING"
SYSTEM_OWNER_TYPE = "SYSTEM"
TREASURY_ACCOUNT_TYPE = "TREASURY"
TREASURY_OWNER_ID = "M_CORP_TREASURY"
ACTIVE_ACCOUNT_STATUS = "ACTIVE"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Account:
    account_id: int
    owner_id: str
    owner_type: str
    institution_id: str
    account_type: str
    balance: int
    created_at: str
    status: str
    is_cash_backed: bool


@dataclass(frozen=True)
class LedgerTransaction:
    transaction_id: str
    transaction_type: str
    amount: int
    source_account_id: int | None
    destination_account_id: int | None
    created_at: str
    status: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class InstitutionInitialization:
    institution_id: str
    vault_owner_id: str
    initial_vault_balance: int
    initialized_at: str


@dataclass(frozen=True)
class EconomyStats:
    treasury_balance: int
    total_received: int
    total_paid: int
    operation_count: int

    @property
    def net_flow(self) -> int:
        return self.total_received - self.total_paid


class LedgerStore:
    """Small SQLite repository for accounts and immutable transaction records."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS institutions (
                    institution_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    vault_owner_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS accounts (
                    account_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    owner_type TEXT NOT NULL,
                    institution_id TEXT NOT NULL REFERENCES institutions(institution_id),
                    account_type TEXT NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    UNIQUE (institution_id, owner_type, owner_id, account_type)
                );

                CREATE TABLE IF NOT EXISTS ledger_transactions (
                    transaction_id TEXT PRIMARY KEY,
                    institution_id TEXT NOT NULL REFERENCES institutions(institution_id),
                    transaction_type TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK (amount > 0),
                    source_account_id INTEGER REFERENCES accounts(account_id),
                    destination_account_id INTEGER REFERENCES accounts(account_id),
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS transaction_participants (
                    transaction_id TEXT NOT NULL REFERENCES ledger_transactions(transaction_id),
                    owner_id TEXT NOT NULL,
                    PRIMARY KEY (transaction_id, owner_id)
                );

                -- Kept separate from institutions so this is a compatible,
                -- additive migration for Banking MVP V1 SQLite files.
                CREATE TABLE IF NOT EXISTS institution_initializations (
                    institution_id TEXT PRIMARY KEY REFERENCES institutions(institution_id),
                    vault_owner_id TEXT NOT NULL,
                    initial_vault_balance INTEGER NOT NULL CHECK (initial_vault_balance >= 0),
                    initialized_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_accounts_institution
                    ON accounts(institution_id, status);
                CREATE INDEX IF NOT EXISTS idx_transactions_institution_created
                    ON ledger_transactions(institution_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_transaction_participants_owner
                    ON transaction_participants(owner_id, transaction_id);
                """
            )
            self._migrate_cash_backed_accounts(connection)
            # Existing Phase 1 institutions may already be initialized. Give
            # each one its zero-valued covered treasury without changing any
            # historical account or transaction.
            for row in connection.execute(
                "SELECT institution_id FROM institution_initializations"
            ):
                self._get_or_create_treasury_account(connection, str(row["institution_id"]))

    def ensure_institution(
        self, institution_id: str, name: str, vault_owner_id: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO institutions (institution_id, name, vault_owner_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(institution_id) DO UPDATE SET
                    name = excluded.name,
                    vault_owner_id = excluded.vault_owner_id
                """,
                (institution_id, name, str(vault_owner_id), _utc_now()),
            )

    def get_or_create_user_account(self, institution_id: str, user_id: int) -> Account:
        with self._connection() as connection:
            return self._get_or_create_user_account(connection, institution_id, user_id)

    def get_initialization(self, institution_id: str) -> InstitutionInitialization | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM institution_initializations WHERE institution_id = ?",
                (institution_id,),
            ).fetchone()
        if row is None:
            return None
        return InstitutionInitialization(
            institution_id=str(row["institution_id"]),
            vault_owner_id=str(row["vault_owner_id"]),
            initial_vault_balance=int(row["initial_vault_balance"]),
            initialized_at=str(row["initialized_at"]),
        )

    def mark_initialized(
        self, institution_id: str, vault_owner_id: int, initial_vault_balance: int
    ) -> InstitutionInitialization:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO institution_initializations (
                    institution_id, vault_owner_id, initial_vault_balance, initialized_at
                ) VALUES (?, ?, ?, ?)
                """,
                (institution_id, str(vault_owner_id), initial_vault_balance, _utc_now()),
            )
            self._get_or_create_treasury_account(connection, institution_id)
        initialization = self.get_initialization(institution_id)
        assert initialization is not None
        return initialization

    def transaction_count(self, institution_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM ledger_transactions WHERE institution_id = ?",
                (institution_id,),
            ).fetchone()
        return int(row["count"])

    def get_user_balance(self, institution_id: str, user_id: int) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT balance FROM accounts
                WHERE institution_id = ? AND owner_type = ? AND owner_id = ?
                  AND account_type = ? AND status = ?
                """,
                (
                    institution_id,
                    USER_OWNER_TYPE,
                    str(user_id),
                    CHECKING_ACCOUNT_TYPE,
                    ACTIVE_ACCOUNT_STATUS,
                ),
            ).fetchone()
        return 0 if row is None else int(row["balance"])

    def get_or_create_treasury_account(self, institution_id: str) -> Account:
        with self._connection() as connection:
            return self._get_or_create_treasury_account(connection, institution_id)

    def get_treasury_balance(self, institution_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT balance FROM accounts
                WHERE institution_id = ? AND owner_type = ? AND owner_id = ?
                  AND account_type = ? AND status = ?
                """,
                (
                    institution_id,
                    SYSTEM_OWNER_TYPE,
                    TREASURY_OWNER_ID,
                    TREASURY_ACCOUNT_TYPE,
                    ACTIVE_ACCOUNT_STATUS,
                ),
            ).fetchone()
        return 0 if row is None else int(row["balance"])

    def record_deposit(
        self, institution_id: str, user_id: int, amount: int, metadata: dict[str, Any] | None = None
    ) -> int:
        self._require_positive_amount(amount)
        with self._connection() as connection:
            account = self._get_or_create_user_account(connection, institution_id, user_id)
            connection.execute(
                "UPDATE accounts SET balance = balance + ? WHERE account_id = ?",
                (amount, account.account_id),
            )
            self._insert_transaction(
                connection,
                institution_id=institution_id,
                transaction_type="CASH_DEPOSIT",
                amount=amount,
                destination_account_id=account.account_id,
                participants=(user_id,),
                metadata=metadata,
            )
            return account.balance + amount

    def record_withdrawal(
        self, institution_id: str, user_id: int, amount: int, metadata: dict[str, Any] | None = None
    ) -> int:
        self._require_positive_amount(amount)
        with self._connection() as connection:
            account = self._get_or_create_user_account(connection, institution_id, user_id)
            if account.balance < amount:
                raise InsufficientLedgerFunds(account.balance, amount)
            connection.execute(
                "UPDATE accounts SET balance = balance - ? WHERE account_id = ?",
                (amount, account.account_id),
            )
            self._insert_transaction(
                connection,
                institution_id=institution_id,
                transaction_type="CASH_WITHDRAWAL",
                amount=amount,
                source_account_id=account.account_id,
                participants=(user_id,),
                metadata=metadata,
            )
            return account.balance - amount

    def record_transfer(
        self,
        institution_id: str,
        source_user_id: int,
        destination_user_id: int,
        amount: int,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[int, int]:
        self._require_positive_amount(amount)
        if source_user_id == destination_user_id:
            raise SameAccountTransfer()
        with self._connection() as connection:
            source = self._get_or_create_user_account(connection, institution_id, source_user_id)
            if source.balance < amount:
                raise InsufficientLedgerFunds(source.balance, amount)
            destination = self._get_or_create_user_account(
                connection, institution_id, destination_user_id
            )
            connection.execute(
                "UPDATE accounts SET balance = balance - ? WHERE account_id = ?",
                (amount, source.account_id),
            )
            connection.execute(
                "UPDATE accounts SET balance = balance + ? WHERE account_id = ?",
                (amount, destination.account_id),
            )
            self._insert_transaction(
                connection,
                institution_id=institution_id,
                transaction_type="BANK_TRANSFER",
                amount=amount,
                source_account_id=source.account_id,
                destination_account_id=destination.account_id,
                participants=(source_user_id, destination_user_id),
                metadata=metadata,
            )
            return (source.balance - amount, destination.balance + amount)

    def record_player_to_treasury(
        self, institution_id: str, user_id: int, amount: int, metadata: dict[str, Any] | None = None
    ) -> tuple[int, int]:
        """Atomically transfer covered credits from a user to M Corp's treasury."""
        self._require_positive_amount(amount)
        with self._connection() as connection:
            source = self._get_or_create_user_account(connection, institution_id, user_id)
            if source.balance < amount:
                raise InsufficientLedgerFunds(source.balance, amount)
            treasury = self._get_or_create_treasury_account(connection, institution_id)
            self._transfer_between_accounts(
                connection,
                institution_id=institution_id,
                transaction_type="PLAYER_TO_TREASURY",
                amount=amount,
                source=source,
                destination=treasury,
                participants=(user_id, TREASURY_OWNER_ID),
                metadata=metadata,
            )
            return (source.balance - amount, treasury.balance + amount)

    def record_treasury_to_player(
        self, institution_id: str, user_id: int, amount: int, metadata: dict[str, Any] | None = None
    ) -> tuple[int, int]:
        """Atomically transfer covered credits from treasury to a user."""
        self._require_positive_amount(amount)
        with self._connection() as connection:
            treasury = self._get_or_create_treasury_account(connection, institution_id)
            if treasury.balance < amount:
                raise InsufficientLedgerFunds(treasury.balance, amount)
            destination = self._get_or_create_user_account(connection, institution_id, user_id)
            self._transfer_between_accounts(
                connection,
                institution_id=institution_id,
                transaction_type="TREASURY_TO_PLAYER",
                amount=amount,
                source=treasury,
                destination=destination,
                participants=(TREASURY_OWNER_ID, user_id),
                metadata=metadata,
            )
            return (treasury.balance - amount, destination.balance + amount)

    def record_cash_event(
        self,
        institution_id: str,
        transaction_type: str,
        amount: int,
        participants: Iterable[int],
        metadata: dict[str, Any] | None = None,
        status: str = "COMPLETED",
    ) -> None:
        self._require_positive_amount(amount)
        with self._connection() as connection:
            self._insert_transaction(
                connection,
                institution_id=institution_id,
                transaction_type=transaction_type,
                amount=amount,
                participants=participants,
                metadata=metadata,
                status=status,
            )

    def record_recovery_required(
        self,
        institution_id: str,
        transaction_type: str,
        amount: int,
        participants: Iterable[int],
        metadata: dict[str, Any],
    ) -> None:
        self.record_cash_event(
            institution_id,
            transaction_type,
            amount,
            participants,
            metadata,
            status="RECOVERY_REQUIRED",
        )

    def total_balance(self, institution_id: str) -> int:
        """Return all active ledger balances, including future non-covered accounts."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(balance), 0) AS total
                FROM accounts
                WHERE institution_id = ? AND status = ?
                """,
                (institution_id, ACTIVE_ACCOUNT_STATUS),
            ).fetchone()
        return int(row["total"])

    def total_cash_backed_balance(self, institution_id: str) -> int:
        """Return active liabilities which must be covered by the Red vault."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(balance), 0) AS total
                FROM accounts
                WHERE institution_id = ? AND status = ? AND is_cash_backed = 1
                """,
                (institution_id, ACTIVE_ACCOUNT_STATUS),
            ).fetchone()
        return int(row["total"])

    def economy_stats(self, institution_id: str) -> EconomyStats:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN transaction_type = 'PLAYER_TO_TREASURY'
                        AND status = 'COMPLETED' THEN amount ELSE 0 END), 0) AS received,
                    COALESCE(SUM(CASE WHEN transaction_type = 'TREASURY_TO_PLAYER'
                        AND status = 'COMPLETED' THEN amount ELSE 0 END), 0) AS paid,
                    COALESCE(SUM(CASE WHEN transaction_type IN (
                        'PLAYER_TO_TREASURY', 'TREASURY_TO_PLAYER'
                    ) AND status = 'COMPLETED' THEN 1 ELSE 0 END), 0) AS operations
                FROM ledger_transactions
                WHERE institution_id = ?
                """,
                (institution_id,),
            ).fetchone()
        return EconomyStats(
            treasury_balance=self.get_treasury_balance(institution_id),
            total_received=int(row["received"]),
            total_paid=int(row["paid"]),
            operation_count=int(row["operations"]),
        )

    def recent_transactions(
        self, institution_id: str, user_id: int, limit: int = 10
    ) -> list[LedgerTransaction]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT t.*
                FROM ledger_transactions AS t
                JOIN transaction_participants AS p ON p.transaction_id = t.transaction_id
                WHERE t.institution_id = ? AND p.owner_id = ?
                ORDER BY t.created_at DESC
                LIMIT ?
                """,
                (institution_id, str(user_id), limit),
            ).fetchall()
        return [self._transaction_from_row(row) for row in rows]

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _get_or_create_user_account(
        self, connection: sqlite3.Connection, institution_id: str, user_id: int
    ) -> Account:
        row = connection.execute(
            """
            SELECT * FROM accounts
            WHERE institution_id = ? AND owner_type = ? AND owner_id = ?
              AND account_type = ? AND status = ?
            """,
            (
                institution_id,
                USER_OWNER_TYPE,
                str(user_id),
                CHECKING_ACCOUNT_TYPE,
                ACTIVE_ACCOUNT_STATUS,
            ),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO accounts (
                    owner_id, owner_type, institution_id, account_type, balance, created_at, status,
                    is_cash_backed
                ) VALUES (?, ?, ?, ?, 0, ?, ?, 1)
                """,
                (
                    str(user_id),
                    USER_OWNER_TYPE,
                    institution_id,
                    CHECKING_ACCOUNT_TYPE,
                    _utc_now(),
                    ACTIVE_ACCOUNT_STATUS,
                ),
            )
            row = connection.execute("SELECT * FROM accounts WHERE account_id = last_insert_rowid()").fetchone()
        return self._account_from_row(row)

    def _get_or_create_treasury_account(
        self, connection: sqlite3.Connection, institution_id: str
    ) -> Account:
        row = connection.execute(
            """
            SELECT * FROM accounts
            WHERE institution_id = ? AND owner_type = ? AND owner_id = ?
              AND account_type = ? AND status = ?
            """,
            (
                institution_id,
                SYSTEM_OWNER_TYPE,
                TREASURY_OWNER_ID,
                TREASURY_ACCOUNT_TYPE,
                ACTIVE_ACCOUNT_STATUS,
            ),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO accounts (
                    owner_id, owner_type, institution_id, account_type, balance, created_at, status,
                    is_cash_backed
                ) VALUES (?, ?, ?, ?, 0, ?, ?, 1)
                """,
                (
                    TREASURY_OWNER_ID,
                    SYSTEM_OWNER_TYPE,
                    institution_id,
                    TREASURY_ACCOUNT_TYPE,
                    _utc_now(),
                    ACTIVE_ACCOUNT_STATUS,
                ),
            )
            row = connection.execute("SELECT * FROM accounts WHERE account_id = last_insert_rowid()").fetchone()
        return self._account_from_row(row)

    def _migrate_cash_backed_accounts(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
        }
        if "is_cash_backed" not in columns:
            connection.execute(
                "ALTER TABLE accounts ADD COLUMN is_cash_backed INTEGER NOT NULL DEFAULT 1 "
                "CHECK (is_cash_backed IN (0, 1))"
            )

    def _transfer_between_accounts(
        self,
        connection: sqlite3.Connection,
        *,
        institution_id: str,
        transaction_type: str,
        amount: int,
        source: Account,
        destination: Account,
        participants: Iterable[int | str],
        metadata: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            "UPDATE accounts SET balance = balance - ? WHERE account_id = ?",
            (amount, source.account_id),
        )
        connection.execute(
            "UPDATE accounts SET balance = balance + ? WHERE account_id = ?",
            (amount, destination.account_id),
        )
        self._insert_transaction(
            connection,
            institution_id=institution_id,
            transaction_type=transaction_type,
            amount=amount,
            source_account_id=source.account_id,
            destination_account_id=destination.account_id,
            participants=participants,
            metadata=metadata,
        )

    @staticmethod
    def _require_positive_amount(amount: int) -> None:
        if not isinstance(amount, int) or amount <= 0:
            raise InvalidAmount()

    def _insert_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        institution_id: str,
        transaction_type: str,
        amount: int,
        participants: Iterable[int | str],
        source_account_id: int | None = None,
        destination_account_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "COMPLETED",
    ) -> None:
        transaction_id = str(uuid.uuid4())
        connection.execute(
            """
            INSERT INTO ledger_transactions (
                transaction_id, institution_id, transaction_type, amount,
                source_account_id, destination_account_id, created_at, status, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transaction_id,
                institution_id,
                transaction_type,
                amount,
                source_account_id,
                destination_account_id,
                _utc_now(),
                status,
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        connection.executemany(
            "INSERT INTO transaction_participants (transaction_id, owner_id) VALUES (?, ?)",
            [(transaction_id, str(owner_id)) for owner_id in set(participants)],
        )

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> Account:
        return Account(
            account_id=int(row["account_id"]),
            owner_id=str(row["owner_id"]),
            owner_type=str(row["owner_type"]),
            institution_id=str(row["institution_id"]),
            account_type=str(row["account_type"]),
            balance=int(row["balance"]),
            created_at=str(row["created_at"]),
            status=str(row["status"]),
            is_cash_backed=bool(row["is_cash_backed"]),
        )

    @staticmethod
    def _transaction_from_row(row: sqlite3.Row) -> LedgerTransaction:
        return LedgerTransaction(
            transaction_id=str(row["transaction_id"]),
            transaction_type=str(row["transaction_type"]),
            amount=int(row["amount"]),
            source_account_id=row["source_account_id"],
            destination_account_id=row["destination_account_id"],
            created_at=str(row["created_at"]),
            status=str(row["status"]),
            metadata=json.loads(str(row["metadata_json"])),
        )


class LedgerError(Exception):
    """Base class for expected ledger-domain errors."""


class InvalidAmount(LedgerError):
    pass


class InsufficientLedgerFunds(LedgerError):
    def __init__(self, balance: int, requested: int) -> None:
        self.balance = balance
        self.requested = requested
        super().__init__(f"Insufficient ledger funds: {requested} requested, {balance} available")


class SameAccountTransfer(LedgerError):
    pass
