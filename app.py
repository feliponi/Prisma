"""Streamlit UI for the Personal Finance Management MVP.
Orchestrates the pipeline: CSV upload -> column mapping -> sanitized preview
-> local AI categorization -> AI-generated budget conciliation insights.
All user-facing text is in Brazilian Portuguese (pt_BR); code stays in English.
"""

from __future__ import annotations

import logging

import pandas as pd
import streamlit as st

from ai_services import (
    DEFAULT_CATEGORIES,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    categorize_transactions,
    generate_financial_insights,
)
from csv_mapper import (
    BankMappingConfig,
    CANONICAL_COLUMNS,
    list_saved_mappings,
    load_mapping,
    process_csv,
    save_mapping,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NEW_PROFILE_LABEL = "➕ Novo perfil de banco"

st.set_page_config(page_title="Gestao Financeira Pessoal", layout="wide")


def _init_session_state() -> None:
    defaults = {
        "raw_df": None,
        "uploaded_file_bytes": None,
        "processed_df": None,
        "categorized_df": None,
        "insights_text": None,
        "selected_bank": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _render_upload_section() -> None:
    st.header("1. Importar extrato bancario (CSV)")
    uploaded_file = st.file_uploader("Selecione o arquivo CSV do banco", type=["csv"])

    if uploaded_file is not None:
        st.session_state["uploaded_file_bytes"] = uploaded_file.getvalue()
        st.session_state["uploaded_file_name"] = uploaded_file.name


def _render_mapping_section() -> BankMappingConfig | None:
    if st.session_state.get("uploaded_file_bytes") is None:
        return None

    st.header("2. Mapear colunas")

    saved_banks = list_saved_mappings()
    options = [NEW_PROFILE_LABEL] + saved_banks
    choice = st.selectbox("Perfil de banco", options, key="bank_profile_choice")

    import io

    preview_buffer = io.BytesIO(st.session_state["uploaded_file_bytes"])
    try:
        header_preview = pd.read_csv(preview_buffer, nrows=5, dtype=str, engine="python")
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Nao foi possivel ler o CSV enviado: {exc}")
        return None

    available_columns = list(header_preview.columns)
    st.caption("Previa das primeiras linhas do arquivo:")
    st.dataframe(header_preview, use_container_width=True)

    if choice != NEW_PROFILE_LABEL:
        existing_config = load_mapping(choice)
        if existing_config is not None:
            st.success(f"Perfil '{choice}' carregado automaticamente.")
            return existing_config
        st.warning("Nao foi possivel carregar o perfil selecionado. Configure manualmente.")

    with st.form("mapping_form"):
        bank_name = st.text_input("Nome do banco", value="" if choice == NEW_PROFILE_LABEL else choice)
        date_column = st.selectbox("Coluna de data", available_columns)
        amount_column = st.selectbox("Coluna de valor", available_columns)
        description_column = st.selectbox("Coluna de descricao", available_columns)
        decimal_separator = st.selectbox(
            "Separador decimal", ["auto", ",", "."], help="'auto' detecta automaticamente"
        )
        csv_delimiter = st.text_input("Delimitador do CSV", value=",")
        skip_rows = st.number_input("Linhas de cabecalho a ignorar", min_value=0, value=0, step=1)
        save_profile = st.checkbox("Salvar este mapeamento para uso futuro", value=True)
        submitted = st.form_submit_button("Aplicar mapeamento")

    if not submitted:
        return None

    if not bank_name.strip():
        st.error("Informe um nome de banco valido.")
        return None

    config = BankMappingConfig(
        bank_name=bank_name.strip(),
        date_column=date_column,
        amount_column=amount_column,
        description_column=description_column,
        decimal_separator=decimal_separator,
        csv_delimiter=csv_delimiter or ",",
        skip_rows=int(skip_rows),
    )

    if save_profile:
        try:
            save_mapping(config)
            st.success(f"Perfil de mapeamento salvo para '{config.bank_name}'.")
        except OSError as exc:
            st.error(f"Falha ao salvar o perfil de mapeamento: {exc}")

    return config


def _render_processing_section(config: BankMappingConfig) -> None:
    import io

    st.header("3. Previa dos dados sanitizados")
    buffer = io.BytesIO(st.session_state["uploaded_file_bytes"])

    try:
        processed_df = process_csv(buffer, config)
    except ValueError as exc:
        st.error(f"Erro no mapeamento de colunas: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
        st.error(f"Erro ao processar o CSV: {exc}")
        return

    if processed_df.empty:
        st.warning("Nenhuma transacao valida foi encontrada apos a sanitizacao dos dados.")
        return

    st.session_state["processed_df"] = processed_df
    st.dataframe(processed_df, use_container_width=True)
    st.caption(f"{len(processed_df)} transacoes validas encontradas.")


def _render_categorization_section() -> None:
    processed_df = st.session_state.get("processed_df")
    if processed_df is None:
        return

    st.header("4. Categorizacao automatica com IA local")

    col1, col2 = st.columns(2)
    with col1:
        model = st.text_input("Modelo Ollama", value=DEFAULT_MODEL)
    with col2:
        base_url = st.text_input("Endpoint Ollama", value=DEFAULT_OLLAMA_URL)

    if st.button("Categorizar transacoes com IA"):
        with st.spinner("Categorizando transacoes com o modelo local..."):
            try:
                categorized_df = categorize_transactions(
                    processed_df,
                    categories=DEFAULT_CATEGORIES,
                    model=model,
                    base_url=base_url,
                )
                st.session_state["categorized_df"] = categorized_df
            except Exception as exc:  # noqa: BLE001 - surfaced directly to the user
                st.error(f"Falha ao categorizar transacoes: {exc}")

    categorized_df = st.session_state.get("categorized_df")
    if categorized_df is not None:
        st.dataframe(categorized_df, use_container_width=True)


def _render_insights_section() -> None:
    categorized_df = st.session_state.get("categorized_df")
    if categorized_df is None:
        return

    st.header("5. Insights de conciliacao orcamentaria")
    st.caption("Informe o orcamento planejado por categoria para gerar a analise.")

    spending_by_category = categorized_df.groupby("category")["amount"].sum().abs()
    budget_inputs: dict[str, float] = {}

    with st.form("budget_form"):
        for category in sorted(spending_by_category.index):
            budget_inputs[category] = st.number_input(
                f"Orcamento planejado - {category}",
                min_value=0.0,
                value=float(spending_by_category[category]),
                step=50.0,
            )
        generate = st.form_submit_button("Gerar insights com IA")

    if generate:
        with st.spinner("Gerando analise executiva com o modelo local..."):
            insights_text = generate_financial_insights(
                category_spending=spending_by_category.to_dict(),
                category_budget=budget_inputs,
            )
            st.session_state["insights_text"] = insights_text

    if st.session_state.get("insights_text"):
        st.subheader("Resumo executivo")
        st.write(st.session_state["insights_text"])


def main() -> None:
    st.title("Gestao Financeira Pessoal - MVP")
    _init_session_state()

    _render_upload_section()
    config = _render_mapping_section()

    if config is not None:
        st.session_state["selected_bank"] = config
    elif st.session_state.get("selected_bank") is not None and st.session_state.get(
        "processed_df"
    ) is None:
        config = st.session_state["selected_bank"]

    if st.session_state.get("selected_bank") is not None:
        _render_processing_section(st.session_state["selected_bank"])

    _render_categorization_section()
    _render_insights_section()


if __name__ == "__main__":
    main()
