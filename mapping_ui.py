"""Visual (drag-and-drop) column-mapping UI layer.

This module owns ONLY the mapping UX: it turns a source-agnostic `RawTable`
into a validated `MappingResult` describing which file column feeds each
canonical field, plus the locale/sign/currency declarations. It performs NO
canonical transform (that stays in csv_mapper.process_csv) and NO ingestion
(that stays in file_ingest).

Drag-and-drop model (streamlit-sortables, `multi_containers=True`): the
component MOVES chips between buckets, it does not draw arbitrary A<->B links.
So the UI is modelled as BUCKETS — one source bucket of unmapped file-column
chips, and one target bucket per canonical field. A file-column chip dragged
into a target bucket binds that column to that field; a target bucket holding
more than one chip is a validation error.

If `streamlit-sortables` is unavailable (import fails for any reason), a
per-field `st.selectbox` fallback ships, producing the exact same
`mapping_dict`. The app must never crash because the component is missing.

All user-facing strings are pt_BR; identifiers/comments/logs stay English.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field

import streamlit as st

from file_ingest import RawTable
from models import AmountSignConvention, Currency

logger = logging.getLogger(__name__)

# Canonical target fields ("app columns") a source column can be bound to.
CANONICAL_TARGET_FIELDS = ["date", "amount", "debit", "credit", "description", "reference", "currency"]

# pt_BR labels for the target buckets (UI only; keys stay English).
TARGET_FIELD_LABELS_PT_BR: dict[str, str] = {
    "date": "Data",
    "amount": "Valor",
    "debit": "Débito",
    "credit": "Crédito",
    "description": "Descrição",
    "reference": "Referência (Complemento)",
    "currency": "Moeda",
}
_LABEL_TO_FIELD = {label: field_name for field_name, label in TARGET_FIELD_LABELS_PT_BR.items()}

# Header of the source bucket that holds not-yet-mapped columns.
SOURCE_BUCKET_LABEL = "Colunas do arquivo (não mapeadas)"

# Sentinel shown in the selectbox fallback for "no source column".
NO_COLUMN_LABEL = "(nenhuma)"

_SIGN_CONVENTION_LABELS = {
    "signed": "Coluna única com sinal",
    "debit_credit_columns": "Colunas separadas de débito/crédito",
    "parentheses": "Negativos entre parênteses",
}

# Deterministic header synonyms for auto-suggestion (compared accent- and
# case-insensitively). No LLM, no fuzzy service.
HEURISTIC_SYNONYMS: dict[str, list[str]] = {
    "date": ["data", "date", "lancamento", "data lancamento", "dt", "data mov"],
    "amount": ["valor", "amount", "montante", "valor (r$)", "value", "vlr"],
    "debit": ["debito", "saida", "debit", "despesa", "pagamento"],
    "credit": ["credito", "entrada", "credit", "receita", "recebimento"],
    "description": ["descricao", "historico", "description", "detalhe", "memo", "lancamento historico", "Partner Name"],
    "reference": ["referencia", "complemento", "reference", "ref", "detalhe2", "payment reference"],
    "currency": ["moeda", "currency", "ccy", "divisa"],
}


@dataclass
class MappingResult:
    """The output of the mapping UI: a validated file->canonical binding + locale.

    `mapping_dict` maps each canonical field to the chosen source column name,
    or None when unbound (valid for optional fields). The remaining fields carry
    the locale/sign/currency declarations needed — together with account
    identity and source provenance supplied by app.py — to assemble an
    `models.AccountProfile` and run `csv_mapper.process_csv`.

    `encoding` and `delimiter` are placeholders here; app.py overrides them from
    the ingest provenance (real CSV dialect, or the bridge's utf-8/";" for
    XLS/PDF) when building the profile.
    """

    mapping_dict: dict[str, str | None]
    amount_sign_convention: str
    invert_sign: bool
    date_format: str
    decimal_separator: str
    thousands_separator: str
    encoding: str
    delimiter: str
    default_currency: str
    internal_transfer_regex: str
    skip_rows_regex: str
    is_valid: bool
    errors: list[str] = field(default_factory=list)


def _normalize_header(name: str) -> str:
    """Lowercase, strip accents and collapse whitespace for synonym matching."""
    decomposed = unicodedata.normalize("NFKD", name)
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(no_accents.lower().split())


def suggest_mapping(columns: list[str]) -> dict[str, str]:
    """Heuristically suggest a canonical-field -> source-column mapping.

    Deterministic (no LLM, no network): normalizes each detected column name
    and matches it against `HEURISTIC_SYNONYMS`, preferring exact synonym hits
    then substring hits. Each source column is suggested for at most one field.

    Args:
        columns: Detected file column names (from `RawTable.columns`).

    Returns:
        A dict of canonical field -> suggested source column, containing only
        the fields a confident guess was found for.
    """
    normalized = {col: _normalize_header(col) for col in columns}
    result: dict[str, str] = {}
    used: set[str] = set()

    # Pass 1: exact normalized synonym match.
    for field_name in CANONICAL_TARGET_FIELDS:
        synonyms = HEURISTIC_SYNONYMS[field_name]
        for col in columns:
            if col in used:
                continue
            if normalized[col] in synonyms:
                result[field_name] = col
                used.add(col)
                break

    # Pass 2: substring match for still-unmapped fields.
    for field_name in CANONICAL_TARGET_FIELDS:
        if field_name in result:
            continue
        synonyms = HEURISTIC_SYNONYMS[field_name]
        for col in columns:
            if col in used:
                continue
            norm = normalized[col]
            if any(syn in norm or norm in syn for syn in synonyms):
                result[field_name] = col
                used.add(col)
                break

    return result


def _visible_fields(amount_sign_convention: str) -> list[str]:
    """Return the canonical fields to show for a given sign convention."""
    if amount_sign_convention == AmountSignConvention.DEBIT_CREDIT_COLUMNS.value:
        return ["date", "description", "debit", "credit", "reference", "currency"]
    return ["date", "description", "amount", "reference", "currency"]


def _required_fields(amount_sign_convention: str) -> list[str]:
    """Return the canonical fields that MUST be bound for a given sign convention.

    - "signed" / "parentheses": require `amount` (debit/credit ignored).
    - "debit_credit_columns": require `debit` AND `credit` (amount ignored).
    `date` and `description` are always required; `currency` is always optional.
    """
    if amount_sign_convention == AmountSignConvention.DEBIT_CREDIT_COLUMNS.value:
        return ["date", "description", "debit", "credit"]
    return ["date", "description", "amount"]


def _validate_mapping(mapping_dict: dict[str, str | None], amount_sign_convention: str) -> list[str]:
    """Validate a proposed mapping, returning pt_BR error messages (empty == ok)."""
    errors: list[str] = []

    for field_name in _required_fields(amount_sign_convention):
        if not mapping_dict.get(field_name):
            label = TARGET_FIELD_LABELS_PT_BR[field_name]
            errors.append(f"O campo obrigatório '{label}' não foi mapeado.")

    # No source column may feed more than one canonical field.
    bound: dict[str, list[str]] = {}
    for field_name in _visible_fields(amount_sign_convention):
        col = mapping_dict.get(field_name)
        if col:
            bound.setdefault(col, []).append(TARGET_FIELD_LABELS_PT_BR[field_name])
    for col, labels in bound.items():
        if len(labels) > 1:
            errors.append(f"A coluna '{col}' está vinculada a mais de um campo: {', '.join(labels)}.")

    return errors


def _initial_layout(columns: list[str], suggested: dict[str, str], visible: list[str]) -> list[dict]:
    """Build the initial streamlit-sortables bucket layout from the suggestions."""
    assigned = {suggested[f] for f in visible if f in suggested and suggested[f] in columns}
    buckets = [{"header": SOURCE_BUCKET_LABEL, "items": [c for c in columns if c not in assigned]}]
    for field_name in visible:
        label = TARGET_FIELD_LABELS_PT_BR[field_name]
        col = suggested.get(field_name)
        items = [col] if (col in columns and col in assigned) else []
        buckets.append({"header": label, "items": items})
    return buckets


def _layout_to_mapping(layout: list[dict]) -> dict[str, str | None]:
    """Read a streamlit-sortables layout back into a canonical mapping dict.

    A target bucket with exactly one chip binds that column; empty binds None;
    more than one chip binds the FIRST (the surplus is reported by validation).
    """
    mapping: dict[str, str | None] = {f: None for f in CANONICAL_TARGET_FIELDS}
    for bucket in layout:
        field_name = _LABEL_TO_FIELD.get(bucket.get("header", ""))
        if field_name is None:
            continue
        items = bucket.get("items", [])
        mapping[field_name] = items[0] if items else None
    return mapping


def _render_sortable_buckets(
    columns: list[str], suggested: dict[str, str], amount_sign_convention: str
) -> dict[str, str | None]:
    """Render the drag-and-drop bucket UI (streamlit-sortables) and read bindings.

    Raises:
        ImportError: if streamlit-sortables is not installed (caller falls back).
    """
    from streamlit_sortables import sort_items  # may raise ImportError

    visible = _visible_fields(amount_sign_convention)
    state_key = f"sortable_layout_{amount_sign_convention}"
    layout = st.session_state.get(state_key) or _initial_layout(columns, suggested, visible)

    st.caption("Arraste cada coluna do arquivo para o campo correspondente.")
    new_layout = sort_items(layout, multi_containers=True, key=f"sortables_{amount_sign_convention}")
    st.session_state[state_key] = new_layout
    return _layout_to_mapping(new_layout)


def _render_selectbox_fallback(
    columns: list[str], suggested: dict[str, str], amount_sign_convention: str
) -> dict[str, str | None]:
    """Render the no-dependency fallback: one `st.selectbox` per visible field."""
    options = [NO_COLUMN_LABEL] + columns
    mapping: dict[str, str | None] = {f: None for f in CANONICAL_TARGET_FIELDS}
    for field_name in _visible_fields(amount_sign_convention):
        label = TARGET_FIELD_LABELS_PT_BR[field_name]
        default = suggested.get(field_name)
        index = options.index(default) if default in options else 0
        optional = field_name not in _required_fields(amount_sign_convention)
        choice = st.selectbox(
            f"{label}{' (opcional)' if optional else ''}",
            options,
            index=index,
            key=f"map_sel_{field_name}",
        )
        mapping[field_name] = None if choice == NO_COLUMN_LABEL else choice
    return mapping


def render_mapping_ui(raw: RawTable, suggested: dict[str, str]) -> MappingResult:
    """Render the full visual mapping step and return a validated `MappingResult`.

    Args:
        raw: The ingested source-agnostic table to map.
        suggested: Auto-suggested bindings from `suggest_mapping`.

    Returns:
        A `MappingResult` reflecting the current UI state (its `is_valid` gates
        the downstream dry-run + save-profile steps in app.py).
    """
    st.subheader("Mapeamento de colunas")

    convention = st.selectbox(
        "Convenção de sinal do valor",
        [c.value for c in AmountSignConvention],
        format_func=lambda v: _SIGN_CONVENTION_LABELS[v],
        key="map_convention",
    )

    try:
        mapping_dict = _render_sortable_buckets(raw.columns, suggested, convention)
    except Exception as exc:  # noqa: BLE001 - ANY component failure -> fallback
        logger.warning("streamlit-sortables unavailable; using selectbox fallback: %s", exc)
        st.info("Componente de arrastar-e-soltar indisponível; usando seleção por lista.")
        mapping_dict = _render_selectbox_fallback(raw.columns, suggested, convention)

    st.markdown("**Localização e regras**")
    col_a, col_b = st.columns(2)
    with col_a:
        date_format = st.text_input("Formato de data", value="%Y-%m-%d", key="map_date_format")
        decimal_separator = st.selectbox("Separador decimal", [",", "."], key="map_decimal")
        thousands_separator = st.selectbox("Separador de milhar", [".", ",", ""], key="map_thousands")
        invert_sign = st.checkbox(
            "Inverter sinal (ex.: cartão lista compras como positivo)", key="map_invert"
        )
    with col_b:
        default_currency = st.selectbox("Moeda padrão", [c.value for c in Currency], key="map_currency")
        skip_rows_regex = st.text_input(
            "Regex para ignorar linhas (rodapé/cabeçalho)",
            value="^(Saldo|Total|Balance)",
            key="map_skip",
        )
        internal_transfer_regex = st.text_input(
            "Regex de transferência interna",
            value="(?i)PAG.*FATURA|PGTO.*FATURA|PGTO CARTAO|BILL PAYMENT",
            key="map_internal",
        )

    errors = _validate_mapping(mapping_dict, convention)
    return MappingResult(
        mapping_dict=mapping_dict,
        amount_sign_convention=convention,
        invert_sign=invert_sign,
        date_format=date_format,
        decimal_separator=decimal_separator,
        thousands_separator=thousands_separator,
        encoding="utf-8",
        delimiter=";",
        default_currency=default_currency,
        internal_transfer_regex=internal_transfer_regex,
        skip_rows_regex=skip_rows_regex,
        is_valid=len(errors) == 0,
        errors=errors,
    )
