"""Backend module for account profile management and CSV sanitization.

Contracts only (Phase 1). Every function below is fully type-hinted and
documented but raises NotImplementedError; bodies land in Phase 2.

Responsibilities:
    - Persist/load per-account mapping profiles (`mappings/{account_id}_config.json`).
    - Transform an arbitrary raw bank/card CSV into the canonical
      `TransactionRecord` DataFrame: drop junk rows, parse dates and amounts
      per the account's declared locale, resolve the amount sign (layout
      convention, then `invert_sign`), resolve currency, tag internal
      transfers, and stamp the idempotent `transaction_hash`.

Does NOT persist to SQLite (see db.py) and does NOT call the LLM
(see ai_services.py). UI concerns live in app.py only.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from models import AccountProfile, DEFAULT_MAPPINGS_DIR

logger = logging.getLogger(__name__)


def list_profiles(mappings_dir: Path = DEFAULT_MAPPINGS_DIR) -> list[str]:
    """List account_ids for which a saved mapping profile exists on disk.

    Args:
        mappings_dir: Directory containing `{account_id}_config.json` files.

    Returns:
        Account IDs sorted alphabetically. Empty list if the directory
        does not exist or contains no valid profiles.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def load_profile(account_id: str, mappings_dir: Path = DEFAULT_MAPPINGS_DIR) -> AccountProfile | None:
    """Load a previously saved account profile by its account_id.

    Args:
        account_id: The unique account identifier (e.g. "nubank_cc").
        mappings_dir: Directory containing `{account_id}_config.json` files.

    Returns:
        The parsed AccountProfile, or None if no matching file exists or it
        fails validation.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def save_profile(profile: AccountProfile, mappings_dir: Path = DEFAULT_MAPPINGS_DIR) -> Path:
    """Persist an account profile as `{account_id}_config.json`.

    Args:
        profile: The account profile to save. `profile.account_id` determines
            the filename; saving with an existing account_id overwrites it.
        mappings_dir: Directory to write the JSON file into (created if absent).

    Returns:
        The path the profile was written to.

    Raises:
        OSError: if the mappings directory cannot be created or written to.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def process_csv(raw_bytes: bytes, profile: AccountProfile) -> pd.DataFrame:
    """Transform a raw bank/card CSV export into the canonical transaction DataFrame.

    Pipeline (Phase 2):
        1. Decode `raw_bytes` using `profile.encoding` and parse with
           `profile.delimiter`.
        2. Drop rows matching `profile.skip_rows_regex` (embedded
           headers/footers, e.g. "Saldo Anterior") and fully empty rows.
        3. Parse the date column per `profile.date_format`.
        4. Parse the amount per `profile.amount_sign_convention`
           ("signed" | "debit_credit_columns" | "parentheses"), using
           `profile.decimal_separator` / `profile.thousands_separator`.
        5. Apply `profile.invert_sign` AFTER layout resolution, to normalize
           into the canonical convention (spend negative, income/refund
           positive).
        6. Resolve `currency`: read per-row from `column_map.currency` if
           set (validated against the Currency enum, rejecting unknown
           values), else use `profile.default_currency` for every row.
        7. Tag `is_internal_transfer` via `profile.internal_transfer_regex`
           against the description; when True, force
           `category = "Transferência interna"`.
        8. Stamp `account_id` / `account_type` from the profile.
        9. Compute `transaction_hash` = sha256(
               f"{account_id}|{date_iso}|{amount}|{currency}|{description_normalized}"
           ) using `text_utils.normalize_description`.

    Args:
        raw_bytes: Raw CSV file content exactly as uploaded.
        profile: The account profile describing how to parse this CSV.

    Returns:
        A DataFrame with columns
        [transaction_hash, account_id, account_type, date, amount, currency,
         description, category, is_internal_transfer], matching
        `models.TransactionRecord` field-for-field. `category` defaults to
        "Uncategorized" except for internal-transfer rows.

    Raises:
        ValueError: if a mapped column is missing from the CSV, or a
            per-row currency value is not in the Currency enum.
        UnicodeDecodeError: if `raw_bytes` cannot be decoded with
            `profile.encoding`.
        pandas.errors.ParserError: if the CSV cannot be parsed at all.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _drop_skip_rows(df: pd.DataFrame, skip_rows_regex: str) -> pd.DataFrame:
    """Drop rows whose first column matches `skip_rows_regex`, plus fully-empty rows.

    Args:
        df: Raw DataFrame as read from the CSV, all-string dtype.
        skip_rows_regex: Anchored regex (e.g. "^(Saldo|Total|Balance)")
            identifying embedded header/footer rows to discard.

    Returns:
        The DataFrame with matching and empty rows removed.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _parse_date_column(series: pd.Series, date_format: str) -> pd.Series:
    """Parse a raw string date column using the profile's declared format.

    Args:
        series: Raw date strings.
        date_format: strptime-style format string, e.g. "%d/%m/%Y".

    Returns:
        A datetime64[ns] series. Unparseable values become NaT (and are
        expected to be dropped by the caller).

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _parse_decimal_series(
    series: pd.Series, decimal_separator: str, thousands_separator: str
) -> pd.Series:
    """Parse a raw numeric-string column into floats using declared locale separators.

    Args:
        series: Raw amount strings (sign already stripped of layout markers
            by the caller where applicable, e.g. parentheses).
        decimal_separator: "," or "." — which character denotes the decimal point.
        thousands_separator: "," or "." or "" — which character (if any) is
            a thousands grouping separator to strip before float conversion.

    Returns:
        A float64 series. Unparseable values become NaN.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _resolve_amount(raw_df: pd.DataFrame, profile: AccountProfile) -> pd.Series:
    """Resolve the canonical signed `amount` column from the raw CSV.

    Dispatches on `profile.amount_sign_convention`:
        - "signed": read `column_map.amount` directly (sign already present).
        - "debit_credit_columns": debit values become negative, credit
          values become positive, from `column_map.debit` / `column_map.credit`.
        - "parentheses": "(1.234,56)"-style values become negative.
    Then applies `profile.invert_sign` (multiplies the whole column by -1)
    to normalize into the canonical convention.

    Args:
        raw_df: Raw DataFrame as read from the CSV.
        profile: The account profile driving parsing rules.

    Returns:
        A float64 series of canonically-signed amounts, same index as raw_df.

    Raises:
        ValueError: if required amount/debit/credit columns are missing for
            the declared convention.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _resolve_currency(raw_df: pd.DataFrame, profile: AccountProfile) -> pd.Series:
    """Resolve the canonical `currency` column from the raw CSV.

    Reads per-row from `column_map.currency` when set (validating each value
    against the Currency enum and raising on anything else); otherwise fills
    every row with `profile.default_currency`.

    Args:
        raw_df: Raw DataFrame as read from the CSV.
        profile: The account profile driving currency resolution.

    Returns:
        A string series of ISO 4217 currency codes ("BRL" | "EUR").

    Raises:
        ValueError: if a per-row currency value is not in the Currency enum.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _tag_internal_transfers(description_series: pd.Series, internal_transfer_regex: str) -> pd.Series:
    """Flag rows whose description matches the account's internal-transfer pattern.

    Args:
        description_series: Raw (pre-normalization) description strings.
        internal_transfer_regex: Case-insensitive pattern identifying
            card-bill payments and similar self-transfers (e.g. "PAG.*fatura").

    Returns:
        A boolean series, True where the description matches.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _compute_transaction_hash(
    account_id: str, date_iso: str, amount: float, currency: str, description_normalized: str
) -> str:
    """Compute the idempotent primary-key hash for one transaction row.

    Formula: sha256(f"{account_id}|{date_iso}|{amount}|{currency}|{description_normalized}").

    Args:
        account_id: The owning account's identifier.
        date_iso: Transaction date formatted as "YYYY-MM-DD".
        amount: Canonically-signed float amount.
        currency: ISO 4217 currency code.
        description_normalized: Output of `text_utils.normalize_description`.

    Returns:
        A 64-character lowercase hex SHA-256 digest.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError
