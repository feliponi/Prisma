"""SQLite persistence layer for the Personal Finance MVP.

Owns the `accounts`, `categories`, and `transactions` tables (see schema.sql).
Idempotent import is enforced here via `INSERT OR IGNORE` keyed on
`transaction_hash`. No AI logic and no Streamlit imports belong in this module.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
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

# Single source of truth for the effective (display) description. The ORIGINAL
# `description` is immutable (it feeds transaction_hash); this expression is the
# ONLY place COALESCE logic lives — reuse it everywhere the display label is read.
# NULLIF treats an empty-string override as "no override".
EFFECTIVE_DESCRIPTION_SQL = "COALESCE(NULLIF(transactions.description_override, ''), transactions.description)"


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

    # --- Asset valuation ledger (non-liquid assets, e.g. ETFs) --------------
    # Kept STRICTLY separate from the transactional cash flow. Each asset owns
    # its currency; BRL and EUR are never mixed. The composite PK on the history
    # table makes valuation snapshots idempotent (one per asset per day).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assets (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL UNIQUE,
            currency TEXT NOT NULL CHECK (currency IN ('BRL', 'EUR'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS asset_valuation_history (
            asset_id INTEGER NOT NULL REFERENCES assets (id),
            date     TEXT NOT NULL,
            balance  REAL NOT NULL,
            PRIMARY KEY (asset_id, date)
        )
        """
    )

    # --- Opening balance per account ---------------------------------------
    if not _column_exists(conn, "accounts", "opening_balance"):
        logger.info("Migration: adding accounts.opening_balance")
        conn.execute("ALTER TABLE accounts ADD COLUMN opening_balance REAL NOT NULL DEFAULT 0.0")
    if not _column_exists(conn, "accounts", "opening_balance_date"):
        logger.info("Migration: adding accounts.opening_balance_date")
        conn.execute("ALTER TABLE accounts ADD COLUMN opening_balance_date TEXT")
    if not _column_exists(conn, "accounts", "currency"):
        logger.info("Migration: adding accounts.currency")
        conn.execute("ALTER TABLE accounts ADD COLUMN currency TEXT")

    # --- Editable description override + notes + pre-tracking flag ----------
    if not _column_exists(conn, "transactions", "is_before_tracking"):
        logger.info("Migration: adding transactions.is_before_tracking")
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN is_before_tracking INTEGER NOT NULL DEFAULT 0 "
            "CHECK (is_before_tracking IN (0, 1))"
        )
    if not _column_exists(conn, "transactions", "description_override"):
        logger.info("Migration: adding transactions.description_override")
        conn.execute("ALTER TABLE transactions ADD COLUMN description_override TEXT")
    if not _column_exists(conn, "transactions", "notes"):
        logger.info("Migration: adding transactions.notes")
        conn.execute("ALTER TABLE transactions ADD COLUMN notes TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_transactions_before ON transactions (is_before_tracking)")
    conn.commit()

    # --- Back-fill opening_balance_date / currency for existing accounts ----
    # Default opening date = the day BEFORE each account's earliest transaction
    # (so no existing row is retroactively excluded), opening_balance stays 0,
    # currency = the account's most common transaction currency.
    for row in conn.execute("SELECT account_id, opening_balance_date, currency FROM accounts").fetchall():
        account_id = row["account_id"]
        if row["opening_balance_date"] is None:
            earliest = conn.execute(
                "SELECT MIN(date) AS d FROM transactions WHERE account_id = ?", (account_id,)
            ).fetchone()["d"]
            if earliest is not None:
                opening_date = (
                    datetime.strptime(earliest, "%Y-%m-%d").date() - timedelta(days=1)
                ).isoformat()
                conn.execute(
                    "UPDATE accounts SET opening_balance_date = ? WHERE account_id = ?",
                    (opening_date, account_id),
                )
        if row["currency"] is None:
            common = conn.execute(
                "SELECT currency FROM transactions WHERE account_id = ? "
                "GROUP BY currency ORDER BY COUNT(*) DESC LIMIT 1", (account_id,)
            ).fetchone()
            if common is not None:
                conn.execute(
                    "UPDATE accounts SET currency = ? WHERE account_id = ?",
                    (common["currency"], account_id),
                )
    conn.commit()
    for account_id in [r["account_id"] for r in conn.execute("SELECT account_id FROM accounts").fetchall()]:
        recompute_tracking_flags(conn, account_id)


def recompute_tracking_flags(conn: sqlite3.Connection, account_id: str) -> int:
    """Recompute `is_before_tracking` for every row of one account.

    A row is 'before tracking' when its date is on or before the account's
    `opening_balance_date` (already baked into the opening balance). When the
    account has no opening date, nothing is before tracking. Must be called
    whenever the opening date changes or new rows are imported.

    Returns:
        The number of rows updated (flag flipped).
    """
    row = conn.execute(
        "SELECT opening_balance_date FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    opening_date = row["opening_balance_date"] if row is not None else None
    if opening_date is None:
        cursor = conn.execute(
            "UPDATE transactions SET is_before_tracking = 0 "
            "WHERE account_id = ? AND is_before_tracking != 0",
            (account_id,),
        )
    else:
        cursor = conn.execute(
            "UPDATE transactions SET is_before_tracking = "
            "CASE WHEN date <= ? THEN 1 ELSE 0 END WHERE account_id = ?",
            (opening_date, account_id),
        )
    conn.commit()
    return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


def get_account(conn: sqlite3.Connection, account_id: str) -> dict | None:
    """Return the full accounts row (incl. opening balance/date/currency) as a dict."""
    row = conn.execute("SELECT * FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
    return dict(row) if row is not None else None


def set_opening_balance(
    conn: sqlite3.Connection,
    account_id: str,
    opening_balance: float,
    opening_balance_date: str | None,
    currency: str | None = None,
) -> None:
    """Set an account's opening balance / tracking date (and optionally currency).

    `opening_balance` is the balance as of the END of `opening_balance_date`.
    Recomputes `is_before_tracking` for all of the account's rows, because the
    date change shifts which rows are already baked into the opening balance.

    Raises:
        sqlite3.Error: on connection failure.
    """
    try:
        if currency is not None:
            conn.execute(
                "UPDATE accounts SET opening_balance = ?, opening_balance_date = ?, currency = ? "
                "WHERE account_id = ?",
                (float(opening_balance), opening_balance_date, currency, account_id),
            )
        else:
            conn.execute(
                "UPDATE accounts SET opening_balance = ?, opening_balance_date = ? WHERE account_id = ?",
                (float(opening_balance), opening_balance_date, account_id),
            )
        conn.commit()
        recompute_tracking_flags(conn, account_id)
        logger.info(
            "Set opening balance for '%s': %.2f as of %s", account_id, opening_balance, opening_balance_date
        )
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to set opening balance for '%s'", account_id)
        raise


def running_balance(conn: sqlite3.Connection, account_id: str) -> float | None:
    """Current running balance = opening_balance + SUM(amount) for post-tracking rows.

    Internal transfers are INCLUDED (they move real money within the account);
    pre-tracking rows are excluded (already baked into opening_balance). For a
    credit-card account this represents the outstanding amount owed, not cash.

    Returns:
        The running balance, or None if the account does not exist.
    """
    account = get_account(conn, account_id)
    if account is None:
        return None
    total = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        "WHERE account_id = ? AND is_before_tracking = 0",
        (account_id,),
    ).fetchone()["s"]
    return float(account["opening_balance"]) + float(total)


def fetch_account_ledger(
    conn: sqlite3.Connection,
    account_ids: tuple[str, ...],
    currency: str,
    include_before: bool = False,
) -> pd.DataFrame:
    """Fetch rows (internal transfers INCLUDED) for the running-balance series.

    Args:
        conn: An open SQLite connection.
        account_ids: Accounts to include (must all be the given currency).
        currency: The single currency (BRL/EUR never mixed).
        include_before: If False (default), pre-tracking rows are excluded
            (already baked into opening_balance). If True, all rows are returned
            (the caller decomposes the baseline to avoid double-counting).

    Returns:
        DataFrame [date, amount] ordered by date.
    """
    if not account_ids:
        return pd.DataFrame(columns=["date", "amount"])
    placeholders = ",".join("?" * len(account_ids))
    query = (
        f"SELECT date, amount FROM transactions "
        f"WHERE currency = ? AND account_id IN ({placeholders})"
    )
    if not include_before:
        query += " AND is_before_tracking = 0"
    query += " ORDER BY date"
    return pd.read_sql_query(query, conn, params=[currency, *account_ids], parse_dates=["date"])


def opening_balance_sum(conn: sqlite3.Connection, account_ids: tuple[str, ...], currency: str) -> float:
    """Sum of opening balances for the given same-currency accounts."""
    if not account_ids:
        return 0.0
    placeholders = ",".join("?" * len(account_ids))
    row = conn.execute(
        f"SELECT COALESCE(SUM(opening_balance), 0) AS s FROM accounts "
        f"WHERE currency = ? AND account_id IN ({placeholders})",
        [currency, *account_ids],
    ).fetchone()
    return float(row["s"])


def balance_as_of(
    conn: sqlite3.Connection, account_ids: tuple[str, ...], currency: str, end_iso: str
) -> float:
    """Running balance for same-currency accounts as of `end_iso`.

    = opening_balance_sum + SUM(amount) over non-pre-tracking rows (internal
    transfers INCLUDED) with date <= end_iso. Category-agnostic and never mixes
    currencies. Invariant to the 'include before' toggle (pre-tracking amounts
    are already inside the opening balance). Mirrors `running_balance` but
    bounded to a date and aggregated over several accounts, so it stays
    consistent with the per-account "Saldo por conta" totals.

    Args:
        conn: An open SQLite connection.
        account_ids: Same-currency accounts to include (empty -> 0.0).
        currency: The single ISO 4217 currency.
        end_iso: Inclusive ISO date upper bound.

    Returns:
        The running balance as of `end_iso`.
    """
    if not account_ids:
        return 0.0
    placeholders = ",".join("?" * len(account_ids))
    row = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        f"WHERE currency = ? AND is_before_tracking = 0 AND date <= ? "
        f"AND account_id IN ({placeholders})",
        [currency, end_iso, *account_ids],
    ).fetchone()
    return opening_balance_sum(conn, account_ids, currency) + float(row["s"])


def pre_tracking_amount_sum(conn: sqlite3.Connection, account_ids: tuple[str, ...], currency: str) -> float:
    """Sum of amounts of pre-tracking rows for the given same-currency accounts.

    Used to DECOMPOSE the opening balance when the 'include before' toggle is on,
    so the balance line extends back through the pre-tracking period without
    double-counting (those amounts are already inside `opening_balance`).
    """
    if not account_ids:
        return 0.0
    placeholders = ",".join("?" * len(account_ids))
    row = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) AS s FROM transactions "
        f"WHERE currency = ? AND is_before_tracking = 1 AND account_id IN ({placeholders})",
        [currency, *account_ids],
    ).fetchone()
    return float(row["s"])


def set_description_override(conn: sqlite3.Connection, transaction_hash: str, override: str | None) -> None:
    """Set (or clear, when override is None/empty) a row's editable display label.

    NEVER touches `description` or `transaction_hash` — those are immutable. An
    empty/blank override is stored as NULL ("restaurar original").

    Raises:
        sqlite3.Error: on connection failure.
    """
    value = override.strip() if isinstance(override, str) and override.strip() else None
    try:
        conn.execute(
            "UPDATE transactions SET description_override = ? WHERE transaction_hash = ?",
            (value, transaction_hash),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to set description_override for %s", transaction_hash)
        raise


def set_notes(conn: sqlite3.Connection, transaction_hash: str, notes: str | None) -> None:
    """Set (or clear) a row's free-text note. LOCAL-ONLY — never sent to the LLM.

    Raises:
        sqlite3.Error: on connection failure.
    """
    value = notes.strip() if isinstance(notes, str) and notes.strip() else None
    try:
        conn.execute(
            "UPDATE transactions SET notes = ? WHERE transaction_hash = ?",
            (value, transaction_hash),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to set notes for %s", transaction_hash)
        raise


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
    # currency comes from the profile's default currency; COALESCE on update
    # keeps an already-set account currency if the profile omitted one.
    conn.execute(
        """
        INSERT INTO accounts (account_id, account_type, bank_name, currency)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (account_id) DO UPDATE SET
            account_type = excluded.account_type,
            bank_name = excluded.bank_name,
            currency = COALESCE(excluded.currency, accounts.currency)
        """,
        (profile.account_id, profile.account_type.value, profile.bank_name, profile.default_currency.value),
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
    # Recompute the pre-tracking flag for every account that received rows, so
    # newly-imported rows are correctly classified against the opening date.
    for account_id in df["account_id"].unique().tolist():
        recompute_tracking_flags(conn, str(account_id))
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
               a.opening_balance, a.opening_balance_date, a.currency,
               COUNT(t.transaction_hash) AS tx_count,
               MIN(t.date) AS min_date, MAX(t.date) AS max_date
        FROM accounts a
        LEFT JOIN transactions t ON t.account_id = a.account_id
        GROUP BY a.account_id, a.account_type, a.bank_name,
                 a.opening_balance, a.opening_balance_date, a.currency
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
    include_before: bool = False,
) -> pd.DataFrame:
    """Query transactions for the dashboard, pushing all filters into SQL.

    Currency is a SELECTOR, not a mixing filter: exactly one currency is
    queried at a time (BRL and EUR are never summed or converted). The
    effective display label (`description_effective`) is computed via the
    single `EFFECTIVE_DESCRIPTION_SQL` expression; the immutable original
    (`description`) and `description_override`/`notes` are also returned.

    Args:
        conn: An open SQLite connection.
        currency: The single ISO 4217 currency to render.
        start_iso, end_iso: Inclusive ISO date bounds (None = unbounded).
        account_ids: Restrict to these accounts (None/empty = all).
        categories: Restrict to these categories (None/empty = all).
        exclude_internal_transfers: Always True for spend metrics.
        include_before: If False (default), pre-tracking rows
            (is_before_tracking = 1) are excluded.

    Returns:
        A DataFrame (parsed `date`) filtered on indexed columns.
    """
    query = (
        f"SELECT transaction_hash, account_id, account_type, date, amount, currency, "
        f"description, description_override, {EFFECTIVE_DESCRIPTION_SQL} AS description_effective, "
        f"notes, category, is_internal_transfer, category_source, is_before_tracking "
        f"FROM transactions WHERE currency = ?"
    )
    params: list = [currency]
    if exclude_internal_transfers:
        query += " AND is_internal_transfer = 0"
    if not include_before:
        query += " AND is_before_tracking = 0"
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
    if "is_before_tracking" in df.columns:
        df["is_before_tracking"] = df["is_before_tracking"].astype(bool)
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

    Eligible = not an internal transfer, not manually edited, still
    'Uncategorized', and not before tracking. Optionally scoped to one account
    and/or a specific set of hashes (used to categorize ONLY newly-inserted rows).

    The `description` returned is the EFFECTIVE label (a user-corrected override
    is better LLM signal than the raw bank text). `notes` are LOCAL-ONLY and are
    deliberately never selected here — they must never reach the LLM.

    Returns:
        A DataFrame with columns [transaction_hash, description] (effective).
    """
    query = (
        f"SELECT transaction_hash, {EFFECTIVE_DESCRIPTION_SQL} AS description FROM transactions "
        f"WHERE is_internal_transfer = 0 AND category_source != 'manual' "
        f"AND category = 'Uncategorized' AND is_before_tracking = 0"
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


# ---------------------------------------------------------------------------
# Asset valuation ledger (non-liquid assets, e.g. ETFs)
# ---------------------------------------------------------------------------
# Manually-tracked valuations, kept STRICTLY separate from the transactional
# cash flow: they never enter spend/income metrics or account balances. Each
# asset carries its own currency; BRL and EUR are never mixed or converted.
def register_asset(conn: sqlite3.Connection, name: str, currency: str) -> int:
    """Register an asset (idempotent on name) and return its id.

    Uses `INSERT OR IGNORE` so re-registering an existing name is a no-op and
    keeps the original currency. The id is then looked up by the unique name.

    Args:
        conn: An open SQLite connection.
        name: The asset's display name (unique).
        currency: The asset's ISO 4217 currency ('BRL' or 'EUR').

    Returns:
        The asset's row id.

    Raises:
        sqlite3.Error: on constraint violation (e.g. invalid currency).
    """
    conn.execute("INSERT OR IGNORE INTO assets (name, currency) VALUES (?, ?)", (name, currency))
    conn.commit()
    row = conn.execute("SELECT id FROM assets WHERE name = ?", (name,)).fetchone()
    return int(row["id"])


def append_asset_valuation(
    conn: sqlite3.Connection, asset_id: int, date_iso: str, balance: float
) -> None:
    """Append (or upsert) a valuation snapshot for one asset on one date.

    Idempotent per (asset_id, date): re-logging the same date UPDATES the
    balance rather than inserting a duplicate.

    Args:
        conn: An open SQLite connection.
        asset_id: The owning asset's id.
        date_iso: ISO 8601 snapshot date.
        balance: The valuation in the asset's own currency.

    Raises:
        sqlite3.Error: on connection failure.
    """
    conn.execute(
        """
        INSERT INTO asset_valuation_history (asset_id, date, balance) VALUES (?, ?, ?)
        ON CONFLICT (asset_id, date) DO UPDATE SET balance = excluded.balance
        """,
        (int(asset_id), date_iso, float(balance)),
    )
    conn.commit()


def fetch_assets(conn: sqlite3.Connection, currency: str | None = None) -> list[dict]:
    """List registered assets, optionally scoped to a single currency.

    Args:
        conn: An open SQLite connection.
        currency: If set, restrict to assets of this single currency (BRL/EUR
            never mixed). If None, return all assets.

    Returns:
        One dict per asset with keys id, name, currency, ordered by name.
    """
    if currency is not None:
        cursor = conn.execute(
            "SELECT id, name, currency FROM assets WHERE currency = ? ORDER BY name", (currency,)
        )
    else:
        cursor = conn.execute("SELECT id, name, currency FROM assets ORDER BY name")
    return [dict(row) for row in cursor.fetchall()]


def fetch_asset_valuations(conn: sqlite3.Connection, currency: str) -> pd.DataFrame:
    """Fetch the valuation history for all assets of ONE currency.

    Single-currency by construction (BRL and EUR are never mixed). Used to plot
    the overlaid valuation-over-time chart.

    Args:
        conn: An open SQLite connection.
        currency: The single ISO 4217 currency to render.

    Returns:
        DataFrame [asset, date, balance] ordered by date (parsed `date`).
    """
    query = (
        "SELECT a.name AS asset, h.date AS date, h.balance AS balance "
        "FROM asset_valuation_history h "
        "JOIN assets a ON a.id = h.asset_id "
        "WHERE a.currency = ? ORDER BY h.date"
    )
    return pd.read_sql_query(query, conn, params=[currency], parse_dates=["date"])


def delete_asset(conn: sqlite3.Connection, asset_id: int) -> None:
    """Delete an asset AND its full valuation history.

    Raises:
        sqlite3.Error: on connection failure.
    """
    conn.execute("DELETE FROM asset_valuation_history WHERE asset_id = ?", (int(asset_id),))
    conn.execute("DELETE FROM assets WHERE id = ?", (int(asset_id),))
    conn.commit()
    logger.info("Deleted asset id=%s and its valuation history", asset_id)
