"""Backend module for account profile management and CSV sanitization.

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

import hashlib
import io
import json
import logging
import re
from pathlib import Path

import pandas as pd

from models import (
    AccountProfile,
    AmountSignConvention,
    Currency,
    DEFAULT_CATEGORY,
    DEFAULT_MAPPINGS_DIR,
    INTERNAL_TRANSFER_CATEGORY,
)
from text_utils import normalize_description

logger = logging.getLogger(__name__)

CANONICAL_COLUMNS = [
    "transaction_hash",
    "account_id",
    "account_type",
    "date",
    "amount",
    "currency",
    "description",
    "category",
    "is_internal_transfer",
]


def _profile_path(account_id: str, mappings_dir: Path) -> Path:
    """Build the on-disk path for an account's mapping profile."""
    return mappings_dir / f"{account_id}_config.json"


def list_profiles(mappings_dir: Path = DEFAULT_MAPPINGS_DIR) -> list[str]:
    """List account_ids for which a saved mapping profile exists on disk.

    Args:
        mappings_dir: Directory containing `{account_id}_config.json` files.

    Returns:
        Account IDs sorted alphabetically. Empty list if the directory
        does not exist or contains no valid profiles.
    """
    if not mappings_dir.exists():
        return []
    account_ids: list[str] = []
    for path in sorted(mappings_dir.glob("*_config.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            account_ids.append(data["account_id"])
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to read profile file %s: %s", path, exc)
    return sorted(account_ids)


def load_profile(account_id: str, mappings_dir: Path = DEFAULT_MAPPINGS_DIR) -> AccountProfile | None:
    """Load a previously saved account profile by its account_id.

    Args:
        account_id: The unique account identifier (e.g. "nubank_cc").
        mappings_dir: Directory containing `{account_id}_config.json` files.

    Returns:
        The parsed AccountProfile, or None if no matching file exists or it
        fails validation.
    """
    path = _profile_path(account_id, mappings_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AccountProfile.from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.error("Failed to load profile for '%s': %s", account_id, exc)
        return None


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
    """
    mappings_dir.mkdir(parents=True, exist_ok=True)
    path = _profile_path(profile.account_id, mappings_dir)
    path.write_text(json.dumps(profile.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved profile for account '%s' to %s", profile.account_id, path)
    return path


def compute_schema_fingerprint(columns: list[str]) -> str:
    """Compute a stable fingerprint of a file's column set for profile reuse.

    Formula (contract — Phase 2 must match exactly so pre-saved fingerprints
    stay comparable): sha256 over the newline-joined, case-sensitively sorted
    detected column names:

        sha256("\\n".join(sorted(columns)).encode("utf-8")).hexdigest()

    Column ORDER does not affect the fingerprint (only the set/sorting does),
    so the same bank export reused month to month fingerprints identically.

    Args:
        columns: Detected file column names (from `RawTable.columns`).

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    joined = "\n".join(sorted(columns))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def find_profile_by_fingerprint(
    fingerprint: str, mappings_dir: Path = DEFAULT_MAPPINGS_DIR
) -> AccountProfile | None:
    """Find a saved profile whose stored `schema_fingerprint` matches.

    Enables the one-click "Reaproveitar mapeamento salvo" path: on a new
    upload the app computes the fingerprint and, if a saved profile matches,
    offers to skip manual mapping entirely.

    Args:
        fingerprint: The uploaded file's `compute_schema_fingerprint` value.
        mappings_dir: Directory containing `{account_id}_config.json` files.

    Returns:
        The first matching `AccountProfile` (by account_id order), or None.
    """
    if not fingerprint:
        return None
    for account_id in list_profiles(mappings_dir):
        profile = load_profile(account_id, mappings_dir)
        if profile is not None and profile.schema_fingerprint == fingerprint:
            return profile
    return None


def process_csv(raw_bytes: bytes, profile: AccountProfile) -> pd.DataFrame:
    """Transform a raw bank/card CSV export into the canonical transaction DataFrame.

    Pipeline:
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
    """
    try:
        text = raw_bytes.decode(profile.encoding)
    except UnicodeDecodeError as exc:
        logger.error("Failed to decode CSV for account '%s': %s", profile.account_id, exc)
        raise

    try:
        raw_df = pd.read_csv(
            io.StringIO(text),
            delimiter=profile.delimiter,
            dtype=str,
            engine="python",
            skip_blank_lines=True,
        )
    except pd.errors.ParserError as exc:
        logger.error("Failed to parse CSV for account '%s': %s", profile.account_id, exc)
        raise

    raw_df.columns = [str(c).strip() for c in raw_df.columns]

    required_columns = [profile.column_map.date, profile.column_map.description]
    if profile.amount_sign_convention == AmountSignConvention.DEBIT_CREDIT_COLUMNS:
        required_columns += [profile.column_map.debit, profile.column_map.credit]
    else:
        required_columns += [profile.column_map.amount]
    if profile.column_map.currency:
        required_columns.append(profile.column_map.currency)

    missing = [c for c in required_columns if c and c not in raw_df.columns]
    if missing:
        raise ValueError(
            f"Mapped columns not found in CSV for account '{profile.account_id}': {missing}. "
            f"Available columns: {list(raw_df.columns)}"
        )

    raw_df = _drop_skip_rows(raw_df, profile.skip_rows_regex)
    if raw_df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    date_series = _parse_date_column(raw_df[profile.column_map.date].astype(str).str.strip(), profile.date_format)
    amount_series = _resolve_amount(raw_df, profile)
    currency_series = _resolve_currency(raw_df, profile)
    description_series = raw_df[profile.column_map.description].astype(str).str.strip()

    canonical_df = pd.DataFrame(
        {
            "date": date_series,
            "amount": amount_series,
            "currency": currency_series,
            "description": description_series,
        }
    )
    canonical_df = canonical_df.dropna(subset=["date", "amount"])
    canonical_df = canonical_df[canonical_df["description"].str.len() > 0]
    canonical_df = canonical_df.reset_index(drop=True)

    if canonical_df.empty:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    canonical_df["account_id"] = profile.account_id
    canonical_df["account_type"] = profile.account_type.value

    is_internal = _tag_internal_transfers(canonical_df["description"], profile.internal_transfer_regex)
    canonical_df["is_internal_transfer"] = is_internal
    canonical_df["category"] = canonical_df["is_internal_transfer"].map(
        {True: INTERNAL_TRANSFER_CATEGORY, False: DEFAULT_CATEGORY}
    )

    date_iso_series = canonical_df["date"].dt.strftime("%Y-%m-%d")
    normalized_description_series = canonical_df["description"].map(normalize_description)

    canonical_df["transaction_hash"] = [
        _compute_transaction_hash(profile.account_id, date_iso, amount, currency, desc_norm)
        for date_iso, amount, currency, desc_norm in zip(
            date_iso_series,
            canonical_df["amount"],
            canonical_df["currency"],
            normalized_description_series,
        )
    ]

    logger.info(
        "Processed CSV for account '%s': %d valid transactions extracted",
        profile.account_id,
        len(canonical_df),
    )
    return canonical_df[CANONICAL_COLUMNS]


def _drop_skip_rows(df: pd.DataFrame, skip_rows_regex: str) -> pd.DataFrame:
    """Drop rows whose first column matches `skip_rows_regex`, plus fully-empty rows.

    Args:
        df: Raw DataFrame as read from the CSV, all-string dtype.
        skip_rows_regex: Anchored regex (e.g. "^(Saldo|Total|Balance)")
            identifying embedded header/footer rows to discard.

    Returns:
        The DataFrame with matching and empty rows removed.
    """
    df = df.dropna(how="all")
    if df.empty or not skip_rows_regex:
        return df
    first_column = df.columns[0]
    pattern = re.compile(skip_rows_regex)
    mask = df[first_column].astype(str).str.match(pattern)
    return df[~mask.fillna(False)]


def _parse_date_column(series: pd.Series, date_format: str) -> pd.Series:
    """Parse a raw string date column using the profile's declared format.

    Args:
        series: Raw date strings.
        date_format: strptime-style format string, e.g. "%d/%m/%Y".

    Returns:
        A datetime64[ns] series. Unparseable values become NaT (and are
        expected to be dropped by the caller).
    """
    return pd.to_datetime(series, format=date_format, errors="coerce")


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
    """
    string_series = series.astype(str).str.strip()
    string_series = string_series.str.replace("R$", "", regex=False)
    string_series = string_series.str.replace("$", "", regex=False)
    string_series = string_series.str.replace(" ", "", regex=False)
    if thousands_separator:
        string_series = string_series.str.replace(thousands_separator, "", regex=False)
    if decimal_separator != ".":
        string_series = string_series.str.replace(decimal_separator, ".", regex=False)
    string_series = string_series.replace({"": None, "nan": None, "None": None})
    return pd.to_numeric(string_series, errors="coerce")


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
    """
    convention = profile.amount_sign_convention
    decimal_sep = profile.decimal_separator
    thousands_sep = profile.thousands_separator

    if convention == AmountSignConvention.SIGNED:
        if not profile.column_map.amount:
            raise ValueError(f"'signed' convention requires column_map.amount for '{profile.account_id}'")
        amount = _parse_decimal_series(raw_df[profile.column_map.amount], decimal_sep, thousands_sep)

    elif convention == AmountSignConvention.DEBIT_CREDIT_COLUMNS:
        if not profile.column_map.debit or not profile.column_map.credit:
            raise ValueError(
                f"'debit_credit_columns' convention requires column_map.debit and "
                f"column_map.credit for '{profile.account_id}'"
            )
        debit = _parse_decimal_series(raw_df[profile.column_map.debit], decimal_sep, thousands_sep)
        credit = _parse_decimal_series(raw_df[profile.column_map.credit], decimal_sep, thousands_sep)
        amount = credit.fillna(0) - debit.abs().fillna(0)
        amount = amount.where(~(debit.isna() & credit.isna()), other=pd.NA)

    elif convention == AmountSignConvention.PARENTHESES:
        if not profile.column_map.amount:
            raise ValueError(f"'parentheses' convention requires column_map.amount for '{profile.account_id}'")
        raw_series = raw_df[profile.column_map.amount].astype(str).str.strip()
        is_negative = raw_series.str.match(r"^\(.*\)$")
        stripped = raw_series.str.replace(r"^\((.*)\)$", r"\1", regex=True)
        amount = _parse_decimal_series(stripped, decimal_sep, thousands_sep)
        amount = amount.where(~is_negative, -amount)

    else:
        raise ValueError(f"Unknown amount_sign_convention: {convention}")

    if profile.invert_sign:
        amount = -amount

    return amount


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
    """
    if profile.column_map.currency:
        raw_currency = raw_df[profile.column_map.currency].astype(str).str.strip().str.upper()
        valid_codes = {c.value for c in Currency}
        invalid = set(raw_currency.unique()) - valid_codes
        if invalid:
            raise ValueError(
                f"Unrecognized currency value(s) for account '{profile.account_id}': {invalid}. "
                f"Allowed: {sorted(valid_codes)}"
            )
        return raw_currency
    return pd.Series(profile.default_currency.value, index=raw_df.index)


def _tag_internal_transfers(description_series: pd.Series, internal_transfer_regex: str) -> pd.Series:
    """Flag rows whose description matches the account's internal-transfer pattern.

    Args:
        description_series: Raw (pre-normalization) description strings.
        internal_transfer_regex: Case-insensitive pattern identifying
            card-bill payments and similar self-transfers (e.g. "PAG.*fatura").

    Returns:
        A boolean series, True where the description matches.
    """
    if not internal_transfer_regex:
        return pd.Series(False, index=description_series.index)
    pattern = re.compile(internal_transfer_regex, re.IGNORECASE)
    return description_series.str.contains(pattern, regex=True, na=False)


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
    """
    key = f"{account_id}|{date_iso}|{amount}|{currency}|{description_normalized}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
