"""Streamlit UI for the Personal Finance MVP.

Pipeline: Select/Create Account -> Upload -> Map -> Preview -> Categorize
(spinner) -> Insights. UI is strictly separated from transform/AI logic:
this module orchestrates calls into csv_mapper.py, ai_services.py, and
db.py, and owns `st.session_state` only.

All user-facing strings (labels, buttons, messages) are pt_BR;
identifiers/comments/logs stay English.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3

import pandas as pd
import streamlit as st

import ai_services
import csv_mapper
import db
import file_ingest
import mapping_ui
from models import (
    AccountProfile,
    AccountType,
    AmountSignConvention,
    ColumnMap,
    Currency,
    DEFAULT_CATEGORIES_PATH,
    DEFAULT_DB_PATH,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NEW_ACCOUNT_LABEL = "+ Nova conta"


@st.cache_resource
def _get_db_connection() -> sqlite3.Connection:
    """Open (and lazily initialize) the singleton SQLite connection for this app."""
    conn = db.get_connection(DEFAULT_DB_PATH)
    db.init_schema(conn)
    db.seed_categories(conn, DEFAULT_CATEGORIES_PATH)
    return conn


def _load_categories_taxonomy() -> list[str]:
    """Load the LLM-selectable category list from categories.json."""
    data = json.loads(DEFAULT_CATEGORIES_PATH.read_text(encoding="utf-8"))
    return list(data["llm_categories"])


def init_session_state() -> None:
    """Populate `st.session_state` with the SessionStateSchema defaults if unset.

    Must be called once at the top of every script rerun before any other
    render function reads session_state, so widget interactions never reset
    pipeline state (uploaded file, mapping, sanitized/categorized data,
    category cache, budgets).
    """
    defaults = {
        "active_account_id": None,
        "bank_profile": None,
        "uploaded_bytes": None,
        "sanitized_df": None,
        "categorized_df": None,
        "category_cache": {},
        "budget_by_category": {},
        "pending_new_account": None,
        # --- Interactive-import pipeline keys (Phase 2 wiring) ------------
        "raw_table": None,  # file_ingest.RawTable of the current upload
        "suggested_mapping": None,  # mapping_ui.suggest_mapping output
        "mapping_result": None,  # mapping_ui.MappingResult from the visual step
        "header_row": 0,  # user-chosen header row index (Excel/PDF)
        "selected_sheet": None,  # user-chosen Excel sheet name
        "source_type": None,  # "csv" | "xls" | "pdf" of the current upload
        "csv_dialect": None,  # file_ingest.CsvDialect after sniff + overrides
        "pdf_extraction": None,  # file_ingest.PdfExtractionResult, if PDF
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def render_account_selector() -> str | None:
    """Render the account picker: an existing `account_id` or "+ Nova conta".

    On selecting an existing account, loads its AccountProfile via
    `csv_mapper.load_profile` into `session_state["bank_profile"]` and sets
    `session_state["active_account_id"]`. On "+ Nova conta", clears
    `active_account_id` so `render_account_creation_form` takes over.

    Returns:
        The selected account_id, or None while creating a new account.
    """
    st.header("1. Selecionar ou criar conta")

    existing_accounts = csv_mapper.list_profiles()
    options = [NEW_ACCOUNT_LABEL] + existing_accounts
    active_account_id = st.session_state.get("active_account_id")
    default_index = options.index(active_account_id) if active_account_id in options else 0
    # Key is derived from active_account_id (not a fixed constant), so when
    # it changes programmatically (e.g. right after a new account is
    # created in render_mapping_section), the widget re-anchors to it on
    # the next rerun instead of snapping back to "+ Nova conta".
    choice = st.selectbox(
        "Conta", options, index=default_index, key=f"account_choice_{active_account_id or 'new'}"
    )

    if choice == NEW_ACCOUNT_LABEL:
        st.session_state["active_account_id"] = None
        st.session_state["bank_profile"] = None
        return None

    if st.session_state.get("active_account_id") != choice:
        profile = csv_mapper.load_profile(choice)
        if profile is None:
            st.error(f"Não foi possível carregar o perfil da conta '{choice}'.")
            return None
        st.session_state["active_account_id"] = choice
        st.session_state["bank_profile"] = profile
        st.session_state["sanitized_df"] = None
        st.session_state["categorized_df"] = None
        st.session_state["pending_new_account"] = None

    st.success(f"Conta ativa: {choice}")
    return choice


def render_account_creation_form() -> dict | None:
    """Render the new-account form: account_id, account_type, bank_name, locale,
    sign convention, invert_sign, default_currency, internal_transfer_regex.

    Column mapping (which CSV column maps to date/amount/description/etc.)
    is deferred to `render_mapping_section`, since it requires the uploaded
    CSV's header row. This form collects everything else and stores it as a
    pending draft in `session_state["pending_new_account"]`.

    Returns:
        The pending profile-fields dict, or None until the form is submitted.
    """
    st.header("1b. Detalhes da nova conta")

    with st.form("account_creation_form"):
        account_id = st.text_input("Identificador da conta (ex: nubank_cc)")
        account_type = st.selectbox(
            "Tipo de conta", [t.value for t in AccountType],
            format_func=lambda v: "Conta bancária" if v == "bank_account" else "Cartão de crédito",
        )
        bank_name = st.text_input("Nome do banco/instituição")
        default_currency = st.selectbox("Moeda padrão", [c.value for c in Currency])
        amount_sign_convention = st.selectbox(
            "Convenção de sinal do valor",
            [c.value for c in AmountSignConvention],
            format_func=lambda v: {
                "signed": "Coluna única com sinal",
                "debit_credit_columns": "Colunas separadas de débito/crédito",
                "parentheses": "Negativos entre parênteses",
            }[v],
        )
        invert_sign = st.checkbox(
            "Inverter sinal (ex: cartão de crédito lista compras como positivo)"
        )
        date_format = st.text_input("Formato de data (ex: %d/%m/%Y)", value="%d/%m/%Y")
        decimal_separator = st.selectbox("Separador decimal", [",", "."])
        thousands_separator = st.selectbox("Separador de milhar", [".", ",", ""])
        encoding = st.text_input("Codificação do arquivo", value="utf-8")
        delimiter = st.text_input("Delimitador do CSV", value=",")
        skip_rows_regex = st.text_input(
            "Regex para ignorar linhas (rodapé/cabeçalho embutido)",
            value="^(Saldo|Total|Balance)",
        )
        internal_transfer_regex = st.text_input(
            "Regex para transferência interna (ex: pagamento de fatura)",
            value="(?i)PAG.*FATURA|PGTO CARTAO|BILL PAYMENT",
        )
        submitted = st.form_submit_button("Continuar para mapeamento de colunas")

    if not submitted:
        return None

    if not account_id.strip() or not bank_name.strip():
        st.error("Informe um identificador de conta e um nome de banco válidos.")
        return None

    if account_id.strip() in csv_mapper.list_profiles():
        st.error(f"Já existe uma conta com o identificador '{account_id.strip()}'.")
        return None

    pending = {
        "account_id": account_id.strip(),
        "account_type": account_type,
        "bank_name": bank_name.strip(),
        "invert_sign": invert_sign,
        "date_format": date_format,
        "decimal_separator": decimal_separator,
        "thousands_separator": thousands_separator,
        "encoding": encoding.strip() or "utf-8",
        "delimiter": delimiter.strip() or ",",
        "amount_sign_convention": amount_sign_convention,
        "default_currency": default_currency,
        "skip_rows_regex": skip_rows_regex,
        "internal_transfer_regex": internal_transfer_regex,
    }
    st.session_state["pending_new_account"] = pending
    return pending


def render_upload_section() -> bytes | None:
    """Render the CSV file uploader, scoped to the active account.

    Returns:
        The raw uploaded file bytes, or None if nothing has been uploaded
        yet in this session for the active account.
    """
    st.header("2. Importar extrato (CSV)")
    uploaded_file = st.file_uploader("Selecione o arquivo CSV", type=["csv"], key="csv_uploader")

    if uploaded_file is not None:
        st.session_state["uploaded_bytes"] = uploaded_file.getvalue()

    return st.session_state.get("uploaded_bytes")


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
    """
    existing_profile = st.session_state.get("bank_profile")
    if existing_profile is not None:
        return existing_profile

    pending = st.session_state.get("pending_new_account")
    if pending is None:
        return None

    st.header("3. Mapear colunas")

    try:
        header_preview = pd.read_csv(
            io.BytesIO(raw_bytes), nrows=5, dtype=str, engine="python", delimiter=pending["delimiter"]
        )
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Não foi possível ler o CSV enviado: {exc}")
        return None

    available_columns = list(header_preview.columns)
    st.caption("Prévia das primeiras linhas do arquivo:")
    st.dataframe(header_preview, use_container_width=True)

    convention = pending["amount_sign_convention"]

    with st.form("column_mapping_form"):
        date_column = st.selectbox("Coluna de data", available_columns)
        description_column = st.selectbox("Coluna de descrição", available_columns)

        amount_column = None
        debit_column = None
        credit_column = None
        if convention == AmountSignConvention.DEBIT_CREDIT_COLUMNS.value:
            debit_column = st.selectbox("Coluna de débito", available_columns)
            credit_column = st.selectbox("Coluna de crédito", available_columns)
        else:
            amount_column = st.selectbox("Coluna de valor", available_columns)

        use_currency_column = st.checkbox("Moeda varia por linha (coluna própria)?")
        currency_column = None
        if use_currency_column:
            currency_column = st.selectbox("Coluna de moeda", available_columns)

        submitted = st.form_submit_button("Salvar mapeamento e continuar")

    if not submitted:
        return None

    column_map = ColumnMap(
        date=date_column,
        amount=amount_column,
        debit=debit_column,
        credit=credit_column,
        description=description_column,
        currency=currency_column,
    )

    profile = AccountProfile(
        account_id=pending["account_id"],
        account_type=AccountType(pending["account_type"]),
        bank_name=pending["bank_name"],
        invert_sign=pending["invert_sign"],
        column_map=column_map,
        date_format=pending["date_format"],
        decimal_separator=pending["decimal_separator"],
        thousands_separator=pending["thousands_separator"],
        encoding=pending["encoding"],
        delimiter=pending["delimiter"],
        amount_sign_convention=AmountSignConvention(convention),
        default_currency=Currency(pending["default_currency"]),
        skip_rows_regex=pending["skip_rows_regex"],
        internal_transfer_regex=pending["internal_transfer_regex"],
    )

    try:
        csv_mapper.save_profile(profile)
    except OSError as exc:
        st.error(f"Falha ao salvar o perfil de mapeamento: {exc}")
        return None

    conn = _get_db_connection()
    db.upsert_account(conn, profile)

    st.session_state["bank_profile"] = profile
    # Setting active_account_id here re-anchors the account selector widget
    # (keyed off active_account_id) to this account on the NEXT rerun,
    # instead of falling back to "+ Nova conta" and wiping this state.
    st.session_state["active_account_id"] = profile.account_id
    st.session_state["pending_new_account"] = None
    st.success(f"Conta '{profile.account_id}' criada e mapeamento salvo.")
    return profile


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
    """
    st.header("4. Prévia dos dados sanitizados")

    try:
        sanitized_df = csv_mapper.process_csv(raw_bytes, profile)
    except ValueError as exc:
        st.error(f"Erro no mapeamento de colunas: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Erro ao processar o CSV: {exc}")
        return None

    if sanitized_df.empty:
        st.warning("Nenhuma transação válida foi encontrada após a sanitização dos dados.")
        return None

    st.session_state["sanitized_df"] = sanitized_df
    st.dataframe(sanitized_df, use_container_width=True)
    st.caption(f"{len(sanitized_df)} transações válidas encontradas.")

    if st.button("Salvar transações no banco de dados local"):
        conn = _get_db_connection()
        inserted = db.insert_transactions(conn, sanitized_df)
        st.success(f"{inserted} novas transações importadas (duplicatas foram ignoradas).")

    return sanitized_df


def render_categorization_section() -> pd.DataFrame | None:
    """Trigger `ai_services.categorize_transactions` for the active account's
    sanitized (non-internal-transfer) rows, showing a pt_BR loading spinner.

    Reuses and updates `session_state["category_cache"]` so repeated
    merchants across accounts are categorized once. Internal-transfer rows
    keep their forced "Transferência interna" category and are skipped.

    Returns:
        The categorized DataFrame, or None until the user triggers
        categorization or if the Ollama call fails (error surfaced in pt_BR).
    """
    sanitized_df = st.session_state.get("sanitized_df")
    if sanitized_df is None:
        return None

    st.header("5. Categorização automática com IA local")

    if st.button("Categorizar transações com IA"):
        categories = _load_categories_taxonomy()
        categorizable_mask = ~sanitized_df["is_internal_transfer"]
        categorizable_df = sanitized_df[categorizable_mask]

        with st.spinner("Categorizando transações com o modelo local..."):
            try:
                description_to_category = ai_services.categorize_transactions(
                    categorizable_df["description"].tolist(),
                    categories=categories,
                    cache=st.session_state["category_cache"],
                )
            except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
                st.error(f"Falha ao categorizar transações: {exc}")
                return None

        categorized_df = sanitized_df.copy()
        categorized_df.loc[categorizable_mask, "category"] = categorized_df.loc[
            categorizable_mask, "description"
        ].map(description_to_category)

        conn = _get_db_connection()
        category_by_hash = dict(zip(categorized_df["transaction_hash"], categorized_df["category"]))
        db.update_categories(conn, category_by_hash)

        st.session_state["categorized_df"] = categorized_df

    categorized_df = st.session_state.get("categorized_df")
    if categorized_df is not None:
        st.dataframe(categorized_df, use_container_width=True)

    return categorized_df


def render_budget_editor() -> dict[str, dict[str, float]]:
    """Render per-category, per-currency budget input fields.

    Persists edits to `session_state["budget_by_category"]`
    (`{currency: {category: planned_amount}}`), the source of truth consumed
    by `render_insights_section`.

    Returns:
        The current budget mapping after this render pass.
    """
    st.subheader("Orçamento planejado")

    conn = _get_db_connection()
    aggregates = db.compute_spending_aggregates(conn)
    currencies_present = sorted({agg["currency"] for agg in aggregates})

    if not currencies_present:
        st.caption("Nenhum gasto registrado ainda.")
        return st.session_state["budget_by_category"]

    for currency in currencies_present:
        st.markdown(f"**{currency}**")
        categories_for_currency = sorted(
            {cat for agg in aggregates if agg["currency"] == currency for cat in agg["category_totals"]}
        )
        currency_budgets = st.session_state["budget_by_category"].setdefault(currency, {})
        for category in categories_for_currency:
            default_value = currency_budgets.get(category, 0.0)
            value = st.number_input(
                f"Orçamento - {category} ({currency})",
                min_value=0.0,
                value=float(default_value),
                step=50.0,
                key=f"budget_{currency}_{category}",
            )
            currency_budgets[category] = value

    return st.session_state["budget_by_category"]


def render_insights_section() -> None:
    """Render the two insight views and trigger `ai_services.generate_financial_insights`.

    Views:
        (a) Per-account: spend vs. budget for the single active account.
        (b) Consolidated: spend vs. budget aggregated across all accounts
            sharing the same currency (never mixed across BRL/EUR).
    Internal transfers are excluded from both. Displays a pt_BR loading
    spinner while the Ollama call is in flight.
    """
    st.header("6. Insights de conciliação orçamentária")

    budget_by_category = render_budget_editor()
    budget_entries = [
        {"category": category, "currency": currency, "planned_amount": amount}
        for currency, categories in budget_by_category.items()
        for category, amount in categories.items()
    ]

    conn = _get_db_connection()
    active_account_id = st.session_state.get("active_account_id")

    view = st.radio("Visão", ["Por conta", "Consolidada (mesma moeda)"], horizontal=True)

    if view == "Por conta":
        if not active_account_id:
            st.caption("Selecione uma conta para ver os insights por conta.")
            return
        aggregates = db.compute_spending_aggregates(conn, account_id=active_account_id)
    else:
        aggregates = db.compute_spending_aggregates(conn)

    if not aggregates:
        st.caption("Nenhum gasto registrado ainda para gerar insights.")
        return

    if st.button("Gerar insights com IA"):
        with st.spinner("Gerando análise executiva com o modelo local..."):
            insights_text = ai_services.generate_financial_insights(aggregates, budget_entries)
        st.session_state["insights_text"] = insights_text

    insights_text = st.session_state.get("insights_text")
    if insights_text:
        st.subheader("Resumo executivo")
        st.write(insights_text)


# ---------------------------------------------------------------------------
# Interactive-import pipeline (Phase 1 contracts — NOT yet wired into main()).
#
# These stubs describe the NEW pipeline order that will REPLACE the current
# "Select/Create Account -> Upload -> Map" section in Phase 2:
#
#   1. Upload (CSV / XLS / XLSX / PDF)
#   2. Ingest  -> file_ingest -> RawTable (+ CsvDialect / sheet / PDF review)
#   3. Show detected columns + sample values
#   4. Bind file to account (existing OR "Nova conta")
#   5. Visual drag-and-drop mapping (mapping_ui.render_mapping_ui)
#   6. Declare locale / sign / currency / internal_transfer_regex
#   7. Validate + preview canonical DataFrame -> Save profile
#
# Categorize / Insights downstream stages are unchanged. main() is left on the
# current working flow until these bodies land in Phase 2.
# ---------------------------------------------------------------------------


def render_file_ingest_section() -> "file_ingest.RawTable | None":
    """Upload + ingest step: accept CSV/XLS/XLSX/PDF and produce a `RawTable`.

    Dispatches on file type: CSV via `sniff_csv` (+ user-overridable dialect
    widgets) -> `read_csv`; Excel via `list_excel_sheets` + sheet/header-row
    pickers -> `read_excel`; PDF via strategy/header-row pickers -> `read_pdf`,
    surfacing `PdfExtractionResult.warnings` and requiring confirmation when
    `needs_manual_review` is True. Persists the result and its provenance
    (`raw_table`, `source_type`, `csv_dialect`, `selected_sheet`,
    `header_row`, `pdf_extraction`) to session_state.

    Returns:
        The ingested `RawTable`, or None until a file is ingested (or when a
        PDF extraction still awaits manual review/abort).

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_column_preview(raw: "file_ingest.RawTable") -> None:
    """Show the detected columns and sample values from an ingested `RawTable`.

    Args:
        raw: The ingested source-agnostic table to preview.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_account_binding(raw: "file_ingest.RawTable") -> str | None:
    """Bind the uploaded file to an account (existing or "Nova conta").

    Computes `csv_mapper.compute_schema_fingerprint(raw.columns)` and, if
    `csv_mapper.find_profile_by_fingerprint` matches a saved profile, offers a
    one-click "Reaproveitar mapeamento salvo" that skips the visual mapping
    step entirely.

    Args:
        raw: The ingested table whose column fingerprint is checked for reuse.

    Returns:
        The bound account_id, or None while a new account is being created.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_visual_mapping_section(raw: "file_ingest.RawTable") -> "mapping_ui.MappingResult | None":
    """Run the drag-and-drop mapping step and stash its `MappingResult`.

    Seeds the buckets with `mapping_ui.suggest_mapping(raw.columns)`, renders
    `mapping_ui.render_mapping_ui`, and persists the result to
    `session_state["mapping_result"]`.

    Args:
        raw: The ingested table being mapped.

    Returns:
        The current `MappingResult`, or None until the user completes mapping.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_mapping_validation_and_save(
    raw: "file_ingest.RawTable",
    mapping_result: "mapping_ui.MappingResult",
    account_id: str,
) -> "AccountProfile | None":
    """Dry-run + validate the mapping on sample rows, then save the profile.

    Assembles a candidate `AccountProfile` (mapping + locale + the import
    provenance fields: `source_type`, `sheet_name`, `header_row`,
    `pdf_strategy`, `schema_fingerprint`), dry-runs `csv_mapper.process_csv`
    over `raw.sample_rows` to display parsed date / amount / sign / currency,
    and refuses to save if parsing fails. On success persists via
    `csv_mapper.save_profile` + `db.upsert_account`.

    Args:
        raw: The ingested table (its sample rows drive the dry run).
        mapping_result: The validated mapping/locale declarations.
        account_id: The bound account identifier.

    Returns:
        The saved `AccountProfile`, or None if validation failed or the user
        has not confirmed the save.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def main() -> None:
    """Entry point: wires the full Select Account -> Upload -> Map -> Preview
    -> Categorize -> Insights pipeline in order, gated on session_state so
    each stage only renders once its prerequisite state is populated.
    """
    st.set_page_config(page_title="Gestão Financeira Pessoal", layout="wide")
    st.title("Gestão Financeira Pessoal — MVP")

    init_session_state()
    _get_db_connection()

    account_id = render_account_selector()

    if account_id is None:
        render_account_creation_form()

    raw_bytes = render_upload_section()
    if raw_bytes is None:
        return

    profile = render_mapping_section(raw_bytes)
    if profile is None:
        return

    sanitized_df = render_preview_section(raw_bytes, profile)
    if sanitized_df is None:
        return

    render_categorization_section()
    render_insights_section()


if __name__ == "__main__":
    main()
