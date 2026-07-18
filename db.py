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


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Return True if `column` exists on `table` (via PRAGMA table_info)."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in cursor.fetchall())


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring an existing (possibly populated) database up to the current schema.

    Idempotent and non-destructive — safe to run on every startup and on a
    database created before these columns/tables existed. Never drops or
    rewrites user data.

    Migrations:
        - transactions.category_source: added with DEFAULT 'llm' if missing;
          existing internal-transfer rows are back-filled to 'rule'.
        - budgets table: created if missing (see schema.sql).

    Args:
        conn: An open SQLite connection with the base schema initialized.

    Raises:
        sqlite3.Error: if a migration statement fails.
    """
    if not _column_exists(conn, "transactions", "category_source"):
        logger.info("Migration: adding transactions.category_source")
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN category_source TEXT NOT NULL "
            "DEFAULT 'llm' CHECK (category_source IN ('llm', 'manual', 'rule'))"
        )
        # Back-fill: rows already flagged as internal transfers are rule-sourced.
        conn.execute(
            "UPDATE transactions SET category_source = 'rule' WHERE is_internal_transfer = 1"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS budgets (
            category        TEXT NOT NULL REFERENCES categories (name),
            currency        TEXT NOT NULL CHECK (currency IN ('BRL', 'EUR')),
            planned_amount  REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (category, currency)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions (category)")
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
        is_internal = int(bool(row["is_internal_transfer"]))
        # Forced categories (internal transfers) are rule-sourced; everything
        # else starts as 'llm' (pending / auto categorization).
        category_source = "rule" if is_internal else "llm"
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
                is_internal,
                category_source,
            )
        )

    cursor = conn.executemany(
        """
        INSERT OR IGNORE INTO transactions (
            transaction_hash, account_id, account_type, date, amount,
            currency, description, category, is_internal_transfer, category_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


# ---------------------------------------------------------------------------
# Navigation / dashboard support
# ---------------------------------------------------------------------------
def count_accounts(conn: sqlite3.Connection) -> int:
    """Return the number of rows in the `accounts` table."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"])


def count_transactions(conn: sqlite3.Connection) -> int:
    """Return the number of rows in the `transactions` table."""
    return int(conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"])


def account_stats(conn: sqlite3.Connection) -> list[dict]:
    """Per-account summary: type, bank, transaction count and date range.

    Returns:
        One dict per account with keys account_id, account_type, bank_name,
        tx_count, min_date, max_date (dates are ISO strings or None).
    """
    cursor = conn.execute(
        """
        SELECT a.account_id, a.account_type, a.bank_name,
               COUNT(t.transaction_hash) AS tx_count,
               MIN(t.date) AS min_date, MAX(t.date) AS max_date
        FROM accounts a
        LEFT JOIN transactions t ON t.account_id = a.account_id
        GROUP BY a.account_id, a.account_type, a.bank_name
        ORDER BY a.account_id
        """
    )
    return [dict(row) for row in cursor.fetchall()]


def account_date_range(conn: sqlite3.Connection, account_id: str) -> tuple[str, str] | None:
    """Return (min_date, max_date) ISO strings for an account, or None if empty."""
    row = conn.execute(
        "SELECT MIN(date) AS lo, MAX(date) AS hi FROM transactions WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None or row["lo"] is None:
        return None
    return row["lo"], row["hi"]


def list_currencies(conn: sqlite3.Connection) -> list[str]:
    """List distinct currencies present among non-internal-transfer transactions."""
    cursor = conn.execute(
        "SELECT DISTINCT currency FROM transactions WHERE is_internal_transfer = 0 ORDER BY currency"
    )
    return [row["currency"] for row in cursor.fetchall()]


def existing_hashes(
    conn: sqlite3.Connection, account_id: str, start_iso: str, end_iso: str
) -> set[str]:
    """Return the set of transaction_hashes already stored for an account in a date range.

    Used by the incremental-import pre-import summary to compute the
    new-vs-duplicate split BEFORE inserting anything. Does not modify data.
    """
    cursor = conn.execute(
        "SELECT transaction_hash FROM transactions "
        "WHERE account_id = ? AND date BETWEEN ? AND ?",
        (account_id, start_iso, end_iso),
    )
    return {row["transaction_hash"] for row in cursor.fetchall()}


def fetch_transactions_filtered(
    conn: sqlite3.Connection,
    currency: str,
    start_iso: str | None = None,
    end_iso: str | None = None,
    account_ids: tuple[str, ...] | None = None,
    categories: tuple[str, ...] | None = None,
    exclude_internal_transfers: bool = True,
) -> pd.DataFrame:
    """Query transactions for the dashboard, pushing all filters into SQL.

    Currency is a SELECTOR, not a mixing filter: exactly one currency is
    queried at a time (BRL and EUR are never summed or converted).

    Args:
        conn: An open SQLite connection.
        currency: The single ISO 4217 currency to render.
        start_iso, end_iso: Inclusive ISO date bounds (None = unbounded).
        account_ids: Restrict to these accounts (None/empty = all).
        categories: Restrict to these categories (None/empty = all).
        exclude_internal_transfers: Always True for spend metrics.

    Returns:
        A DataFrame (parsed `date`) filtered on indexed columns.
    """
    query = "SELECT * FROM transactions WHERE currency = ?"
    params: list = [currency]
    if exclude_internal_transfers:
        query += " AND is_internal_transfer = 0"
    if start_iso is not None:
        query += " AND date >= ?"
        params.append(start_iso)
    if end_iso is not None:
        query += " AND date <= ?"
        params.append(end_iso)
    if account_ids:
        query += f" AND account_id IN ({','.join('?' * len(account_ids))})"
        params.extend(account_ids)
    if categories:
        query += f" AND category IN ({','.join('?' * len(categories))})"
        params.extend(categories)
    query += " ORDER BY date"

    df = pd.read_sql_query(query, conn, params=params, parse_dates=["date"])
    if "is_internal_transfer" in df.columns:
        df["is_internal_transfer"] = df["is_internal_transfer"].astype(bool)
    return df


def list_all_accounts(conn: sqlite3.Connection) -> list[str]:
    """Return all account_ids (ordered), regardless of transaction presence."""
    cursor = conn.execute("SELECT account_id FROM accounts ORDER BY account_id")
    return [row["account_id"] for row in cursor.fetchall()]


def list_all_categories(conn: sqlite3.Connection) -> list[str]:
    """Return all category names (ordered)."""
    cursor = conn.execute("SELECT name FROM categories ORDER BY name")
    return [row["name"] for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Category editing (manual edits are durable)
# ---------------------------------------------------------------------------
def apply_llm_categories(conn: sqlite3.Connection, category_by_hash: dict[str, str]) -> int:
    """Write LLM-resolved categories, but NEVER overwrite manual edits.

    Rows whose `category_source = 'manual'` are left untouched; the rest are
    updated and marked `category_source = 'llm'`.

    Returns:
        The number of rows updated.
    """
    if not category_by_hash:
        return 0
    cursor = conn.executemany(
        "UPDATE transactions SET category = ?, category_source = 'llm' "
        "WHERE transaction_hash = ? AND category_source != 'manual'",
        [(category, tx_hash) for tx_hash, category in category_by_hash.items()],
    )
    conn.commit()
    return cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else 0


def set_manual_category(conn: sqlite3.Connection, transaction_hash: str, category: str) -> None:
    """Persist a user's manual category edit (durable; survives later LLM runs)."""
    conn.execute(
        "UPDATE transactions SET category = ?, category_source = 'manual' WHERE transaction_hash = ?",
        (category, transaction_hash),
    )
    conn.commit()


def fetch_categorizable(
    conn: sqlite3.Connection,
    account_id: str | None = None,
    only_hashes: tuple[str, ...] | None = None,
) -> pd.DataFrame:
    """Fetch rows eligible for LLM categorization.

    Eligible = not an internal transfer, not manually edited, and still
    'Uncategorized'. Optionally scoped to one account and/or a specific set of
    hashes (used to categorize ONLY newly-inserted rows).

    Returns:
        A DataFrame with columns [transaction_hash, description].
    """
    query = (
        "SELECT transaction_hash, description FROM transactions "
        "WHERE is_internal_transfer = 0 AND category_source != 'manual' "
        "AND category = 'Uncategorized'"
    )
    params: list = []
    if account_id is not None:
        query += " AND account_id = ?"
        params.append(account_id)
    if only_hashes:
        query += f" AND transaction_hash IN ({','.join('?' * len(only_hashes))})"
        params.extend(only_hashes)
    return pd.read_sql_query(query, conn, params=params)


# ---------------------------------------------------------------------------
# Account deletion (cascade transactions)
# ---------------------------------------------------------------------------
def delete_account(conn: sqlite3.Connection, account_id: str) -> int:
    """Delete an account AND all of its transactions.

    Returns:
        The number of transactions removed.

    Raises:
        sqlite3.Error: on connection failure.
    """
    tx_removed = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE account_id = ?", (account_id,)
        ).fetchone()["n"]
    )
    conn.execute("DELETE FROM transactions WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
    conn.commit()
    logger.info("Deleted account '%s' and %d transactions", account_id, tx_removed)
    return tx_removed


# ---------------------------------------------------------------------------
# Categories & budgets management
# ---------------------------------------------------------------------------
def add_category(conn: sqlite3.Connection, name: str) -> None:
    """Add a category name (idempotent via INSERT OR IGNORE)."""
    conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    conn.commit()


def get_budgets(conn: sqlite3.Connection, currency: str) -> dict[str, float]:
    """Return {category: planned_amount} budgets for one currency."""
    cursor = conn.execute(
        "SELECT category, planned_amount FROM budgets WHERE currency = ?", (currency,)
    )
    return {row["category"]: float(row["planned_amount"]) for row in cursor.fetchall()}


def set_budget(conn: sqlite3.Connection, category: str, currency: str, planned_amount: float) -> None:
    """Upsert a per-(category, currency) planned budget amount."""
    conn.execute(
        """
        INSERT INTO budgets (category, currency, planned_amount) VALUES (?, ?, ?)
        ON CONFLICT (category, currency) DO UPDATE SET planned_amount = excluded.planned_amount
        """,
        (category, currency, float(planned_amount)),
    )
    conn.commit()


def delete_budget(conn: sqlite3.Connection, category: str, currency: str) -> None:
    """Remove a budget line for a (category, currency) pair."""
    conn.execute("DELETE FROM budgets WHERE category = ? AND currency = ?", (category, currency))
    conn.commit()
