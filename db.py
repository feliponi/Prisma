"""SQLite persistence layer for the Personal Finance MVP.

Owns the `accounts`, `categories`, and `transactions` tables (see schema.sql).
Idempotent import is enforced here via `INSERT OR IGNORE` keyed on
`transaction_hash`. No AI logic and no Streamlit imports belong in this module.
"""

from __future__ import annotations

import json
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

DEFAULT_SCHEMA_PATH = Path("schema.sql")


def get_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enabled, creating the file if absent.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        An open `sqlite3.Connection` with `PRAGMA foreign_keys = ON`.

    Raises:
        sqlite3.Error: if the database file cannot be opened.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection, schema_path: Path = DEFAULT_SCHEMA_PATH) -> None:
    """Create the `accounts`, `categories`, and `transactions` tables if absent.

    Args:
        conn: An open SQLite connection.
        schema_path: Path to the schema.sql DDL file to execute.

    Raises:
        sqlite3.Error: if the DDL script fails to execute.
    """
    ddl = schema_path.read_text(encoding="utf-8")
    conn.executescript(ddl)
    conn.commit()


def seed_categories(conn: sqlite3.Connection, categories_path: Path = DEFAULT_CATEGORIES_PATH) -> None:
    """Populate the `categories` table from categories.json (llm_categories + system_categories).

    Idempotent: existing rows are left untouched (`INSERT OR IGNORE`).

    Args:
        conn: An open SQLite connection with the schema already initialized.
        categories_path: Path to the categories.json taxonomy file.

    Raises:
        FileNotFoundError: if categories_path does not exist.
        json.JSONDecodeError: if categories.json is malformed.
    """
    taxonomy = json.loads(categories_path.read_text(encoding="utf-8"))
    names = list(taxonomy["llm_categories"]) + list(taxonomy["system_categories"])
    conn.executemany(
        "INSERT OR IGNORE INTO categories (name) VALUES (?)",
        [(name,) for name in names],
    )
    conn.commit()
    logger.info("Seeded %d categories", len(names))


def upsert_account(conn: sqlite3.Connection, profile: AccountProfile) -> None:
    """Insert or update the `accounts` row for this profile's account_id.

    Args:
        conn: An open SQLite connection.
        profile: The account profile whose account_id/account_type/bank_name
            should be reflected in the `accounts` table.

    Raises:
        sqlite3.Error: on constraint violation or connection failure.
    """
    conn.execute(
        """
        INSERT INTO accounts (account_id, account_type, bank_name)
        VALUES (?, ?, ?)
        ON CONFLICT (account_id) DO UPDATE SET
            account_type = excluded.account_type,
            bank_name = excluded.bank_name
        """,
        (profile.account_id, profile.account_type.value, profile.bank_name),
    )
    conn.commit()


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
    """
    if df.empty:
        return 0

    rows = []
    for _, row in df.iterrows():
        date_value = row["date"]
        date_iso = date_value.strftime("%Y-%m-%d") if hasattr(date_value, "strftime") else str(date_value)
        rows.append(
            (
                row["transaction_hash"],
                row["account_id"],
                row["account_type"],
                date_iso,
                float(row["amount"]),
                row["currency"],
                row["description"],
                row["category"],
                int(bool(row["is_internal_transfer"])),
            )
        )

    cursor = conn.executemany(
        """
        INSERT OR IGNORE INTO transactions (
            transaction_hash, account_id, account_type, date, amount,
            currency, description, category, is_internal_transfer
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    inserted = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0
    logger.info("Inserted %d new transactions (of %d rows submitted)", inserted, len(rows))
    return inserted


def fetch_accounts(conn: sqlite3.Connection) -> list[AccountSummary]:
    """List all known accounts from the `accounts` table.

    Args:
        conn: An open SQLite connection.

    Returns:
        All accounts, ordered by account_id.
    """
    cursor = conn.execute(
        "SELECT account_id, account_type, bank_name FROM accounts ORDER BY account_id"
    )
    return [
        AccountSummary(account_id=row["account_id"], account_type=row["account_type"], bank_name=row["bank_name"])
        for row in cursor.fetchall()
    ]


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
    """
    query = "SELECT * FROM transactions WHERE 1=1"
    params: list = []
    if account_id is not None:
        query += " AND account_id = ?"
        params.append(account_id)
    if currency is not None:
        query += " AND currency = ?"
        params.append(currency)
    if exclude_internal_transfers:
        query += " AND is_internal_transfer = 0"
    query += " ORDER BY date"

    df = pd.read_sql_query(query, conn, params=params, parse_dates=["date"])
    if "is_internal_transfer" in df.columns:
        df["is_internal_transfer"] = df["is_internal_transfer"].astype(bool)
    return df


def update_categories(conn: sqlite3.Connection, category_by_hash: dict[str, str]) -> int:
    """Persist LLM-resolved (or manually edited) categories back to the `transactions` table.

    Args:
        conn: An open SQLite connection.
        category_by_hash: Mapping of transaction_hash -> new category name.

    Returns:
        The number of rows updated.
    """
    if not category_by_hash:
        return 0
    cursor = conn.executemany(
        "UPDATE transactions SET category = ? WHERE transaction_hash = ?",
        [(category, tx_hash) for tx_hash, category in category_by_hash.items()],
    )
    conn.commit()
    return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0


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
    """
    query = """
        SELECT account_id, currency, category, amount
        FROM transactions
        WHERE is_internal_transfer = 0 AND amount < 0
    """
    params: list = []
    if account_id is not None:
        query += " AND account_id = ?"
        params.append(account_id)

    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return []

    df["spend"] = df["amount"].abs()
    grouped = df.groupby(["account_id", "currency", "category"])["spend"].sum()

    aggregates: dict[tuple[str, str], dict[str, float]] = {}
    for (acc_id, currency, category), total in grouped.items():
        key = (acc_id, currency)
        aggregates.setdefault(key, {})[category] = float(total)

    return [
        SpendingAggregate(account_id=acc_id, currency=currency, category_totals=totals)
        for (acc_id, currency), totals in sorted(aggregates.items())
    ]
