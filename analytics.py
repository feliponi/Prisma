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
    include_before: bool = False,
    db_path: str = str(DEFAULT_DB_PATH),
) -> pd.DataFrame:
    """Load the filtered, single-currency, non-internal transactions (cached).

    Args:
        data_version: Monotonic counter; changing it busts the cache after writes.
        currency: The single currency to render.
        start, end: Inclusive ISO date bounds.
        account_ids: Accounts to include (empty tuple = all).
        categories: Categories to include (empty tuple = all).
        include_before: If True, also include pre-tracking rows (part of the
            cache key so the toggle busts the cache).
        db_path: Database path (part of the cache key).

    Returns:
        A DataFrame filtered entirely in SQL (with `description_effective`/`notes`).
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
            include_before=include_before,
        )
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def load_balance_series(
    data_version: int,
    currency: str,
    start: str,
    end: str,
    account_ids: tuple[str, ...],
    include_before: bool = False,
    db_path: str = str(DEFAULT_DB_PATH),
) -> tuple[pd.DataFrame, float]:
    """Compute the running-balance time series and the current balance (cached).

    Running balance INCLUDES internal transfers (they move real money within
    the account) and NEVER mixes currencies. Pre-tracking amounts are already
    baked into `opening_balance`; to keep totals correct while honoring the
    "include before" toggle, the baseline is DECOMPOSED rather than double-counted:

        - include_before = False: baseline = SUM(opening_balance); the cumulative
          series runs over post-tracking rows only.
        - include_before = True:  baseline = SUM(opening_balance) - SUM(pre-tracking
          amounts); the cumulative series runs over ALL rows (pre + post). The
          balance at/after the tracking date is identical to the False case;
          only the pre-tracking build-up becomes visible.

    Args:
        data_version, currency, start, end, account_ids, include_before, db_path:
            filter tuple (all part of the cache key).

    Returns:
        (series_df, current_balance) where series_df has columns [date, balance]
        for dates within [start, end], and current_balance is the latest balance.
    """
    conn = db.get_connection(db_path)
    try:
        opening_sum = db.opening_balance_sum(conn, account_ids, currency)
        ledger = db.fetch_account_ledger(conn, account_ids, currency, include_before=include_before)
        if include_before:
            pre = db.pre_tracking_amount_sum(conn, account_ids, currency)
            baseline = opening_sum - pre
        else:
            baseline = opening_sum
    finally:
        conn.close()
    series = balance_series(ledger, baseline, start, end)
    current = float(baseline + ledger["amount"].sum()) if not ledger.empty else float(baseline)
    return series, current


@st.cache_data(show_spinner=False)
def load_asset_valuations(
    data_version: int,
    currency: str,
    db_path: str = str(DEFAULT_DB_PATH),
) -> pd.DataFrame:
    """Load the valuation history of all assets of ONE currency (cached).

    Assets (non-liquid, e.g. ETFs) are tracked independently of the cash flow;
    BRL and EUR are never mixed. The cache is keyed on `data_version` so writes
    bust it, mirroring `load_transactions`.

    Args:
        data_version: Monotonic counter; changing it busts the cache after writes.
        currency: The single currency to render.
        db_path: Database path (part of the cache key).

    Returns:
        DataFrame [asset, date, balance] ordered by date.
    """
    conn = db.get_connection(db_path)
    try:
        return db.fetch_asset_valuations(conn, currency)
    finally:
        conn.close()


def balance_series(ledger: pd.DataFrame, baseline: float, start: str, end: str) -> pd.DataFrame:
    """Cumulative running balance per date, sliced to [start, end].

    The cumulative sum runs over the FULL ledger from `baseline` (so the balance
    at any date reflects all prior history), then only the in-period dates are
    returned for plotting.

    Args:
        ledger: DataFrame [date, amount] ordered by date.
        baseline: The balance just before the first ledger row.
        start, end: Inclusive ISO date bounds of the display window.

    Returns:
        DataFrame [date, balance] for the period (empty if no data).
    """
    if ledger.empty:
        return pd.DataFrame(columns=["date", "balance"])
    daily = ledger.groupby(ledger["date"].dt.normalize())["amount"].sum().sort_index()
    cumulative = baseline + daily.cumsum()
    out = cumulative.reset_index()
    out.columns = ["date", "balance"]
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    return out[(out["date"] >= start_ts) & (out["date"] <= end_ts)].reset_index(drop=True)


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
    # pd.concat above drops the Series/index names; set them explicitly so
    # reset_index() yields deterministic ["category", "spend"] columns.
    totals.index.name = "category"
    totals.name = "spend"
    return totals.reset_index().sort_values("spend", ascending=False).reset_index(drop=True)


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
