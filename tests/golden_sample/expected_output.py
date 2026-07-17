"""Golden-sample acceptance fixture for `csv_mapper.process_csv`.

This is DATA, not module logic: it defines the exact canonical rows Phase 2's
`process_csv` must produce for each raw CSV / profile pair under
`tests/golden_sample/`. `transaction_hash` values are pre-computed with the
formula from `csv_mapper._compute_transaction_hash` and the normalization
rule documented in `text_utils.normalize_description` (uppercase, strip,
collapse internal whitespace), so equality checks in Phase 2 can compare
hashes directly rather than re-deriving them.

Coverage checklist (per the Phase 1 spec's golden-sample requirements):
    - BR-locale decimal "1.000,50"          -> itau_corrente row 0
    - EUR-locale decimal "1,000.50"         -> wise_multi row 0
    - Card purchase POSITIVE + invert_sign  -> nubank_cc row 0
    - Card refund/estorno -> POSITIVE       -> nubank_cc row 1
    - Bank spend -> NEGATIVE (no invert)    -> itau_corrente row 0
    - "PGTO CARTAO" internal transfer       -> itau_corrente row 1
    - Embedded "Saldo Anterior" footer      -> itau_corrente (dropped)
    - Fully empty row                       -> itau_corrente (dropped)
    - Per-row currency column               -> wise_multi (EUR row 0, BRL row 1)
    - Repeated merchant across accounts     -> itau_corrente row 0 and
      nubank_cc row 0 share the SAME normalized_description
      ("SUPERMERCADO PAO DE ACUCAR"), demonstrating that
      ai_services.categorize_transactions should resolve them with a single
      cached LLM call despite belonging to different accounts.
    - debit_credit_columns convention       -> santander_dc (bonus coverage)
    - parentheses convention                -> bpi_parentheses (bonus coverage)

Each row dict's keys match `models.TransactionRecord` field-for-field.
"""

from __future__ import annotations

EXPECTED_ITAU_CORRENTE = [
    {
        "transaction_hash": "44f4c5b0bf661eb4b107b86b7ebec45b3c04ef68813f8ab49e34b202598b6cbf",
        "account_id": "itau_corrente",
        "account_type": "bank_account",
        "date": "2024-03-01",
        "amount": -1000.5,
        "currency": "BRL",
        "description": "SUPERMERCADO PAO DE ACUCAR",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
    {
        "transaction_hash": "2ddcda235d79c86635d2e965fe41c82abfa3a64c97416fe0ef7d016d29fa6475",
        "account_id": "itau_corrente",
        "account_type": "bank_account",
        "date": "2024-03-02",
        "amount": -2500.0,
        "currency": "BRL",
        "description": "PGTO CARTAO NUBANK",
        "category": "Transferência interna",
        "is_internal_transfer": True,
    },
    {
        "transaction_hash": "86e7a33186a78dcf8dae13d660e83db0eb6255f5b5e01d17d6c397470bc39ed5",
        "account_id": "itau_corrente",
        "account_type": "bank_account",
        "date": "2024-03-03",
        "amount": 5000.0,
        "currency": "BRL",
        "description": "SALARIO EMPRESA XPTO",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
]

EXPECTED_NUBANK_CC = [
    {
        "transaction_hash": "79ff0aad8fa80e6ab212299f492c2b1ed8b4e09a102b1e2fbbc636441657bcf1",
        "account_id": "nubank_cc",
        "account_type": "credit_card",
        "date": "2024-03-01",
        "amount": -150.75,
        "currency": "BRL",
        "description": "SUPERMERCADO PAO DE ACUCAR",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
    {
        "transaction_hash": "e7c033f504026c8fff6f4dead5a0bcb61d907534833f319281796da5a1172a88",
        "account_id": "nubank_cc",
        "account_type": "credit_card",
        "date": "2024-03-03",
        "amount": 50.0,
        "currency": "BRL",
        "description": "ESTORNO SUPERMERCADO PAO DE ACUCAR",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
]

EXPECTED_WISE_MULTI = [
    {
        "transaction_hash": "c75eb82f8a6279c738b13cd27d51f315a0c4165c69bcfd00b4a7025605dde04c",
        "account_id": "wise_multi",
        "account_type": "bank_account",
        "date": "2024-03-05",
        "amount": -1000.5,
        "currency": "EUR",
        "description": "RENT PAYMENT LISBON",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
    {
        "transaction_hash": "cbf698ae3de4e8861e2e4aabf5ce6c208e724c0faa0a3244582ea7c20162b72b",
        "account_id": "wise_multi",
        "account_type": "bank_account",
        "date": "2024-03-06",
        "amount": 2500.0,
        "currency": "BRL",
        "description": "FREELANCE PAYMENT BRAZIL",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
]

EXPECTED_SANTANDER_DC = [
    {
        "transaction_hash": "b1d01cf29a9c49fee224d7948542f7dab1268454c332b573390424deb7efd637",
        "account_id": "santander_dc",
        "account_type": "bank_account",
        "date": "2024-03-07",
        "amount": -89.9,
        "currency": "BRL",
        "description": "FARMACIA SAO PAULO",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
    {
        "transaction_hash": "0e6631d01ac4dd2fff34c9e8118724ffc688c11406de250b9961fa40f518645e",
        "account_id": "santander_dc",
        "account_type": "bank_account",
        "date": "2024-03-08",
        "amount": 1200.0,
        "currency": "BRL",
        "description": "DEPOSITO TED",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
]

EXPECTED_BPI_PARENTHESES = [
    {
        "transaction_hash": "de15eaa1e1980376ea571049df9c0dac2bb45e1a9653040134ee0d575bc184a2",
        "account_id": "bpi_parentheses",
        "account_type": "bank_account",
        "date": "2024-03-09",
        "amount": -45.0,
        "currency": "EUR",
        "description": "RESTAURANTE PORTO",
        "category": "Uncategorized",
        "is_internal_transfer": False,
    },
]

ALL_EXPECTED = {
    "itau_corrente": EXPECTED_ITAU_CORRENTE,
    "nubank_cc": EXPECTED_NUBANK_CC,
    "wise_multi": EXPECTED_WISE_MULTI,
    "santander_dc": EXPECTED_SANTANDER_DC,
    "bpi_parentheses": EXPECTED_BPI_PARENTHESES,
}
