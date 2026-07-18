"""Streamlit UI for the Personal Finance MVP.

New upload-first pipeline (replaces the old create-account-first + column
mapping screen):

    1. Upload (CSV / XLS / XLSX / PDF)
    2. Ingest  -> file_ingest.RawTable (detected columns + sample rows)
    3. Preview detected columns + sample values
    4. If a saved profile matches the schema fingerprint -> one-click reuse
    5. Otherwise: bind to an account, auto-suggest + visually map columns,
       declare locale / sign / currency / internal-transfer rules
    6. Validate + dry-run parse on the sample -> preview canonical DataFrame
    7. Save profile + import (INSERT OR IGNORE, unchanged) -> categorize -> insights

UI is strictly separated from transform/AI logic: this module orchestrates
calls into file_ingest.py, mapping_ui.py, csv_mapper.py, ai_services.py and
db.py, and owns `st.session_state`. All user-facing strings are pt_BR;
identifiers/comments/logs stay English.
"""

from __future__ import annotations

import hashlib
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
BRIDGE_DELIMITER = ";"  # delimiter used when serializing XLS/PDF RawTables to CSV
_EXTENSION_TO_SOURCE = {"csv": "csv", "xls": "xls", "xlsx": "xls", "pdf": "pdf"}
_ACCOUNT_TYPE_LABELS = {"bank_account": "Conta bancária", "credit_card": "Cartão de crédito"}
_PDF_STRATEGY_LABELS = {"stream": "Texto alinhado (stream)", "lattice": "Tabela com linhas (lattice)"}

# session_state keys cleared whenever a different file is uploaded.
_PIPELINE_STATE_KEYS = [
    "raw_table", "bank_profile", "active_account_id", "sanitized_df", "categorized_df",
    "mapping_result", "csv_dialect", "csv_guess", "selected_sheet", "source_type",
    "pdf_extraction", "pdf_strategy", "insights_text",
]
_PIPELINE_WIDGET_PREFIXES = ("map_", "sortable", "csv_", "xls_", "pdf_", "bind_", "reuse_")


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
    """Populate `st.session_state` with pipeline defaults if unset.

    Called once at the top of every rerun so widget interactions never reset
    pipeline state (uploaded file, ingest result, mapping, sanitized/
    categorized data, category cache, budgets).
    """
    defaults = {
        "active_account_id": None,
        "bank_profile": None,
        "uploaded_bytes": None,
        "uploaded_sig": None,
        "sanitized_df": None,
        "categorized_df": None,
        "category_cache": {},
        "budget_by_category": {},
        "insights_text": None,
        "raw_table": None,
        "mapping_result": None,
        "header_row": 0,
        "selected_sheet": None,
        "source_type": None,
        "csv_dialect": None,
        "csv_guess": None,
        "pdf_extraction": None,
        "pdf_strategy": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _reset_pipeline_state() -> None:
    """Clear all per-file pipeline state (called when a new file is uploaded)."""
    for key in _PIPELINE_STATE_KEYS:
        st.session_state[key] = 0 if key == "header_row" else None
    for key in list(st.session_state.keys()):
        if key.startswith(_PIPELINE_WIDGET_PREFIXES):
            del st.session_state[key]


def _detect_source_type(filename: str) -> str | None:
    """Map an uploaded filename's extension to a source type, or None."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXTENSION_TO_SOURCE.get(ext)


# ---------------------------------------------------------------------------
# Step 1-2: upload + ingest
# ---------------------------------------------------------------------------
def _ingest_csv_ui(raw_bytes: bytes) -> file_ingest.RawTable | None:
    """Render CSV dialect controls (pre-filled with sniffer guesses) + ingest."""
    st.markdown("**Dialeto do CSV** (ajuste se necessário)")
    guess = st.session_state.get("csv_guess")
    if guess is None:
        guess = file_ingest.sniff_csv(raw_bytes)
        st.session_state["csv_guess"] = guess

    col1, col2, col3, col4 = st.columns(4)
    delimiter = col1.text_input("Delimitador", value=guess.delimiter, key="csv_delimiter")
    encoding = col2.selectbox(
        "Codificação", ["utf-8", "latin-1"],
        index=0 if guess.encoding == "utf-8" else 1, key="csv_encoding",
    )
    decimal_separator = col3.selectbox(
        "Separador decimal", [",", "."],
        index=0 if guess.decimal_separator == "," else 1, key="csv_decimal",
    )
    thousands_options = [".", ",", ""]
    thousands_separator = col4.selectbox(
        "Separador de milhar", thousands_options,
        index=thousands_options.index(guess.thousands_separator)
        if guess.thousands_separator in thousands_options else 0,
        key="csv_thousands",
    )

    dialect = file_ingest.CsvDialect(
        delimiter=delimiter or ",",
        encoding=encoding,
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
    )
    st.session_state["csv_dialect"] = dialect
    st.session_state["header_row"] = 0
    try:
        return file_ingest.read_csv(raw_bytes, dialect)
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Não foi possível ler o CSV: {exc}")
        return None


def _ingest_excel_ui(raw_bytes: bytes) -> file_ingest.RawTable | None:
    """Render Excel sheet + header-row controls and ingest the chosen sheet."""
    try:
        sheets = file_ingest.list_excel_sheets(raw_bytes)
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Não foi possível ler o arquivo Excel: {exc}")
        return None

    sheet = st.selectbox("Planilha", sheets, key="xls_sheet")
    header_row = st.number_input(
        "Índice da linha de cabeçalho (0 = primeira linha)",
        min_value=0, value=0, step=1, key="xls_header_row",
    )
    st.session_state["selected_sheet"] = sheet
    st.session_state["header_row"] = int(header_row)
    try:
        return file_ingest.read_excel(raw_bytes, sheet, int(header_row))
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Não foi possível ler a planilha: {exc}")
        return None


def _ingest_pdf_ui(raw_bytes: bytes) -> file_ingest.RawTable | None:
    """Render PDF strategy + header-row controls, ingest, and gate on manual review."""
    strategy = st.selectbox(
        "Estratégia de extração", ["stream", "lattice"],
        format_func=lambda v: _PDF_STRATEGY_LABELS[v], key="pdf_strategy_choice",
    )
    header_row = st.number_input(
        "Índice da linha de cabeçalho (0 = primeira linha)",
        min_value=0, value=0, step=1, key="pdf_header_row",
    )
    st.session_state["pdf_strategy"] = strategy
    st.session_state["header_row"] = int(header_row)

    result = file_ingest.read_pdf(raw_bytes, strategy, int(header_row))
    st.session_state["pdf_extraction"] = result
    for warning in result.warnings:
        st.warning(warning)

    if result.raw_table is None:
        return None
    if result.needs_manual_review:
        confirmed = st.checkbox(
            "Confirmo que revisei a extração do PDF e os dados estão corretos.",
            key="pdf_confirm",
        )
        if not confirmed:
            st.info("Marque a confirmação para prosseguir com os dados extraídos do PDF.")
            return None
    return result.raw_table


def render_file_ingest_section() -> file_ingest.RawTable | None:
    """Upload + ingest step: accept CSV/XLS/XLSX/PDF and produce a `RawTable`."""
    st.header("1. Enviar arquivo")
    uploaded = st.file_uploader(
        "Selecione o arquivo (CSV, XLS, XLSX ou PDF)",
        type=["csv", "xls", "xlsx", "pdf"], key="file_uploader",
    )
    if uploaded is None:
        return None

    data = uploaded.getvalue()
    signature = hashlib.sha256(data).hexdigest()
    if signature != st.session_state.get("uploaded_sig"):
        _reset_pipeline_state()
        st.session_state["uploaded_sig"] = signature
        st.session_state["uploaded_bytes"] = data
        st.session_state["source_type"] = _detect_source_type(uploaded.name)

    source_type = st.session_state.get("source_type")
    if source_type == "csv":
        raw = _ingest_csv_ui(data)
    elif source_type == "xls":
        raw = _ingest_excel_ui(data)
    elif source_type == "pdf":
        raw = _ingest_pdf_ui(data)
    else:
        st.error("Tipo de arquivo não suportado.")
        return None

    st.session_state["raw_table"] = raw
    return raw


def render_column_preview(raw: file_ingest.RawTable) -> None:
    """Show the detected columns and sample values from an ingested `RawTable`."""
    st.subheader("Colunas detectadas")
    st.write(", ".join(raw.columns) if raw.columns else "(nenhuma coluna detectada)")
    st.caption(f"{raw.row_count} linha(s) detectada(s). Amostra:")
    st.dataframe(pd.DataFrame(raw.sample_rows), use_container_width=True)


# ---------------------------------------------------------------------------
# Step 4: one-click reuse of a matching saved profile
# ---------------------------------------------------------------------------
def render_reuse_offer(raw: file_ingest.RawTable) -> AccountProfile | None:
    """If a saved profile's schema_fingerprint matches, offer one-click reuse."""
    fingerprint = csv_mapper.compute_schema_fingerprint(raw.columns)
    match = csv_mapper.find_profile_by_fingerprint(fingerprint)
    if match is None:
        return None

    st.header("2. Perfil correspondente encontrado")
    st.info(f"Um mapeamento salvo corresponde a este arquivo (conta '{match.account_id}').")
    if st.button("Reaproveitar mapeamento salvo", key="reuse_confirm"):
        return match
    st.caption("Ou crie um novo mapeamento abaixo.")
    return None


# ---------------------------------------------------------------------------
# Step 5-7: bind account, map columns, validate, save
# ---------------------------------------------------------------------------
def render_account_binding(raw: file_ingest.RawTable) -> dict | None:
    """Bind the uploaded file to an existing account or a brand-new one."""
    st.header("3. Vincular a uma conta")
    existing = csv_mapper.list_profiles()
    choice = st.selectbox("Conta", [NEW_ACCOUNT_LABEL] + existing, key="bind_choice")

    if choice == NEW_ACCOUNT_LABEL:
        account_id = st.text_input("Identificador da conta (ex.: nubank_cc)", key="bind_account_id")
        account_type = st.selectbox(
            "Tipo de conta", [t.value for t in AccountType],
            format_func=lambda v: _ACCOUNT_TYPE_LABELS[v], key="bind_account_type",
        )
        bank_name = st.text_input("Nome do banco/instituição", key="bind_bank_name")
        if not account_id.strip() or not bank_name.strip():
            st.info("Preencha o identificador e o nome do banco para continuar.")
            return None
        return {
            "account_id": account_id.strip(),
            "account_type": account_type,
            "bank_name": bank_name.strip(),
        }

    profile = csv_mapper.load_profile(choice)
    if profile is None:
        st.error("Falha ao carregar a conta selecionada.")
        return None
    return {
        "account_id": choice,
        "account_type": profile.account_type.value,
        "bank_name": profile.bank_name,
    }


def _build_profile(
    binding: dict, mapping_result: "mapping_ui.MappingResult", raw: file_ingest.RawTable
) -> AccountProfile:
    """Assemble an `AccountProfile` from the binding + mapping + ingest provenance."""
    source_type = st.session_state.get("source_type")
    if source_type == "csv":
        dialect = st.session_state.get("csv_dialect")
        delimiter, encoding = dialect.delimiter, dialect.encoding
    else:
        delimiter, encoding = BRIDGE_DELIMITER, "utf-8"

    column_map = ColumnMap(
        date=mapping_result.mapping_dict.get("date"),
        amount=mapping_result.mapping_dict.get("amount"),
        debit=mapping_result.mapping_dict.get("debit"),
        credit=mapping_result.mapping_dict.get("credit"),
        description=mapping_result.mapping_dict.get("description"),
        currency=mapping_result.mapping_dict.get("currency"),
    )
    return AccountProfile(
        account_id=binding["account_id"],
        account_type=AccountType(binding["account_type"]),
        bank_name=binding["bank_name"],
        invert_sign=mapping_result.invert_sign,
        column_map=column_map,
        date_format=mapping_result.date_format,
        decimal_separator=mapping_result.decimal_separator,
        thousands_separator=mapping_result.thousands_separator,
        encoding=encoding,
        delimiter=delimiter,
        amount_sign_convention=AmountSignConvention(mapping_result.amount_sign_convention),
        default_currency=Currency(mapping_result.default_currency),
        skip_rows_regex=mapping_result.skip_rows_regex,
        internal_transfer_regex=mapping_result.internal_transfer_regex,
        source_type=source_type,
        sheet_name=raw.sheet_name,
        header_row=int(st.session_state.get("header_row", 0)),
        pdf_strategy=raw.pdf_strategy,
        schema_fingerprint=csv_mapper.compute_schema_fingerprint(raw.columns),
    )


def _sample_csv_bytes(raw: file_ingest.RawTable, profile: AccountProfile) -> bytes:
    """Serialize the RawTable sample to CSV bytes for the dry-run parse."""
    return file_ingest.raw_table_to_csv_bytes(raw, profile.delimiter, profile.encoding)


def render_validate_and_save(
    raw: file_ingest.RawTable, mapping_result: "mapping_ui.MappingResult", binding: dict
) -> AccountProfile | None:
    """Validate the mapping, dry-run parse the sample, then save the profile."""
    st.header("4. Validar e salvar")
    if not mapping_result.is_valid:
        for error in mapping_result.errors:
            st.error(error)
        return None

    profile = _build_profile(binding, mapping_result, raw)
    try:
        sample_df = csv_mapper.process_csv(_sample_csv_bytes(raw, profile), profile)
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Falha ao interpretar a amostra com este mapeamento: {exc}")
        return None

    if sample_df.empty:
        st.warning("A amostra não produziu nenhuma linha válida. Revise o mapeamento e a localização.")
        return None

    display = sample_df.copy()
    display["sinal"] = display["amount"].map(lambda a: "negativo" if a < 0 else "positivo")
    st.caption("Prévia da interpretação da amostra (data, valor, sinal, moeda):")
    st.dataframe(
        display[["date", "amount", "sinal", "currency", "description", "is_internal_transfer"]],
        use_container_width=True,
    )

    if st.button("Salvar perfil e continuar", key="save_profile_btn"):
        try:
            csv_mapper.save_profile(profile)
        except OSError as exc:
            st.error(f"Falha ao salvar o perfil: {exc}")
            return None
        conn = _get_db_connection()
        db.upsert_account(conn, profile)
        st.session_state["bank_profile"] = profile
        st.session_state["active_account_id"] = profile.account_id
        st.success(f"Perfil da conta '{profile.account_id}' salvo.")
        st.rerun()
    return None


# ---------------------------------------------------------------------------
# Step 7: canonical preview + import
# ---------------------------------------------------------------------------
def _full_csv_bytes(profile: AccountProfile) -> bytes:
    """Re-ingest the FULL file (all rows) via the profile's provenance, then bridge."""
    data = st.session_state.get("uploaded_bytes")
    source_type = profile.source_type
    if source_type == "csv":
        dialect = st.session_state.get("csv_dialect") or file_ingest.CsvDialect(
            delimiter=profile.delimiter, encoding=profile.encoding,
            decimal_separator=profile.decimal_separator, thousands_separator=profile.thousands_separator,
        )
        full = file_ingest.read_csv(data, dialect, max_rows=None)
    elif source_type == "xls":
        full = file_ingest.read_excel(data, profile.sheet_name, profile.header_row, max_rows=None)
    else:  # pdf
        result = file_ingest.read_pdf(data, profile.pdf_strategy or "stream", profile.header_row, max_rows=None)
        if result.raw_table is None:
            raise ValueError("Extração de PDF indisponível para importação completa.")
        full = result.raw_table
    return file_ingest.raw_table_to_csv_bytes(full, profile.delimiter, profile.encoding)


def render_canonical_preview_and_import(profile: AccountProfile) -> pd.DataFrame | None:
    """Run `process_csv` on the full file, preview the canonical rows, and import."""
    st.header("5. Prévia canônica e importação")
    try:
        sanitized_df = csv_mapper.process_csv(_full_csv_bytes(profile), profile)
    except ValueError as exc:
        st.error(f"Erro no mapeamento de colunas: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Erro ao processar o arquivo: {exc}")
        return None

    if sanitized_df.empty:
        st.warning("Nenhuma transação válida foi encontrada após a sanitização dos dados.")
        return None

    st.session_state["sanitized_df"] = sanitized_df
    st.dataframe(sanitized_df, use_container_width=True)
    st.caption(f"{len(sanitized_df)} transações válidas. Conta ativa: {profile.account_id}.")

    if st.button("Importar transações no banco de dados local", key="import_btn"):
        conn = _get_db_connection()
        inserted = db.insert_transactions(conn, sanitized_df)
        st.success(f"{inserted} novas transações importadas (duplicatas foram ignoradas).")

    return sanitized_df


# ---------------------------------------------------------------------------
# Downstream stages (UNCHANGED)
# ---------------------------------------------------------------------------
def render_categorization_section() -> pd.DataFrame | None:
    """Trigger `ai_services.categorize_transactions` for the active account's
    sanitized (non-internal-transfer) rows, showing a pt_BR loading spinner.

    Reuses and updates `session_state["category_cache"]` so repeated merchants
    across accounts are categorized once. Internal-transfer rows keep their
    forced "Transferência interna" category and are skipped.
    """
    sanitized_df = st.session_state.get("sanitized_df")
    if sanitized_df is None:
        return None

    st.header("6. Categorização automática com IA local")

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
    Internal transfers are excluded from both.
    """
    st.header("7. Insights de conciliação orçamentária")

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


def main() -> None:
    """Entry point: wires the upload-first pipeline, gated on session_state so
    each stage only renders once its prerequisite state is populated.
    """
    st.set_page_config(page_title="Gestão Financeira Pessoal", layout="wide")
    st.title("Gestão Financeira Pessoal — MVP")

    init_session_state()
    _get_db_connection()

    raw = render_file_ingest_section()
    if raw is None:
        return
    render_column_preview(raw)

    profile = st.session_state.get("bank_profile")
    if profile is None:
        reused = render_reuse_offer(raw)
        if reused is not None:
            st.session_state["bank_profile"] = reused
            st.session_state["active_account_id"] = reused.account_id
            st.rerun()

        binding = render_account_binding(raw)
        if binding is None:
            return
        suggested = mapping_ui.suggest_mapping(raw.columns)
        mapping_result = mapping_ui.render_mapping_ui(raw, suggested)
        profile = render_validate_and_save(raw, mapping_result, binding)
        if profile is None:
            return

    sanitized_df = render_canonical_preview_and_import(profile)
    if sanitized_df is None:
        return

    render_categorization_section()
    render_insights_section()


if __name__ == "__main__":
    main()
