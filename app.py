"""Streamlit UI for the Personal Finance MVP.

Contracts only (Phase 1). Every render/handler function below is fully
type-hinted and documented but raises NotImplementedError; bodies land in
Phase 2.

Pipeline: Select/Create Account -> Upload -> Map -> Preview -> Categorize
(spinner) -> Insights. UI is strictly separated from transform/AI logic:
this module orchestrates calls into csv_mapper.py, ai_services.py, and
db.py, and owns `st.session_state` only.

All user-facing strings (labels, buttons, messages) MUST be pt_BR;
identifiers/comments/logs stay English.
"""

from __future__ import annotations

import logging

import pandas as pd

from models import AccountProfile, SessionStateSchema

logger = logging.getLogger(__name__)

NEW_ACCOUNT_LABEL = "+ Nova conta"


def init_session_state() -> None:
    """Populate `st.session_state` with the SessionStateSchema defaults if unset.

    Must be called once at the top of every script rerun before any other
    render function reads session_state, so widget interactions never reset
    pipeline state (uploaded file, mapping, sanitized/categorized data,
    category cache, budgets).

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_account_selector() -> str | None:
    """Render the account picker: an existing `account_id` or "+ Nova conta".

    On selecting an existing account, loads its AccountProfile via
    `csv_mapper.load_profile` into `session_state["bank_profile"]` and sets
    `session_state["active_account_id"]`. On "+ Nova conta", clears
    `active_account_id` so `render_account_creation_form` takes over.

    Returns:
        The selected account_id, or None while creating a new account.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_account_creation_form() -> AccountProfile | None:
    """Render the new-account form: account_id, account_type, bank_name, locale,
    sign convention, invert_sign, default_currency, internal_transfer_regex.

    On submit, builds an AccountProfile, persists it via
    `csv_mapper.save_profile` and `db.upsert_account`, and sets it as the
    active account in session_state.

    Returns:
        The newly created AccountProfile, or None until the form is submitted.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_upload_section() -> bytes | None:
    """Render the CSV file uploader, scoped to the active account.

    Returns:
        The raw uploaded file bytes, or None if nothing has been uploaded
        yet in this session for the active account.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_mapping_section(raw_bytes: bytes) -> AccountProfile | None:
    """Render CSV column mapping controls when the active account's profile
    does not yet fully cover the uploaded file's columns.

    For an existing account profile, auto-applies the saved mapping without
    prompting. For a fresh profile still being built (from
    `render_account_creation_form`), presents column dropdowns bound to the
    uploaded CSV's header row and persists the completed mapping.

    Args:
        raw_bytes: The uploaded CSV's raw bytes, for header introspection.

    Returns:
        The AccountProfile to use for processing, or None until mapping is complete.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_preview_section(raw_bytes: bytes, profile: AccountProfile) -> pd.DataFrame | None:
    """Run `csv_mapper.process_csv` and display the sanitized transaction preview.

    Persists the result to `session_state["sanitized_df"]` and, on user
    confirmation, writes it to SQLite via `db.insert_transactions`
    (idempotent — re-imports report zero new rows).

    Args:
        raw_bytes: The uploaded CSV's raw bytes.
        profile: The resolved account profile to sanitize with.

    Returns:
        The sanitized DataFrame, or None if processing failed (error is
        surfaced to the user via `st.error` in pt_BR).

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_categorization_section() -> pd.DataFrame | None:
    """Trigger `ai_services.categorize_transactions` for the active account's
    sanitized (non-internal-transfer) rows, showing a pt_BR loading spinner.

    Reuses and updates `session_state["category_cache"]` so repeated
    merchants across accounts are categorized once. Internal-transfer rows
    keep their forced "Transferência interna" category and are skipped.

    Returns:
        The categorized DataFrame, or None until the user triggers
        categorization or if the Ollama call fails (error surfaced in pt_BR).

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_budget_editor() -> dict[str, dict[str, float]]:
    """Render per-category, per-currency budget input fields.

    Persists edits to `session_state["budget_by_category"]`
    (`{currency: {category: planned_amount}}`), the source of truth consumed
    by `render_insights_section`.

    Returns:
        The current budget mapping after this render pass.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_insights_section() -> None:
    """Render the two insight views and trigger `ai_services.generate_financial_insights`.

    Views:
        (a) Per-account: spend vs. budget for the single active account.
        (b) Consolidated: spend vs. budget aggregated across all accounts
            sharing the same currency (never mixed across BRL/EUR).
    Internal transfers are excluded from both. Displays a pt_BR loading
    spinner while the Ollama call is in flight.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def main() -> None:
    """Entry point: wires the full Select Account -> Upload -> Map -> Preview
    -> Categorize -> Insights pipeline in order, gated on session_state so
    each stage only renders once its prerequisite state is populated.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


if __name__ == "__main__":
    main()
