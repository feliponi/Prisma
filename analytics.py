"""Dashboard analytics: SQL-pushed, cached aggregations for the Financial Dashboard.

Filtering is pushed into SQL (WHERE on indexed columns) and the resulting
per-currency frame is cached with `@st.cache_data` keyed on the filter tuple
(+ a monotonic data version so writes bust the cache). BRL and EUR are NEVER
summed or converted — every function operates on a single already-selected
currency. Internal transfers are excluded at the query level.

No UI here — this module returns DataFrames/dicts; app.py renders them.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import streamlit as st

import db
from models import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)

PERIOD_PRESETS = [
    "Mês atual",
    "Últimos 3 meses",
    "Últimos 12 meses",
    "Ano atual",
    "Personalizado",
]

# TODO(FX): BRL<->EUR conversion is intentionally NOT implemented. If a future
# FX feature is added, a conversion hook would go here (rate lookup by date),
# and ONLY behind an explicit user opt-in — the default stays "never mix".


@dataclass(frozen=True)
class PeriodBounds:
    """Inclusive ISO date bounds for a period and its previous-equivalent window."""

    start: str
    end: str
    prev_start: str
    prev_end: str


def _iso(d: date) -> str:
    return d.isoformat()


def resolve_period(
    preset: str, today: date, custom_start: date | None = None, custom_end: date | None = None
) -> PeriodBounds:
    """Resolve a preset (or custom range) into current + previous-equivalent bounds.

    The previous window has the same length as the current one and ends the day
    before it starts, so the KPI delta compares like-for-like periods.
    """
    if preset == "Mês atual":
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    elif preset == "Últimos 3 meses":
        end = today
        start = _shift_months(today, -3) + timedelta(days=1)
    elif preset == "Últimos 12 meses":
        end = today
        start = _shift_months(today, -12) + timedelta(days=1)
    elif preset == "Ano atual":
        start = date(today.year, 1, 1)
        end = date(today.year, 12, 31)
    else:  # Personalizado
        start = custom_start or today.replace(day=1)
        end = custom_end or today

    if start > end:
        start, end = end, start

    length = (end - start).days
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length)
    return PeriodBounds(
        start=_iso(start), end=_iso(end),
        prev_start=_iso(prev_start), prev_end=_iso(prev_end),
    )


def _shift_months(d: date, months: int) -> date:
    """Shift a date by N months (clamping the day to the target month length)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@st.cache_data(show_spinner=False)
def load_transactions(
    data_version: int,
    currency: str,
    start: str,
    end: str,
    account_ids: tuple[str, ...],
    categories: tuple[str, ...],
    db_path: str = str(DEFAULT_DB_PATH),
) -> pd.DataFrame:
    """Load the filtered, single-currency, non-internal transactions (cached).

    Args:
        data_version: Monotonic counter; changing it busts the cache after writes.
        currency: The single currency to render.
        start, end: Inclusive ISO date bounds.
        account_ids: Accounts to include (empty tuple = all).
        categories: Categories to include (empty tuple = all).
        db_path: Database path (part of the cache key).

    Returns:
        A DataFrame filtered entirely in SQL.
    """
    conn = db.get_connection(db_path)
    try:
        return db.fetch_transactions_filtered(
            conn,
            currency=currency,
            start_iso=start,
            end_iso=end,
            account_ids=account_ids or None,
            categories=categories or None,
            exclude_internal_transfers=True,
        )
    finally:
        conn.close()


def compute_kpis(df: pd.DataFrame) -> dict[str, float]:
    """Compute expenses (abs of negatives), income (positives) and net for a frame."""
    if df.empty:
        return {"expenses": 0.0, "income": 0.0, "net": 0.0}
    expenses = float(-df.loc[df["amount"] < 0, "amount"].sum())
    income = float(df.loc[df["amount"] > 0, "amount"].sum())
    return {"expenses": expenses, "income": income, "net": income - expenses}


def monthly_evolution(df: pd.DataFrame) -> pd.DataFrame:
    """Per-month income, expense (positive magnitude) and net."""
    if df.empty:
        return pd.DataFrame(columns=["month", "Receitas", "Despesas", "Líquido"])
    work = df.copy()
    work["month"] = work["date"].dt.strftime("%Y-%m")
    grouped = work.groupby("month").agg(
        Receitas=("amount", lambda s: float(s[s > 0].sum())),
        Despesas=("amount", lambda s: float(-s[s < 0].sum())),
    ).reset_index()
    grouped["Líquido"] = grouped["Receitas"] - grouped["Despesas"]
    return grouped.sort_values("month").reset_index(drop=True)


def expense_by_category(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    """Descending expense-by-category with a Top-N + 'Outros' bucket."""
    spend = df[df["amount"] < 0].copy()
    if spend.empty:
        return pd.DataFrame(columns=["category", "spend"])
    spend["spend"] = -spend["amount"]
    totals = spend.groupby("category")["spend"].sum().sort_values(ascending=False)
    if len(totals) > top_n:
        top = totals.iloc[:top_n]
        others = float(totals.iloc[top_n:].sum())
        totals = pd.concat([top, pd.Series({"Outros": others})])
    return totals.reset_index().rename(columns={"index": "category"}).sort_values(
        "spend", ascending=False
    ).reset_index(drop=True)


def expense_by_account(df: pd.DataFrame) -> pd.DataFrame:
    """Expense magnitude per account (descending)."""
    spend = df[df["amount"] < 0].copy()
    if spend.empty:
        return pd.DataFrame(columns=["account_id", "spend"])
    spend["spend"] = -spend["amount"]
    return (
        spend.groupby("account_id")["spend"].sum()
        .sort_values(ascending=False).reset_index()
    )


def budget_vs_actual(df: pd.DataFrame, budgets: dict[str, float]) -> pd.DataFrame:
    """Actual expense vs planned budget, only for categories that have a budget.

    Returns:
        Columns [category, Orçado, Realizado, Estouro] where Estouro is the
        positive overrun (Realizado - Orçado, floored at 0). Empty if no budgets.
    """
    if not budgets:
        return pd.DataFrame(columns=["category", "Orçado", "Realizado", "Estouro"])
    spend = df[df["amount"] < 0].copy()
    spend["spend"] = -spend["amount"]
    actual = spend.groupby("category")["spend"].sum().to_dict()
    rows = []
    for category, planned in sorted(budgets.items()):
        realized = float(actual.get(category, 0.0))
        rows.append({
            "category": category,
            "Orçado": float(planned),
            "Realizado": realized,
            "Estouro": max(0.0, realized - float(planned)),
        })
    return pd.DataFrame(rows)
