"""Streamlit UI for the Personal Finance MVP.

Navigation:
    - 0 accounts            -> ONBOARDING (upload-first import incl. account creation).
    - >= 1 account          -> 3-page app via st.navigation:
        * Dashboard             (default landing; financial overview)
        * Atualizar transações  (incremental import into an existing account)
        * Configurações         (accounts, profiles, taxonomy, budgets)

Rules: UI strings pt_BR; code/identifiers English. BRL/EUR never mixed.
Internal transfers excluded from all metrics. Manual category edits are durable.
UI is separated from transform (csv_mapper/file_ingest), analytics (analytics),
persistence (db) and AI (ai_services) logic.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st

import ai_services
import analytics
import csv_mapper
import db
import file_ingest
import import_service
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
BRIDGE_DELIMITER = ";"
_EXTENSION_TO_SOURCE = {"csv": "csv", "xls": "xls", "xlsx": "xls", "pdf": "pdf"}
_ACCOUNT_TYPE_LABELS = {"bank_account": "Conta bancária", "credit_card": "Cartão de crédito"}
_PDF_STRATEGY_LABELS = {"stream": "Texto alinhado (stream)", "lattice": "Tabela com linhas (lattice)"}

_PIPELINE_STATE_KEYS = [
    "raw_table", "bank_profile", "active_account_id", "sanitized_df",
    "mapping_result", "csv_dialect", "csv_guess", "selected_sheet", "source_type",
    "pdf_extraction", "pdf_strategy", "last_import_hashes", "last_import_account",
]
_PIPELINE_WIDGET_PREFIXES = ("map_", "sortable", "csv_", "xls_", "pdf_", "bind_", "reuse_")


# ---------------------------------------------------------------------------
# Connection / session
# ---------------------------------------------------------------------------
@st.cache_resource
def _get_db_connection() -> sqlite3.Connection:
    """Open, initialize and MIGRATE the singleton SQLite connection."""
    conn = db.get_connection(DEFAULT_DB_PATH)
    db.init_schema(conn)
    db.seed_categories(conn, DEFAULT_CATEGORIES_PATH)
    db.apply_migrations(conn)
    return conn


def _load_categories_taxonomy() -> list[str]:
    """Load the LLM-selectable category list from categories.json."""
    data = json.loads(DEFAULT_CATEGORIES_PATH.read_text(encoding="utf-8"))
    return list(data["llm_categories"])


def init_session_state() -> None:
    """Populate `st.session_state` defaults once per rerun."""
    defaults = {
        "active_account_id": None,
        "bank_profile": None,
        "uploaded_bytes": None,
        "uploaded_sig": None,
        "sanitized_df": None,
        "category_cache": {},
        "raw_table": None,
        "mapping_result": None,
        "header_row": 0,
        "selected_sheet": None,
        "source_type": None,
        "csv_dialect": None,
        "csv_guess": None,
        "pdf_extraction": None,
        "pdf_strategy": None,
        "onboarding_active": False,
        "data_version": 0,
        "last_import_hashes": None,
        "last_import_account": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _bump_data_version() -> None:
    """Invalidate cached dashboard aggregations after any write."""
    st.session_state["data_version"] = st.session_state.get("data_version", 0) + 1


def _reset_pipeline_state() -> None:
    """Clear per-file import pipeline state."""
    for key in _PIPELINE_STATE_KEYS:
        st.session_state[key] = None
    st.session_state["header_row"] = 0
    for key in list(st.session_state.keys()):
        if key.startswith(_PIPELINE_WIDGET_PREFIXES):
            del st.session_state[key]


def _detect_source_type(filename: str) -> str | None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXTENSION_TO_SOURCE.get(ext)


# ---------------------------------------------------------------------------
# Ingestion UI (shared by onboarding)
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
        delimiter=delimiter or ",", encoding=encoding,
        decimal_separator=decimal_separator, thousands_separator=thousands_separator,
    )
    st.session_state["csv_dialect"] = dialect
    st.session_state["header_row"] = 0
    try:
        return file_ingest.read_csv(raw_bytes, dialect)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        st.error(f"Não foi possível ler o CSV: {exc}")
        return None


def _ingest_excel_ui(raw_bytes: bytes) -> file_ingest.RawTable | None:
    """Render Excel sheet + header-row controls and ingest the chosen sheet."""
    try:
        sheets = file_ingest.list_excel_sheets(raw_bytes)
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001
        st.error(f"Não foi possível ler a planilha: {exc}")
        return None


def _ingest_pdf_ui(raw_bytes: bytes) -> file_ingest.RawTable | None:
    """Render PDF strategy + header-row controls, ingest, gate on manual review."""
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
        if not st.checkbox(
            "Confirmo que revisei a extração do PDF e os dados estão corretos.", key="pdf_confirm"
        ):
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
        return {"account_id": account_id.strip(), "account_type": account_type, "bank_name": bank_name.strip()}
    profile = csv_mapper.load_profile(choice)
    if profile is None:
        st.error("Falha ao carregar a conta selecionada.")
        return None
    return {"account_id": choice, "account_type": profile.account_type.value, "bank_name": profile.bank_name}


def _build_profile(binding: dict, mr: "mapping_ui.MappingResult", raw: file_ingest.RawTable) -> AccountProfile:
    """Assemble an `AccountProfile` from the binding + mapping + ingest provenance."""
    source_type = st.session_state.get("source_type")
    if source_type == "csv":
        dialect = st.session_state.get("csv_dialect")
        delimiter, encoding = dialect.delimiter, dialect.encoding
    else:
        delimiter, encoding = BRIDGE_DELIMITER, "utf-8"
    column_map = ColumnMap(
        date=mr.mapping_dict.get("date"), amount=mr.mapping_dict.get("amount"),
        debit=mr.mapping_dict.get("debit"), credit=mr.mapping_dict.get("credit"),
        description=mr.mapping_dict.get("description"), currency=mr.mapping_dict.get("currency"),
    )
    return AccountProfile(
        account_id=binding["account_id"], account_type=AccountType(binding["account_type"]),
        bank_name=binding["bank_name"], invert_sign=mr.invert_sign, column_map=column_map,
        date_format=mr.date_format, decimal_separator=mr.decimal_separator,
        thousands_separator=mr.thousands_separator, encoding=encoding, delimiter=delimiter,
        amount_sign_convention=AmountSignConvention(mr.amount_sign_convention),
        default_currency=Currency(mr.default_currency), skip_rows_regex=mr.skip_rows_regex,
        internal_transfer_regex=mr.internal_transfer_regex, source_type=source_type,
        sheet_name=raw.sheet_name, header_row=int(st.session_state.get("header_row", 0)),
        pdf_strategy=raw.pdf_strategy, schema_fingerprint=csv_mapper.compute_schema_fingerprint(raw.columns),
    )


def _sample_csv_bytes(raw: file_ingest.RawTable, profile: AccountProfile) -> bytes:
    return file_ingest.raw_table_to_csv_bytes(raw, profile.delimiter, profile.encoding)


def render_validate_and_save(raw, mr, binding) -> AccountProfile | None:
    """Validate the mapping, dry-run parse the sample, then save the profile."""
    st.header("4. Validar e salvar")
    if not mr.is_valid:
        for error in mr.errors:
            st.error(error)
        return None
    profile = _build_profile(binding, mr, raw)
    try:
        sample_df = csv_mapper.process_csv(_sample_csv_bytes(raw, profile), profile)
    except Exception as exc:  # noqa: BLE001
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
# Ingest -> full canonical bytes bridge (profile-driven; reused by both flows)
# ---------------------------------------------------------------------------
def _full_csv_bytes_for(profile: AccountProfile, data: bytes, source_type: str | None = None) -> bytes:
    """Re-ingest a file's FULL content via the profile's provenance, then bridge.

    `source_type` overrides `profile.source_type` (which may be None for a
    legacy profile saved before provenance fields existed); it defaults to CSV.
    """
    st_type = source_type or profile.source_type or "csv"
    if st_type == "csv":
        dialect = file_ingest.CsvDialect(
            delimiter=profile.delimiter, encoding=profile.encoding,
            decimal_separator=profile.decimal_separator, thousands_separator=profile.thousands_separator,
        )
        full = file_ingest.read_csv(data, dialect, max_rows=None)
    elif st_type == "xls":
        full = file_ingest.read_excel(data, profile.sheet_name, profile.header_row, max_rows=None)
    else:  # pdf
        result = file_ingest.read_pdf(data, profile.pdf_strategy or "stream", profile.header_row, max_rows=None)
        if result.raw_table is None:
            raise ValueError("Extração de PDF indisponível para importação completa.")
        full = result.raw_table
    return file_ingest.raw_table_to_csv_bytes(full, profile.delimiter, profile.encoding)


def _sample_columns_for(profile: AccountProfile, data: bytes, source_type: str | None = None) -> list[str] | None:
    """Ingest just the columns of a file via the profile's provenance (for fingerprinting)."""
    st_type = source_type or profile.source_type or "csv"
    try:
        if st_type == "csv":
            dialect = file_ingest.CsvDialect(
                delimiter=profile.delimiter, encoding=profile.encoding,
                decimal_separator=profile.decimal_separator, thousands_separator=profile.thousands_separator,
            )
            return file_ingest.read_csv(data, dialect).columns
        if st_type == "xls":
            return file_ingest.read_excel(data, profile.sheet_name, profile.header_row).columns
        result = file_ingest.read_pdf(data, profile.pdf_strategy or "stream", profile.header_row)
        return result.raw_table.columns if result.raw_table else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read columns for fingerprinting: %s", exc)
        return None


def render_canonical_preview_and_import(profile: AccountProfile) -> pd.DataFrame | None:
    """Onboarding: run process_csv on the full file, preview, and import."""
    st.header("5. Prévia canônica e importação")
    try:
        sanitized_df = csv_mapper.process_csv(_full_csv_bytes_for(profile, st.session_state["uploaded_bytes"]), profile)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Erro ao processar o arquivo: {exc}")
        return None
    if sanitized_df.empty:
        st.warning("Nenhuma transação válida foi encontrada após a sanitização dos dados.")
        return None
    st.session_state["sanitized_df"] = sanitized_df
    st.dataframe(sanitized_df, use_container_width=True)
    st.caption(f"{len(sanitized_df)} transações válidas. Conta ativa: {profile.account_id}.")

    # ---- Opening balance (tracking start) ----
    st.subheader("Saldo inicial")
    earliest = sanitized_df["date"].min().date()
    default_open_date = earliest - timedelta(days=1)
    st.caption(
        "O saldo inicial é o saldo ao FINAL da data de início do controle "
        f"(sugestão: {default_open_date.isoformat()}, um dia antes do primeiro lançamento "
        f"deste arquivo — {earliest.isoformat()}). Lançamentos até essa data são "
        "guardados, mas ficam de fora do saldo corrente (já embutidos no saldo inicial)."
    )
    oc1, oc2 = st.columns(2)
    opening_balance = oc1.number_input(
        "Saldo inicial", value=0.0, step=100.0, format="%.2f", key="onb_opening_balance"
    )
    opening_balance_date = oc2.date_input(
        "Data de início do controle", value=default_open_date, key="onb_opening_date"
    )

    if st.button("Importar transações no banco de dados local", key="import_btn"):
        conn = _get_db_connection()
        db.set_opening_balance(
            conn, profile.account_id, float(opening_balance),
            opening_balance_date.isoformat(), currency=profile.default_currency.value,
        )
        inserted = db.insert_transactions(conn, sanitized_df)
        st.session_state["last_import_hashes"] = tuple(sanitized_df["transaction_hash"])
        st.session_state["last_import_account"] = profile.account_id
        _bump_data_version()
        st.success(f"{inserted} novas transações importadas (duplicatas foram ignoradas).")
    return sanitized_df


# ---------------------------------------------------------------------------
# Categorization (new rows only; respects manual + internal transfer)
# ---------------------------------------------------------------------------
def _categorize_hashes(conn: sqlite3.Connection, only_hashes: tuple[str, ...] | None) -> int:
    """Categorize eligible rows (optionally scoped to `only_hashes`). Returns count."""
    todo = db.fetch_categorizable(conn, only_hashes=only_hashes)
    if todo.empty:
        st.info("Nenhuma transação nova elegível para categorização.")
        return 0
    categories = _load_categories_taxonomy()
    with st.spinner("Categorizando transações com o modelo local..."):
        try:
            mapping = ai_services.categorize_transactions(
                todo["description"].tolist(), categories=categories,
                cache=st.session_state["category_cache"],
            )
        except Exception as exc:  # noqa: BLE001
            st.error(f"Falha ao categorizar transações: {exc}")
            return 0
    cat_by_hash = {h: mapping.get(d, "Outros") for h, d in zip(todo["transaction_hash"], todo["description"])}
    updated = db.apply_llm_categories(conn, cat_by_hash)
    _bump_data_version()
    return updated


def _render_post_import(conn: sqlite3.Connection, account_id: str) -> None:
    """Offer to categorize only the just-imported rows, then a finish action."""
    new_hashes = st.session_state.get("last_import_hashes")
    if not new_hashes or st.session_state.get("last_import_account") != account_id:
        return
    st.header("6. Categorizar novas transações")
    if st.button("Categorizar novas transações com IA", key="cat_new_onb"):
        n = _categorize_hashes(conn, new_hashes)
        if n:
            st.success(f"{n} transações categorizadas.")
    if st.button("Concluir e ir para o painel", key="finish_onboarding"):
        _reset_pipeline_state()
        st.session_state["onboarding_active"] = False
        st.session_state["uploaded_sig"] = None
        st.rerun()


# ---------------------------------------------------------------------------
# ONBOARDING (0 accounts, or "Nova conta" from Configurações)
# ---------------------------------------------------------------------------
def render_onboarding() -> None:
    """Full upload-first import flow, including account creation."""
    st.title("Gestão Financeira Pessoal — configuração de conta")
    conn = _get_db_connection()
    raw = render_file_ingest_section()
    if raw is None:
        st.caption("Envie um extrato para começar.")
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
    _render_post_import(conn, profile.account_id)


# ---------------------------------------------------------------------------
# PAGE: Dashboard
# ---------------------------------------------------------------------------
def _fmt_money(value: float, currency: str) -> str:
    return f"{currency} {value:,.2f}"


def _delta_str(current: float, previous: float) -> str | None:
    if previous == 0:
        return None
    return f"{(current - previous) / previous * 100:+.1f}%"


def _render_balance_section(
    conn: sqlite3.Connection, currency: str, accounts_key: tuple[str, ...],
    bounds, include_before: bool, version: int,
) -> None:
    """Render running-balance metrics per account + a balance-over-time chart.

    Balances INCLUDE internal transfers and NEVER mix currencies. Only accounts
    whose own currency matches the dashboard currency are shown. Credit-card
    accounts are labelled "Saldo devedor" (outstanding owed): a negative amount
    means money owed (spend is negative), a positive amount means credit.
    """
    # Restrict to accounts of THIS currency (never mix BRL/EUR).
    accounts = accounts_key or tuple(db.list_all_accounts(conn))
    currency_accounts = tuple(
        a for a in accounts
        if (db.get_account(conn, a) or {}).get("currency") == currency
    )
    if not currency_accounts:
        return

    st.subheader(f"Saldo por conta ({currency})")
    cols = st.columns(min(len(currency_accounts), 4))
    for i, account_id in enumerate(currency_accounts):
        account = db.get_account(conn, account_id)
        balance = db.running_balance(conn, account_id)
        is_card = account["account_type"] == "credit_card"
        label = f"{account_id} — Saldo devedor" if is_card else f"{account_id} — Saldo"
        cols[i % 4].metric(label, _fmt_money(balance or 0.0, currency))
        # Sanity warning: a bank account should not run negative.
        if not is_card and balance is not None and balance < 0:
            st.warning(
                f"⚠️ O saldo da conta '{account_id}' está negativo "
                f"({_fmt_money(balance, currency)}). Verifique o saldo inicial ou "
                "um extrato faltante."
            )
    if any(a["account_type"] == "credit_card" for a in
           (db.get_account(conn, x) for x in currency_accounts)):
        st.caption("Cartões de crédito: 'Saldo devedor' — negativo indica valor devido; "
                   "positivo indica crédito a favor.")

    # Balance-over-time line chart (5th chart; internal transfers included).
    series, current = analytics.load_balance_series(
        version, currency, bounds.start, bounds.end, currency_accounts, include_before,
    )
    if not series.empty:
        st.subheader("Saldo ao longo do tempo")
        chart = alt.Chart(series).mark_line(point=True, color="#00695c").encode(
            x=alt.X("date:T", title="Data"),
            y=alt.Y("balance:Q", title=f"Saldo ({currency})"),
            tooltip=[alt.Tooltip("date:T", title="Data"),
                     alt.Tooltip("balance:Q", title="Saldo", format=".2f")],
        )
        st.altair_chart(chart, use_container_width=True)


def _render_dashboard(conn: sqlite3.Connection) -> None:
    """Render the financial dashboard for a single selected currency."""
    currencies = db.list_currencies(conn)
    if not currencies:
        st.info("Nenhuma transação de gasto/receita registrada ainda.")
        return

    st.title("📊 Painel financeiro")

    # Currency SELECTOR (never mixes BRL/EUR).
    currency = currencies[0] if len(currencies) == 1 else st.radio(
        "Moeda", currencies, horizontal=True, key="dash_currency",
        help="BRL e EUR nunca são somados ou convertidos.",
    )

    # ---- Global filters ----
    with st.container(border=True):
        c1, c2 = st.columns([1, 2])
        preset = c1.selectbox("Período", analytics.PERIOD_PRESETS, key="dash_period")
        today = date.today()
        custom_start = custom_end = None
        if preset == "Personalizado":
            rng = c2.date_input(
                "Intervalo personalizado",
                value=(today.replace(day=1), today), key="dash_custom_range",
            )
            if isinstance(rng, tuple) and len(rng) == 2:
                custom_start, custom_end = rng
        bounds = analytics.resolve_period(preset, today, custom_start, custom_end)

        c3, c4 = st.columns(2)
        account_options = db.list_all_accounts(conn)
        selected_accounts = c3.multiselect(
            "Contas", account_options, default=account_options, key="dash_accounts"
        )
        category_options = db.list_all_categories(conn)
        selected_categories = c4.multiselect("Categorias", category_options, default=[], key="dash_categories")
        include_before = st.toggle(
            "Incluir lançamentos anteriores ao início do controle",
            value=False, key="dash_include_before",
            help="Por padrão, lançamentos anteriores à data de início do controle "
                 "ficam ocultos (já embutidos no saldo inicial).",
        )

    version = st.session_state["data_version"]
    accounts_key = tuple(sorted(selected_accounts))
    df = analytics.load_transactions(
        version, currency, bounds.start, bounds.end,
        accounts_key, tuple(sorted(selected_categories)), include_before,
    )
    prev_df = analytics.load_transactions(
        version, currency, bounds.prev_start, bounds.prev_end,
        accounts_key, tuple(sorted(selected_categories)), include_before,
    )

    # ---- Running balance per account (currency never mixed) ----
    _render_balance_section(conn, currency, accounts_key, bounds, include_before, version)

    if df.empty:
        st.info("Nenhuma transação no período/filtros selecionados.")
        return

    # ---- KPI row ----
    kpi, prev = analytics.compute_kpis(df), analytics.compute_kpis(prev_df)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total de despesas", _fmt_money(kpi["expenses"], currency),
              delta=_delta_str(kpi["expenses"], prev["expenses"]), delta_color="inverse")
    k2.metric("Total de receitas", _fmt_money(kpi["income"], currency),
              delta=_delta_str(kpi["income"], prev["income"]))
    k3.metric("Saldo líquido", _fmt_money(kpi["net"], currency),
              delta=_delta_str(kpi["net"], prev["net"]))
    k4.metric("Δ despesas vs. período anterior",
              _delta_str(kpi["expenses"], prev["expenses"]) or "—")

    # ---- Chart 1: monthly evolution (income/expense bars + net line) ----
    st.subheader("Evolução mensal")
    monthly = analytics.monthly_evolution(df)
    if not monthly.empty:
        melted = monthly.melt(id_vars="month", value_vars=["Receitas", "Despesas"],
                              var_name="Tipo", value_name="Valor")
        bars = alt.Chart(melted).mark_bar().encode(
            x=alt.X("month:N", title="Mês"),
            y=alt.Y("Valor:Q", title=f"Valor ({currency})"),
            color=alt.Color("Tipo:N", scale=alt.Scale(domain=["Receitas", "Despesas"],
                                                       range=["#2e7d32", "#c62828"])),
            xOffset="Tipo:N",
        )
        line = alt.Chart(monthly).mark_line(point=True, color="#1565c0").encode(
            x="month:N", y=alt.Y("Líquido:Q", title=f"Valor ({currency})"),
        )
        st.altair_chart((bars + line).resolve_scale(y="shared"), use_container_width=True)

    # ---- Chart 2: expense by category (Top 10 + Outros) ----
    st.subheader("Despesas por categoria")
    by_cat = analytics.expense_by_category(df, top_n=10)
    if not by_cat.empty:
        chart = alt.Chart(by_cat).mark_bar(color="#c62828").encode(
            x=alt.X("spend:Q", title=f"Gasto ({currency})"),
            y=alt.Y("category:N", sort="-x", title="Categoria"),
            tooltip=["category", "spend"],
        )
        st.altair_chart(chart, use_container_width=True)

    # ---- Chart 3: expense by account (only if >1 account) ----
    if df["account_id"].nunique() > 1:
        st.subheader("Despesas por conta")
        by_acct = analytics.expense_by_account(df)
        chart = alt.Chart(by_acct).mark_bar(color="#6a1b9a").encode(
            x=alt.X("spend:Q", title=f"Gasto ({currency})"),
            y=alt.Y("account_id:N", sort="-x", title="Conta"),
            tooltip=["account_id", "spend"],
        )
        st.altair_chart(chart, use_container_width=True)

    # ---- Chart 4: budget vs actual (only categories with a budget) ----
    budgets = db.get_budgets(conn, currency)
    bva = analytics.budget_vs_actual(df, budgets)
    if not bva.empty:
        st.subheader("Orçado vs. realizado")
        melted = bva.melt(id_vars="category", value_vars=["Orçado", "Realizado"],
                          var_name="Tipo", value_name="Valor")
        chart = alt.Chart(melted).mark_bar().encode(
            x=alt.X("Valor:Q", title=f"Valor ({currency})"),
            y=alt.Y("category:N", title="Categoria"),
            color=alt.Color("Tipo:N", scale=alt.Scale(domain=["Orçado", "Realizado"],
                                                       range=["#90a4ae", "#c62828"])),
            yOffset="Tipo:N",
        )
        st.altair_chart(chart, use_container_width=True)
        overruns = bva[bva["Estouro"] > 0]
        if not overruns.empty:
            st.warning("Estouro de orçamento: " + ", ".join(
                f"{r.category} (+{_fmt_money(r.Estouro, currency)})" for r in overruns.itertuples()
            ))

    # ---- Transaction table ----
    _render_transaction_table(conn, df, currency)


def _render_transaction_table(conn: sqlite3.Connection, df: pd.DataFrame, currency: str) -> None:
    """Filtered, searchable, inline-editable transaction table with CSV export.

    Uses the EFFECTIVE description (COALESCE(override, original)) for display,
    search and export. The original bank text is immutable and shown in a
    disabled 'Original (banco)' column; editing the 'Descrição' cell writes
    `description_override` only (setting it back to the original text clears the
    override — "restaurar original"). `notes` are editable and searchable, and a
    📝 flag marks rows that have notes. Notes are never sent to the LLM.
    """
    st.subheader("Transações")
    s1, s2 = st.columns([2, 1])
    search = s1.text_input("Buscar (descrição ou notas)", key="tbl_search").strip()
    focus_options = ["(todas)"] + sorted(df["category"].unique().tolist())
    focus = s2.selectbox("Focar em categoria", focus_options, key="tbl_focus")

    work = df.copy()
    work["notes"] = work["notes"].fillna("")
    if search:
        mask = (
            work["description_effective"].str.contains(search, case=False, na=False)
            | work["notes"].str.contains(search, case=False, na=False)
        )
        work = work[mask]
    if focus != "(todas)":
        work = work[work["category"] == focus]

    view = pd.DataFrame({
        "📝": work["notes"].map(lambda n: "📝" if n else ""),
        "date": work["date"].dt.strftime("%Y-%m-%d"),
        "Descrição": work["description_effective"],
        "Original (banco)": work["description"],
        "category": work["category"],
        "Notas": work["notes"],
        "amount": work["amount"],
        "account_id": work["account_id"],
        "category_source": work["category_source"],
    })
    view.index = work["transaction_hash"]

    all_categories = db.list_all_categories(conn)
    edited = st.data_editor(
        view,
        column_config={
            "📝": st.column_config.TextColumn("📝", disabled=True, width="small"),
            "date": st.column_config.TextColumn("Data", disabled=True),
            "Descrição": st.column_config.TextColumn(
                "Descrição", help="Edite para sobrescrever o texto do banco; "
                "volte ao texto original para restaurar."),
            "Original (banco)": st.column_config.TextColumn("Original (banco)", disabled=True),
            "category": st.column_config.SelectboxColumn("Categoria", options=all_categories, required=True),
            "Notas": st.column_config.TextColumn("Notas (local)", help="Nunca enviado à IA."),
            "amount": st.column_config.NumberColumn("Valor", disabled=True, format="%.2f"),
            "account_id": st.column_config.TextColumn("Conta", disabled=True),
            "category_source": st.column_config.TextColumn("Origem", disabled=True),
        },
        use_container_width=True, hide_index=True, key="tbl_editor",
    )

    if st.button("Salvar alterações", key="tbl_save"):
        changed = 0
        for tx_hash in edited.index:
            new_desc = str(edited.loc[tx_hash, "Descrição"] or "").strip()
            original = str(view.loc[tx_hash, "Original (banco)"] or "").strip()
            cur_effective = str(view.loc[tx_hash, "Descrição"] or "").strip()
            if new_desc != cur_effective:
                # Setting it back to the bank text clears the override (restaurar).
                db.set_description_override(conn, tx_hash, None if new_desc == original else new_desc)
                changed += 1
            new_notes = str(edited.loc[tx_hash, "Notas"] or "").strip()
            if new_notes != str(view.loc[tx_hash, "Notas"] or "").strip():
                db.set_notes(conn, tx_hash, new_notes or None)
                changed += 1
            new_cat = edited.loc[tx_hash, "category"]
            if new_cat != view.loc[tx_hash, "category"]:
                db.set_manual_category(conn, tx_hash, new_cat)  # durable: category_source='manual'
                changed += 1
        if changed:
            _bump_data_version()
            st.success(f"{changed} alteração(ões) salva(s).")
            st.rerun()
        else:
            st.info("Nenhuma alteração para salvar.")

    # Export: effective label + BOTH original and override + notes (raw text never lost).
    export = pd.DataFrame({
        "date": df["date"].dt.strftime("%Y-%m-%d"),
        "description_effective": df["description_effective"],
        "description_original": df["description"],
        "description_override": df["description_override"],
        "notes": df["notes"],
        "category": df["category"],
        "amount": df["amount"],
        "currency": df["currency"],
        "account_id": df["account_id"],
        "is_internal_transfer": df["is_internal_transfer"],
    })
    st.download_button(
        "Exportar visão filtrada (CSV)", data=export.to_csv(index=False).encode("utf-8"),
        file_name=f"transacoes_{currency}.csv", mime="text/csv", key="tbl_export",
    )


def _dashboard_page() -> None:
    conn = _get_db_connection()
    if db.count_transactions(conn) == 0:
        st.title("📊 Painel financeiro")
        st.info("Você ainda não importou transações. Vá para **Atualizar transações** para adicionar seus extratos.")
        return
    _render_dashboard(conn)


# ---------------------------------------------------------------------------
# PAGE: Atualizar transações (incremental import)
# ---------------------------------------------------------------------------
def _process_incremental_files(profile: AccountProfile, files: list) -> tuple[pd.DataFrame, list[str]]:
    """Process each uploaded file with the account's saved profile.

    Files whose schema_fingerprint does not match are skipped with a pt_BR
    warning unless the user forces application (checkbox). Returns the combined
    canonical frame and a list of warnings.
    """
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    for uploaded in files:
        data = uploaded.getvalue()
        detected = _detect_source_type(uploaded.name)
        # Legacy profiles may have no source_type; infer it from the file then.
        if profile.source_type is not None and detected != profile.source_type:
            warnings.append(
                f"'{uploaded.name}': tipo de arquivo difere do perfil salvo "
                f"({profile.source_type}); arquivo ignorado."
            )
            continue
        eff_source = profile.source_type or detected
        columns = _sample_columns_for(profile, data, eff_source)
        if profile.schema_fingerprint is None:
            # Legacy profile without a recorded fingerprint: apply it directly.
            matches = True
        else:
            matches = columns is not None and (
                csv_mapper.compute_schema_fingerprint(columns) == profile.schema_fingerprint
            )
        if not matches:
            st.warning(
                f"'{uploaded.name}': as colunas não correspondem ao perfil salvo desta conta."
            )
            force = st.checkbox(
                f"Aplicar o perfil salvo mesmo assim em '{uploaded.name}'", key=f"force_{uploaded.name}"
            )
            st.caption("Para remapear as colunas, use **Configurações → editar/recriar a conta**.")
            if not force:
                warnings.append(f"'{uploaded.name}': ignorado (colunas divergentes).")
                continue
        try:
            frame = csv_mapper.process_csv(_full_csv_bytes_for(profile, data, eff_source), profile)
            frames.append(frame)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"'{uploaded.name}': falha ao processar ({exc}).")
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=csv_mapper.CANONICAL_COLUMNS)
    return combined, warnings


def _render_incremental_update(conn: sqlite3.Connection) -> None:
    """Incremental import into an EXISTING account (no account creation here)."""
    st.title("⬆️ Atualizar transações")
    accounts = db.list_all_accounts(conn)
    if not accounts:
        st.info("Nenhuma conta cadastrada. Crie uma conta em **Configurações**.")
        return

    account_id = st.selectbox("Conta", accounts, key="upd_account")
    profile = csv_mapper.load_profile(account_id)
    if profile is None:
        st.error(f"Perfil de importação da conta '{account_id}' não encontrado. Recrie-o em Configurações.")
        return

    files = st.file_uploader(
        "Enviar um ou mais extratos", type=["csv", "xls", "xlsx", "pdf"],
        accept_multiple_files=True, key="upd_files",
    )
    if not files:
        return

    combined, warnings = _process_incremental_files(profile, files)
    if combined.empty:
        for w in warnings:
            st.warning(w)
        st.info("Nenhuma transação processável nos arquivos enviados.")
        return

    # ---- Pre-import summary (BEFORE writing anything) ----
    date_iso = combined["date"].dt.strftime("%Y-%m-%d")
    ex_hashes = db.existing_hashes(conn, account_id, date_iso.min(), date_iso.max())
    plan = import_service.build_import_plan(
        combined, ex_hashes, db.account_date_range(conn, account_id), account_id, warnings,
    )

    st.header("Resumo da importação (pré-visualização)")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Linhas processadas", plan.total_rows)
    m2.metric("Novas", plan.new_rows)
    m3.metric("Já no banco (ignoradas)", plan.duplicate_rows)
    m4.metric("Transferências internas", plan.internal_transfer_rows)
    st.caption(f"Intervalo detectado: {plan.min_date} a {plan.max_date}.")
    if plan.overlaps_existing:
        st.info(f"Este intervalo sobrepõe o histórico existente da conta "
                f"({plan.existing_min_date} a {plan.existing_max_date}).")
    for w in plan.warnings:
        st.warning(w)

    if plan.duplicate_rows:
        with st.expander(f"Ver {plan.duplicate_rows} linha(s) que seriam ignoradas como duplicatas"):
            dup_view = combined[combined["transaction_hash"].isin(ex_hashes)]
            st.dataframe(dup_view[["date", "description", "amount", "currency"]], use_container_width=True)

    # ---- Legitimate-collision decisions ----
    distinct_hashes: set[str] = set()
    if plan.collision_groups:
        st.subheader("Possíveis duplicatas legítimas")
        st.caption(
            "Estas linhas do arquivo compartilham o mesmo identificador "
            "(mesma conta, data, valor e descrição). Decida por grupo:"
        )
        for i, group in enumerate(plan.collision_groups):
            choice = st.radio(
                f"{group.count}× {group.date_iso} · {group.description} · {group.amount:.2f} {group.currency}",
                ["Tratar como a MESMA transação (importar 1)",
                 "Tratar como transações DISTINTAS (importar todas)"],
                key=f"collision_{i}",
            )
            if choice.startswith("Tratar como transações DISTINTAS"):
                distinct_hashes.add(group.transaction_hash)

    if st.button("Confirmar importação", key="upd_confirm"):
        to_insert = import_service.apply_collision_decisions(combined, distinct_hashes)
        inserted = db.insert_transactions(conn, to_insert)
        ignored = len(to_insert) - inserted
        st.session_state["last_import_hashes"] = tuple(to_insert["transaction_hash"])
        st.session_state["last_import_account"] = account_id
        _bump_data_version()
        st.success(f"Importação concluída: {inserted} inseridas, {ignored} ignoradas como duplicatas.")

    # ---- Categorize ONLY the newly inserted rows ----
    if st.session_state.get("last_import_account") == account_id and st.session_state.get("last_import_hashes"):
        if st.button("Categorizar novas transações com IA", key="upd_cat"):
            n = _categorize_hashes(conn, st.session_state["last_import_hashes"])
            if n:
                st.success(f"{n} novas transações categorizadas.")


def _update_page() -> None:
    _render_incremental_update(_get_db_connection())


# ---------------------------------------------------------------------------
# PAGE: Configurações
# ---------------------------------------------------------------------------
def _render_settings(conn: sqlite3.Connection) -> None:
    st.title("⚙️ Configurações")

    st.header("Contas")
    stats = db.account_stats(conn)
    if stats:
        st.dataframe(pd.DataFrame(stats).rename(columns={
            "account_id": "Conta", "account_type": "Tipo", "bank_name": "Banco",
            "currency": "Moeda", "opening_balance": "Saldo inicial",
            "opening_balance_date": "Início do controle",
            "tx_count": "Transações", "min_date": "1º lançamento", "max_date": "Último lançamento",
        }), use_container_width=True)

    if st.button("➕ Nova conta (importar novo banco)", key="cfg_new_account"):
        _reset_pipeline_state()
        st.session_state["uploaded_sig"] = None
        st.session_state["onboarding_active"] = True
        st.rerun()

    # ---- Edit opening balance / tracking date ----
    st.subheader("Saldo inicial e início do controle")
    edit_accounts = db.list_all_accounts(conn)
    if edit_accounts:
        edit_target = st.selectbox("Conta", edit_accounts, key="cfg_bal_account")
        account = db.get_account(conn, edit_target) or {}
        st.caption(
            "O saldo inicial é o saldo ao FINAL da data de início do controle "
            "(já inclui todos os lançamentos até essa data). Lançamentos até essa data "
            "ficam de fora do saldo corrente e das métricas por padrão."
        )
        e1, e2 = st.columns(2)
        new_open_balance = e1.number_input(
            "Saldo inicial", value=float(account.get("opening_balance") or 0.0),
            step=100.0, format="%.2f", key="cfg_bal_value",
        )
        current_date = account.get("opening_balance_date")
        default_d = datetime.strptime(current_date, "%Y-%m-%d").date() if current_date else date.today()
        new_open_date = e2.date_input(
            "Data de início do controle", value=default_d, key="cfg_bal_date",
        )
        st.caption(f"Moeda da conta: {account.get('currency') or '—'}.")
        st.warning(
            "Alterar a data recalcula quais lançamentos são anteriores ao início do "
            "controle e desloca todos os saldos desta conta."
        )
        if st.button("Salvar saldo inicial", key="cfg_bal_save"):
            db.set_opening_balance(conn, edit_target, float(new_open_balance), new_open_date.isoformat())
            _bump_data_version()
            st.success(f"Saldo inicial da conta '{edit_target}' atualizado.")
            st.rerun()

    # ---- Delete account ----
    st.subheader("Excluir conta")
    accounts = db.list_all_accounts(conn)
    if accounts:
        target = st.selectbox("Conta a excluir", accounts, key="cfg_del_account")
        st.warning(
            f"Excluir a conta '{target}' remove TAMBÉM todas as suas transações. Esta ação é irreversível."
        )
        typed = st.text_input(f"Digite '{target}' para confirmar", key="cfg_del_confirm")
        if st.button("Excluir conta permanentemente", key="cfg_del_btn"):
            if typed == target:
                removed = db.delete_account(conn, target)
                csv_mapper.delete_profile(target)
                _bump_data_version()
                st.success(f"Conta '{target}' excluída ({removed} transações removidas).")
                st.rerun()
            else:
                st.error("Confirmação não corresponde ao identificador da conta.")

    # ---- Import profiles ----
    st.header("Perfis de importação salvos")
    profiles = csv_mapper.list_profiles()
    if profiles:
        prof_target = st.selectbox("Perfil", profiles, key="cfg_profile")
        loaded = csv_mapper.load_profile(prof_target)
        if loaded is not None:
            st.json(loaded.to_dict())
        if st.button("Excluir perfil salvo", key="cfg_profile_del"):
            csv_mapper.delete_profile(prof_target)
            st.success(f"Perfil '{prof_target}' excluído.")
            st.rerun()
    else:
        st.caption("Nenhum perfil salvo.")

    # ---- Category taxonomy ----
    st.header("Categorias")
    st.write(", ".join(db.list_all_categories(conn)))
    new_cat = st.text_input("Adicionar categoria", key="cfg_new_cat")
    if st.button("Adicionar", key="cfg_add_cat") and new_cat.strip():
        db.add_category(conn, new_cat.strip())
        st.success(f"Categoria '{new_cat.strip()}' adicionada.")
        st.rerun()

    # ---- Budgets ----
    st.header("Orçamentos")
    currencies = db.list_currencies(conn) or [c.value for c in Currency]
    budget_currency = st.selectbox("Moeda do orçamento", currencies, key="cfg_budget_currency")
    current_budgets = db.get_budgets(conn, budget_currency)
    cat = st.selectbox("Categoria", db.list_all_categories(conn), key="cfg_budget_cat")
    amount = st.number_input(
        "Valor orçado", min_value=0.0, step=50.0,
        value=float(current_budgets.get(cat, 0.0)), key="cfg_budget_amount",
    )
    b1, b2 = st.columns(2)
    if b1.button("Salvar orçamento", key="cfg_budget_save"):
        db.set_budget(conn, cat, budget_currency, amount)
        _bump_data_version()
        st.success(f"Orçamento de '{cat}' ({budget_currency}) salvo.")
        st.rerun()
    if b2.button("Remover orçamento", key="cfg_budget_del"):
        db.delete_budget(conn, cat, budget_currency)
        _bump_data_version()
        st.success(f"Orçamento de '{cat}' ({budget_currency}) removido.")
        st.rerun()
    if current_budgets:
        st.dataframe(
            pd.DataFrame([{"Categoria": k, "Orçado": v} for k, v in sorted(current_budgets.items())]),
            use_container_width=True,
        )


def _settings_page() -> None:
    _render_settings(_get_db_connection())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Route: onboarding when no accounts (or 'Nova conta'); else 3-page nav."""
    st.set_page_config(page_title="Gestão Financeira Pessoal", layout="wide")
    init_session_state()
    conn = _get_db_connection()

    if db.count_accounts(conn) == 0 or st.session_state.get("onboarding_active"):
        st.session_state["onboarding_active"] = True
        render_onboarding()
        return

    navigation = st.navigation([
        st.Page(_dashboard_page, title="Dashboard", icon="📊", default=True),
        st.Page(_update_page, title="Atualizar transações", icon="⬆️"),
        st.Page(_settings_page, title="Configurações", icon="⚙️"),
    ])
    navigation.run()


if __name__ == "__main__":
    main()
