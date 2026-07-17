"""Shared data contracts for the Personal Finance MVP.

Defines the canonical transaction record, account profile schema, and the
supporting enums/TypedDicts used across csv_mapper, ai_services, db, and app.
This module is pure data structure — no I/O, no business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TypedDict

import pandas as pd


class AccountType(str, Enum):
    """Enum of supported account kinds. Drives amount-sign resolution rules."""

    BANK_ACCOUNT = "bank_account"
    CREDIT_CARD = "credit_card"


class Currency(str, Enum):
    """ISO 4217 currencies supported by this MVP. No FX conversion between them."""

    BRL = "BRL"
    EUR = "EUR"


class AmountSignConvention(str, Enum):
    """How the raw CSV encodes the sign of a transaction amount."""

    SIGNED = "signed"
    DEBIT_CREDIT_COLUMNS = "debit_credit_columns"
    PARENTHESES = "parentheses"


@dataclass(frozen=True)
class ColumnMap:
    """Maps canonical fields to source CSV column names for one account profile.

    Exactly one of (amount) or (debit, credit) is populated, depending on the
    profile's `amount_sign_convention`. `currency` is None when the account
    uses a single fixed `default_currency` instead of a per-row column.
    """

    date: str
    amount: str | None
    debit: str | None
    credit: str | None
    description: str
    currency: str | None


@dataclass(frozen=True)
class AccountProfile:
    """Persisted configuration describing how to parse one bank/card CSV export.

    Mirrors the `mappings/{account_id}_config.json` on-disk contract exactly.
    Immutable: any edit produces a new profile that is re-saved under the
    same `account_id`.
    """

    account_id: str
    account_type: AccountType
    bank_name: str
    invert_sign: bool
    column_map: ColumnMap
    date_format: str
    decimal_separator: str
    thousands_separator: str
    encoding: str
    delimiter: str
    amount_sign_convention: AmountSignConvention
    default_currency: Currency
    skip_rows_regex: str
    internal_transfer_regex: str

    def to_dict(self) -> dict:
        """Serialize this profile to a JSON-compatible dict."""
        return {
            "account_id": self.account_id,
            "account_type": self.account_type.value,
            "bank_name": self.bank_name,
            "invert_sign": self.invert_sign,
            "column_map": {
                "date": self.column_map.date,
                "amount": self.column_map.amount,
                "debit": self.column_map.debit,
                "credit": self.column_map.credit,
                "description": self.column_map.description,
                "currency": self.column_map.currency,
            },
            "date_format": self.date_format,
            "decimal_separator": self.decimal_separator,
            "thousands_separator": self.thousands_separator,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "amount_sign_convention": self.amount_sign_convention.value,
            "default_currency": self.default_currency.value,
            "skip_rows_regex": self.skip_rows_regex,
            "internal_transfer_regex": self.internal_transfer_regex,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AccountProfile":
        """Deserialize a profile from a dict loaded from `{account_id}_config.json`.

        Args:
            data: Parsed JSON matching the AccountProfile contract.

        Raises:
            KeyError: if a required field is missing.
            ValueError: if an enum field holds an unrecognized value.
        """
        column_map_data = data["column_map"]
        column_map = ColumnMap(
            date=column_map_data["date"],
            amount=column_map_data.get("amount"),
            debit=column_map_data.get("debit"),
            credit=column_map_data.get("credit"),
            description=column_map_data["description"],
            currency=column_map_data.get("currency"),
        )
        return cls(
            account_id=data["account_id"],
            account_type=AccountType(data["account_type"]),
            bank_name=data["bank_name"],
            invert_sign=bool(data["invert_sign"]),
            column_map=column_map,
            date_format=data["date_format"],
            decimal_separator=data["decimal_separator"],
            thousands_separator=data["thousands_separator"],
            encoding=data["encoding"],
            delimiter=data["delimiter"],
            amount_sign_convention=AmountSignConvention(data["amount_sign_convention"]),
            default_currency=Currency(data["default_currency"]),
            skip_rows_regex=data["skip_rows_regex"],
            internal_transfer_regex=data["internal_transfer_regex"],
        )


@dataclass(frozen=True)
class TransactionRecord:
    """Canonical transaction row, matching the SQLite `transactions` table 1:1.

    Sign convention: spend is always NEGATIVE, income/refund is always
    POSITIVE, regardless of account_type or how the source CSV encoded it.
    """

    transaction_hash: str
    account_id: str
    account_type: AccountType
    date: datetime
    amount: float
    currency: Currency
    description: str
    category: str
    is_internal_transfer: bool = False


class AccountSummary(TypedDict):
    """Row shape returned when listing known accounts (from the `accounts` table)."""

    account_id: str
    account_type: str
    bank_name: str


class SpendingAggregate(TypedDict):
    """Per-account, per-currency category spending totals, internal transfers excluded.

    `category_totals` maps category name -> summed spend magnitude (positive
    float) for that category within this account+currency slice.
    """

    account_id: str
    currency: str
    category_totals: dict[str, float]


class BudgetEntry(TypedDict):
    """A single planned-budget line, scoped to one category and one currency.

    Budgets are never scoped to a single account: the same category budget
    applies across all accounts sharing that currency (per the Non-Goals, no
    cross-currency conversion or summation ever occurs).
    """

    category: str
    currency: str
    planned_amount: float


class CategoriesTaxonomy(TypedDict):
    """Shape of `categories.json`, the single source of truth for category names.

    `llm_categories` is the closed list the categorization LLM must choose
    from (plus its own hardcoded "Outros" fallback, which must also appear
    here). `system_categories` are sentinel values assigned by code paths
    that never invoke the LLM (default-on-import, internal transfers).
    """

    llm_categories: list[str]
    system_categories: list[str]


class SessionStateSchema(TypedDict, total=False):
    """Contract for Streamlit `st.session_state` keys used by app.py.

    Declared here (rather than only inline in app.py) so csv_mapper/ai_services
    consumers and tests can reason about the exact shape without importing
    Streamlit. All keys are optional (`total=False`) since they are populated
    incrementally as the user progresses through the pipeline.
    """

    active_account_id: str | None
    bank_profile: AccountProfile | None
    mapping_dict: dict[str, str | None]
    sanitized_df: pd.DataFrame | None
    categorized_df: pd.DataFrame | None
    category_cache: dict[str, str]
    budget_by_category: dict[str, dict[str, float]]  # currency -> {category: amount}


DEFAULT_CATEGORY = "Uncategorized"
INTERNAL_TRANSFER_CATEGORY = "Transferência interna"
LLM_FALLBACK_CATEGORY = "Outros"

DEFAULT_MAPPINGS_DIR = Path("mappings")
DEFAULT_DB_PATH = Path("finance.db")
DEFAULT_CATEGORIES_PATH = Path("categories.json")
