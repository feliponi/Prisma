"""Visual (drag-and-drop) column-mapping UI layer.

Contracts only (Phase 1). Every function is fully type-hinted and documented
but raises NotImplementedError; bodies land in Phase 2.

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
more than one chip is a validation error (each canonical field binds to at
most one source column).

If `streamlit-sortables` is unavailable, a per-field `st.selectbox` fallback
must ship (imported lazily inside the render function), producing the exact
same `mapping_dict`.

All user-facing strings are pt_BR; identifiers/comments/logs stay English.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from file_ingest import RawTable

logger = logging.getLogger(__name__)

# Canonical target fields ("app columns") a source column can be bound to.
CANONICAL_TARGET_FIELDS = ["date", "amount", "debit", "credit", "description", "currency"]

# pt_BR labels for the target buckets (UI only; keys stay English).
TARGET_FIELD_LABELS_PT_BR: dict[str, str] = {
    "date": "Data",
    "amount": "Valor",
    "debit": "Débito",
    "credit": "Crédito",
    "description": "Descrição",
    "currency": "Moeda",
}

# Sentinel shown in the selectbox fallback for "no source column".
NO_COLUMN_LABEL = "(nenhuma)"

# Deterministic header synonyms for auto-suggestion (lowercased, accent-
# insensitive comparison expected in Phase 2). No LLM, no fuzzy service.
HEURISTIC_SYNONYMS: dict[str, list[str]] = {
    "date": ["data", "date", "lancamento", "lançamento", "data lancamento", "dt"],
    "amount": ["valor", "amount", "montante", "valor (r$)", "value"],
    "debit": ["debito", "débito", "saida", "saída", "debit", "despesa"],
    "credit": ["credito", "crédito", "entrada", "credit", "receita"],
    "description": ["descricao", "descrição", "historico", "histórico", "description", "detalhe", "memo"],
    "currency": ["moeda", "currency", "ccy", "divisa"],
}


@dataclass
class MappingResult:
    """The output of the mapping UI: a validated file->canonical binding + locale.

    `mapping_dict` maps each canonical field to the chosen source column name,
    or None when unbound (which is valid for optional fields). The remaining
    fields carry the locale/sign/currency declarations needed — together with
    account identity and source provenance supplied by app.py — to assemble an
    `models.AccountProfile` and run `csv_mapper.process_csv`.

    `is_valid` reflects whether all convention-required fields are bound and no
    field is double-bound; `errors` holds pt_BR messages for display when not.
    """

    mapping_dict: dict[str, str | None]
    amount_sign_convention: str
    date_format: str
    decimal_separator: str
    thousands_separator: str
    encoding: str
    delimiter: str
    default_currency: str
    internal_transfer_regex: str
    is_valid: bool
    errors: list[str] = field(default_factory=list)


def suggest_mapping(columns: list[str]) -> dict[str, str]:
    """Heuristically suggest a canonical-field -> source-column mapping.

    Deterministic (no LLM, no network): normalizes each detected column name
    (lowercase, strip accents/whitespace) and matches it against
    `HEURISTIC_SYNONYMS`, preferring exact synonym hits then substring hits.
    Each source column is suggested for at most one canonical field.

    Args:
        columns: Detected file column names (from `RawTable.columns`).

    Returns:
        A dict mapping canonical field -> suggested source column, containing
        only the fields a confident guess was found for (others are omitted
        and left for the user to bind manually).

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _required_fields(amount_sign_convention: str) -> list[str]:
    """Return the canonical fields that MUST be bound for a given sign convention.

    - "signed" / "parentheses": require `amount` (debit/credit ignored).
    - "debit_credit_columns": require `debit` AND `credit` (amount ignored).
    `date` and `description` are always required; `currency` is always optional.

    Args:
        amount_sign_convention: One of "signed" | "parentheses" |
            "debit_credit_columns".

    Returns:
        The list of required canonical field names.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _validate_mapping(mapping_dict: dict[str, str | None], amount_sign_convention: str) -> list[str]:
    """Validate a proposed mapping, returning pt_BR error messages (empty == ok).

    Checks (Phase 2):
        - every field required by `amount_sign_convention` is bound;
        - no source column is bound to more than one canonical field;
        - (bucket UI) no target bucket holds more than one chip.

    Args:
        mapping_dict: Canonical field -> source column (or None).
        amount_sign_convention: The chosen sign convention.

    Returns:
        A list of pt_BR error strings; empty when the mapping is valid.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _render_sortable_buckets(
    columns: list[str], suggested: dict[str, str]
) -> dict[str, str | None]:
    """Render the drag-and-drop bucket UI (streamlit-sortables) and read bindings.

    Seeds a source bucket with every file column as a chip (pre-distributing
    chips into target buckets per `suggested`), then reads back which chip
    landed in which target bucket to build `mapping_dict`.

    Args:
        columns: All detected file column names.
        suggested: Auto-suggested canonical field -> source column bindings.

    Returns:
        The current canonical field -> source column (or None) mapping.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def _render_selectbox_fallback(
    columns: list[str], suggested: dict[str, str]
) -> dict[str, str | None]:
    """Render the no-dependency fallback: one `st.selectbox` per canonical field.

    Each selectbox lists the detected columns plus `NO_COLUMN_LABEL`, defaulting
    to `suggested`. Produces the same `mapping_dict` shape as the bucket UI.

    Args:
        columns: All detected file column names.
        suggested: Auto-suggested canonical field -> source column bindings.

    Returns:
        The current canonical field -> source column (or None) mapping.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def render_mapping_ui(raw: RawTable, suggested: dict[str, str]) -> MappingResult:
    """Render the full visual mapping step and return a validated `MappingResult`.

    Orchestration (Phase 2):
        1. Let the user pick `amount_sign_convention` (drives which target
           buckets are required/shown).
        2. Render the drag-and-drop buckets via `_render_sortable_buckets`,
           falling back to `_render_selectbox_fallback` if streamlit-sortables
           is unavailable (imported lazily inside this function).
        3. Collect the locale/currency/internal-transfer declarations.
        4. Validate via `_validate_mapping` and populate `is_valid` / `errors`.

    Args:
        raw: The ingested source-agnostic table to map.
        suggested: Auto-suggested bindings from `suggest_mapping`, used to
            pre-populate the buckets/selectboxes.

    Returns:
        A `MappingResult` reflecting the current UI state (its `is_valid`
        gates the downstream preview + save-profile steps in app.py).

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError
