"""Golden-sample acceptance fixtures for the interactive-import feature.

DATA, not logic. For each source type, this pins the expected `RawTable`
(file_ingest output) and the expected canonical DataFrame rows produced by
bridging RawTable -> csv_mapper.process_csv (via raw_table_to_csv_bytes).
Transaction hashes are pre-computed with csv_mapper._compute_transaction_hash
and text_utils.normalize_description, so Phase 2 can compare directly.

Coverage:
    - CSV, BR locale, ';' delimiter, signed amount     -> itau_ingest
      (incl. a 'PGTO FATURA' internal-transfer row).
    - XLSX with a title row above the header (header_row=2), debit/credit
      columns                                           -> santander_ingest.
    - PDF bank statement, multi-line description        -> EXPECTED_PDF_RESULT
      (must set needs_manual_review=True, never raise).
    - Fingerprint reuse: itau_reuse.csv shares itau_ingest's columns, so its
      schema_fingerprint matches (one-click 'Reaproveitar mapeamento salvo').
"""

from __future__ import annotations

# --- Expected RawTable previews (file_ingest output) ---------------------

EXPECTED_ITAU_CSV_RAWTABLE = {
    "columns": ["Data", "Descricao", "Valor"],
    "sample_rows": [
        {"Data": "05/04/2024", "Descricao": "PADARIA CENTRAL", "Valor": "-45,90"},
        {"Data": "06/04/2024", "Descricao": "PGTO FATURA CARTAO", "Valor": "-1.200,00"},
        {"Data": "07/04/2024", "Descricao": "TRANSFERENCIA RECEBIDA", "Valor": "3.500,00"},
    ],
    "row_count": 3,
    "source_type": "csv",
    "sheet_name": None,
    "pdf_strategy": None,
}

# Best-guess dialect sniff_csv should return for itau_csv_br.csv.
EXPECTED_ITAU_CSV_DIALECT = {
    "delimiter": ";",
    "encoding": "utf-8",
    "decimal_separator": ",",
    "thousands_separator": ".",
}

EXPECTED_SANTANDER_XLSX_RAWTABLE = {
    "columns": ["Data", "Historico", "Debito", "Credito"],
    "sample_rows": [
        {"Data": "08/04/2024", "Historico": "FARMACIA SAUDE", "Debito": "89,90", "Credito": ""},
        {"Data": "09/04/2024", "Historico": "DEPOSITO SALARIO", "Debito": "", "Credito": "5.000,00"},
    ],
    "row_count": 2,
    "source_type": "xls",
    "sheet_name": "Extrato",
    "pdf_strategy": None,
}
# Read with: file_ingest.read_excel(bytes, sheet_name='Extrato', header_row=2)

# --- Expected canonical DataFrame rows (post process_csv bridge) ---------

EXPECTED_ITAU_CANONICAL = [
    {
        "transaction_hash": "07b2d03cfadaea4df8c3d215a06bbb8339cfd10423d33c6d65be9186970688ba",
        "account_id": "itau_ingest",
        "account_type": "bank_account",
        "date": "2024-04-05",
        "amount": -45.9,
        "currency": "BRL",
        "description": "PADARIA CENTRAL",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
    {
        "transaction_hash": "abce6913ca101a28b3a5da2bfd60f3c8f56ef8f1679008639e0476d9350df6f5",
        "account_id": "itau_ingest",
        "account_type": "bank_account",
        "date": "2024-04-06",
        "amount": -1200.0,
        "currency": "BRL",
        "description": "PGTO FATURA CARTAO",
        "category": "Transferência interna",
        "is_internal_transfer": True,
    },
    {
        "transaction_hash": "5393186c7d6883fe50e96218cb71f00107de019d58f0fcd5624605128e90afd8",
        "account_id": "itau_ingest",
        "account_type": "bank_account",
        "date": "2024-04-07",
        "amount": 3500.0,
        "currency": "BRL",
        "description": "TRANSFERENCIA RECEBIDA",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
]

EXPECTED_SANTANDER_CANONICAL = [
    {
        "transaction_hash": "803d420919dd1876cdf7900f2f7bfc815bf62d4b329d4f9b779727c2943da7bc",
        "account_id": "santander_ingest",
        "account_type": "bank_account",
        "date": "2024-04-08",
        "amount": -89.9,
        "currency": "BRL",
        "description": "FARMACIA SAUDE",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
    {
        "transaction_hash": "8db9fe5c5ca051d04193cde00500fc4331e721d42d4f0fd7792a52cc95f8ae53",
        "account_id": "santander_ingest",
        "account_type": "bank_account",
        "date": "2024-04-09",
        "amount": 5000.0,
        "currency": "BRL",
        "description": "DEPOSITO SALARIO",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
]

# --- PDF: extraction is fragile; only the review contract is asserted ----
EXPECTED_PDF_RESULT = {
    # read_pdf MUST NOT raise on this file; it MUST flag manual review
    # (free-text layout, multi-line description -> ragged/unsafe extraction).
    "needs_manual_review": True,
    "warnings_non_empty": True,
    # raw_table may be None or a partial/misaligned table; either is allowed
    # so long as needs_manual_review is True.
}

# --- Fingerprint reuse ---------------------------------------------------
ITAU_SCHEMA_FINGERPRINT = "10228d471b47ddd9b2e072d08f23ce52ee9997895e99ed0b307a1970ab868a7f"
SANTANDER_SCHEMA_FINGERPRINT = "df7d787a2c27321b0c5cd9ffcf722bb63757d7b5b11c73f4f36f3d22f94520bc"
# itau_reuse.csv has the SAME columns as itau_csv_br.csv, so:
#   compute_schema_fingerprint(read_csv(itau_reuse).columns) == ITAU_SCHEMA_FINGERPRINT
# and find_profile_by_fingerprint(...) returns the itau_ingest profile.

