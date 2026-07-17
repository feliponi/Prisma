# Golden Sample — Acceptance Test for `csv_mapper.process_csv`

This fixture is the acceptance test for Phase 2's `csv_mapper.process_csv`
and, indirectly, for the idempotent-import contract of `db.insert_transactions`.

## Layout

- `profiles/*_config.json` — `AccountProfile` JSON, one per simulated account.
- `raw/*_raw.csv` — the dirty CSV export for that account.
- `expected_output.py` — the exact canonical rows `process_csv(raw_bytes, profile)`
  must produce for each pair above, keyed by `account_id` in `ALL_EXPECTED`.

## Accounts and what each proves

| account_id         | account_type  | Proves                                                                 |
|---------------------|---------------|-------------------------------------------------------------------------|
| `itau_corrente`     | bank_account  | BR-locale decimals, spend negative (no invert), `PGTO CARTAO` internal transfer, embedded `Saldo Anterior` footer row, fully empty row — all dropped/tagged correctly. |
| `nubank_cc`         | credit_card   | Purchase listed POSITIVE in the raw CSV, normalized to NEGATIVE via `invert_sign: true`; a refund/estorno listed negative, normalized to POSITIVE. |
| `wise_multi`        | bank_account  | EUR-locale decimals, and a per-row `Currency` column (BRL and EUR rows in the same account). |
| `santander_dc`      | bank_account  | Bonus: `debit_credit_columns` sign convention. |
| `bpi_parentheses`   | bank_account  | Bonus: `parentheses` sign convention. |

## Cross-account dedup signal

`itau_corrente` row 0 and `nubank_cc` row 0 both normalize to the description
`"SUPERMERCADO PAO DE ACUCAR"`. Their `transaction_hash` values differ
(because `account_id` is embedded in the hash, keeping the card purchase and
any later bank-side bill payment distinct), but
`ai_services.categorize_transactions` should still resolve both with a
**single** cached LLM call once Phase 2 is implemented, per the
normalized-description cache key contract.

## How Phase 2 should use this fixture

```python
from pathlib import Path
from csv_mapper import process_csv
from models import AccountProfile
from tests.golden_sample.expected_output import ALL_EXPECTED

for account_id, expected_rows in ALL_EXPECTED.items():
    raw_bytes = Path(f"tests/golden_sample/raw/{account_id}_raw.csv").read_bytes()
    profile = AccountProfile.from_dict(
        json.loads(Path(f"tests/golden_sample/profiles/{account_id}_config.json").read_text())
    )
    result_df = process_csv(raw_bytes, profile)
    # assert result_df, row-for-row, matches expected_rows (order + values)
```

All `transaction_hash` values were pre-computed against the formula in
`csv_mapper._compute_transaction_hash`:

```
sha256(f"{account_id}|{date_iso}|{amount}|{currency}|{description_normalized}")
```

using the normalization rule documented in `text_utils.normalize_description`
(uppercase, strip, collapse internal whitespace). `date_iso` is
`"YYYY-MM-DD"`; `amount` is interpolated via Python's default `float` repr
(e.g. `-1000.5`, not `-1000.50`).
