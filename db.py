"""SQLite persistence layer for the Personal Finance MVP.

Contracts only (Phase 1). Every function below is fully type-hinted and
documented but raises NotImplementedError; bodies land in Phase 2.

Owns the `accounts`, `categories`, and `transactions` tables (see schema.sql).
Idempotent import is enforced here via `INSERT OR IGNORE` keyed on
`transaction_hash`. No AI logic and no Streamlit imports belong in this module.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pandas as pd

from models import (
    DEFAULT_CATEGORIES_PATH,
    DEFAULT_DB_PATH,
    AccountProfile,
    AccountSummary,
    SpendingAggregate,
)

logger = logging.getLogger(__name__)


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled, creating the file if absent.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        An open `sqlite3.Connection` with `PRAGMA foreign_keys = ON`.

    Raises:
        sqlite3.Error: if the database file cannot be opened.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def init_schema(conn: sqlite3.Connection, schema_path: Path = Path("schema.sql")) -> None:
    """Create the `accounts`, `categories`, and `transactions` tables if absent.

    Args:
        conn: An open SQLite connection.
        schema_path: Path to the schema.sql DDL file to execute.

    Raises:
        sqlite3.Error: if the DDL script fails to execute.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def seed_categories(conn: sqlite3.Connection, categories_path: Path = DEFAULT_CATEGORIES_PATH) -> None:
    """Populate the `categories` table from categories.json (llm_categories + system_categories).

    Idempotent: existing rows are left untouched (`INSERT OR IGNORE`).

    Args:
        conn: An open SQLite connection with the schema already initialized.
        categories_path: Path to the categories.json taxonomy file.

    Raises:
        FileNotFoundError: if categories_path does not exist.
        json.JSONDecodeError: if categories.json is malformed.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def upsert_account(conn: sqlite3.Connection, profile: AccountProfile) -> None:
    """Insert or update the `accounts` row for this profile's account_id.

    Args:
        conn: An open SQLite connection.
        profile: The account profile whose account_id/account_type/bank_name
            should be reflected in the `accounts` table.

    Raises:
        sqlite3.Error: on constraint violation or connection failure.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def insert_transactions(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Idempotently insert canonical transaction rows into the `transactions` table.

    Uses `INSERT OR IGNORE` keyed on `transaction_hash`, so re-importing an
    already-seen CSV (or an overlapping date range from a fresh export)
    inserts zero duplicate rows.

    Args:
        conn: An open SQLite connection with the schema initialized and the
            owning account already upserted.
        df: DataFrame matching `models.TransactionRecord` field-for-field,
            as produced by `csv_mapper.process_csv`.

    Returns:
        The number of NEW rows actually inserted (excludes ignored duplicates).

    Raises:
        sqlite3.Error: on constraint violation (e.g. unknown category or
            unregistered account_id) or connection failure.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def fetch_accounts(conn: sqlite3.Connection) -> list[AccountSummary]:
    """List all known accounts from the `accounts` table.

    Args:
        conn: An open SQLite connection.

    Returns:
        All accounts, ordered by account_id.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def fetch_transactions(
    conn: sqlite3.Connection,
    account_id: str | None = None,
    currency: str | None = None,
    exclude_internal_transfers: bool = True,
) -> pd.DataFrame:
    """Query transactions, optionally scoped to one account and/or currency.

    Args:
        conn: An open SQLite connection.
        account_id: If set, restrict to this single account. If None, return
            transactions across all accounts.
        currency: If set, restrict to this single ISO 4217 currency code.
            If None, all currencies are included (never summed together by
            the caller — see Non-Goals: no FX conversion).
        exclude_internal_transfers: If True (default), rows with
            `is_internal_transfer = 1` are omitted, matching the spend-insight
            contract in ai_services.generate_financial_insights.

    Returns:
        A DataFrame matching `models.TransactionRecord` field-for-field.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def compute_spending_aggregates(
    conn: sqlite3.Connection, account_id: str | None = None
) -> list[SpendingAggregate]:
    """Compute per-account, per-currency category spending totals.

    Internal transfers are always excluded. Spend magnitudes are reported as
    positive floats (i.e. `abs(sum(amount))` for amount < 0 rows per category).

    Args:
        conn: An open SQLite connection.
        account_id: If set, restrict aggregation to this single account.
            If None, compute one SpendingAggregate per (account, currency)
            pair present in the data.

    Returns:
        A list of SpendingAggregate, one per distinct (account_id, currency)
        pair covered by the query.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError
