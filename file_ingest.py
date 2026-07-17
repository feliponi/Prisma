"""File ingestion layer: turn arbitrary uploaded bytes into a `RawTable`.

Contracts only (Phase 1). Every function is fully type-hinted and documented
but raises NotImplementedError; bodies land in Phase 2.

Single responsibility: SOURCE-AGNOSTIC extraction of tabular structure
(headers + sample rows + row count) from CSV / XLS(X) / PDF bytes. This
module holds NO canonical-model semantics — it never maps to date/amount/etc.
and never computes transaction hashes. Mapping lives in mapping_ui.py and the
canonical transform lives in csv_mapper.py.

Runtime dependencies (Phase 2, imported lazily inside the readers so this
module stays importable without them):
    - openpyxl  -> Excel reading (`read_excel`, `list_excel_sheets`).
    - pdfplumber -> PDF table extraction (`read_pdf`).
CSV reading uses the standard library + pandas only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

# How many leading data rows to retain as a preview in every RawTable.
SAMPLE_ROW_LIMIT = 20

SourceType = Literal["csv", "xls", "pdf"]
PdfStrategy = Literal["lattice", "stream"]
Encoding = Literal["utf-8", "latin-1"]


@dataclass(frozen=True)
class CsvDialect:
    """Best-guess parsing parameters for a CSV, all user-overridable in the UI.

    Every field is a heuristic guess produced by `sniff_csv`; the mapping UI
    must expose each one for override before any parse is trusted.
    """

    delimiter: str
    encoding: Encoding
    decimal_separator: str
    thousands_separator: str
    quotechar: str = '"'
    has_header: bool = True


@dataclass(frozen=True)
class RawTable:
    """Source-agnostic tabular preview of an uploaded file.

    All sample values are raw strings exactly as extracted (no locale/number
    parsing here). `columns` are the detected header names in file order.
    """

    columns: list[str]
    sample_rows: list[dict[str, str]]
    row_count: int
    source_type: SourceType
    sheet_name: str | None = None
    pdf_strategy: str | None = None


@dataclass
class PdfExtractionResult:
    """Result wrapper for the fragile PDF path.

    PDF table extraction may fail or misalign; the reader NEVER raises on a
    bad table. Instead it returns this wrapper so the UI can surface warnings
    and require explicit user confirmation (or abort) before the extracted
    `raw_table` is trusted.
    """

    raw_table: RawTable | None
    warnings: list[str] = field(default_factory=list)
    needs_manual_review: bool = False


def sniff_csv(raw_bytes: bytes) -> CsvDialect:
    """Detect a CSV's dialect by sampling: delimiter, encoding, and separators.

    Detection strategy (Phase 2):
        - Try decoding as utf-8, falling back to latin-1.
        - Use `csv.Sniffer` (and delimiter frequency heuristics) on a sample
          to guess the field delimiter and quote character.
        - Infer decimal vs thousands separators from numeric-looking cells
          (e.g. "1.234,56" -> decimal ",", thousands "."; "1,234.56" -> the
          reverse). Best guess only; the UI must let the user override each.

    Args:
        raw_bytes: The uploaded CSV file content.

    Returns:
        The best-guess `CsvDialect`.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def read_csv(raw_bytes: bytes, dialect: CsvDialect) -> RawTable:
    """Read CSV bytes into a `RawTable` using the (possibly user-edited) dialect.

    Args:
        raw_bytes: The uploaded CSV file content.
        dialect: The dialect to parse with (from `sniff_csv`, after any
            user overrides).

    Returns:
        A `RawTable` with `source_type == "csv"`, up to `SAMPLE_ROW_LIMIT`
        sample rows, and the full `row_count`.

    Raises:
        UnicodeDecodeError: if `raw_bytes` cannot be decoded with the
            dialect's encoding.
        ValueError: if the content cannot be parsed as delimited text.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def list_excel_sheets(raw_bytes: bytes) -> list[str]:
    """List sheet names in an uploaded XLS/XLSX workbook (openpyxl engine).

    Args:
        raw_bytes: The uploaded Excel workbook content.

    Returns:
        Sheet names in workbook order. The UI presents these so the user can
        choose which sheet to import.

    Raises:
        ValueError: if the bytes are not a readable Excel workbook.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def read_excel(raw_bytes: bytes, sheet_name: str, header_row: int) -> RawTable:
    """Read one Excel sheet into a `RawTable`, treating `header_row` as the header.

    Rows above `header_row` (e.g. an embedded title/subtitle row) are skipped;
    the row at `header_row` supplies `columns`; subsequent rows are data.

    Args:
        raw_bytes: The uploaded Excel workbook content.
        sheet_name: The sheet to read (from `list_excel_sheets`).
        header_row: Zero-based index of the row holding the column headers.

    Returns:
        A `RawTable` with `source_type == "xls"` and `sheet_name` set.

    Raises:
        ValueError: if the sheet or header row cannot be read.
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def raw_table_to_csv_bytes(raw: RawTable, delimiter: str, encoding: Encoding = "utf-8") -> bytes:
    """Serialize a `RawTable` back into delimited CSV bytes.

    This is the bridge that lets the UNCHANGED `csv_mapper.process_csv`
    (which parses delimited bytes) consume XLS- and PDF-sourced data too:
    ingest -> RawTable -> `raw_table_to_csv_bytes` -> `process_csv`. The
    header line is `raw.columns` joined by `delimiter`; each data line is the
    corresponding `sample_rows` values in column order.

    Note: `RawTable` is a preview (up to `SAMPLE_ROW_LIMIT` rows), so this is
    used for the dry-run/preview and for small imports; the Phase-2 full-import
    path re-extracts all rows before serializing.

    Args:
        raw: The ingested table to serialize.
        delimiter: Field delimiter to emit (typically `profile.delimiter`).
        encoding: Output text encoding.

    Returns:
        CSV-encoded bytes suitable for `csv_mapper.process_csv`.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError


def read_pdf(raw_bytes: bytes, strategy: PdfStrategy, header_row: int) -> PdfExtractionResult:
    """Attempt PDF table extraction (pdfplumber), never raising on a bad table.

    PDF is fragile and has no guaranteed columns. This function:
        - extracts tables using the given `strategy` ("lattice" for ruled
          tables, "stream" for whitespace-aligned tables);
        - treats `header_row` (within the best candidate table) as the header;
        - on failure/misalignment, returns a result with
          `raw_table=None` (or a partial table), a populated `warnings` list,
          and `needs_manual_review=True` — it does NOT raise;
        - never auto-trusts extraction: the UI must have the user confirm.

    Args:
        raw_bytes: The uploaded PDF content.
        strategy: pdfplumber table-detection strategy.
        header_row: Zero-based header-row index within the detected table.

    Returns:
        A `PdfExtractionResult`. `needs_manual_review` is True whenever the
        extraction is uncertain (no table found, ragged column counts, empty
        headers, or a single-column blob).

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError
