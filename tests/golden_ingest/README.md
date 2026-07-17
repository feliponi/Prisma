# Golden Sample — Interactive Import (ingestion + visual mapping)

Acceptance test for the upload-first, source-agnostic import pipeline
(`file_ingest.py` + `mapping_ui.py` + the `csv_mapper` fingerprint/reuse
deltas). Pin these fixtures in Phase 2.

## Layout

- `raw/*` — one dirty input per source type.
- `profiles/*_config.json` — the target saved `AccountProfile` shape,
  including the additive import-provenance fields
  (`source_type`, `sheet_name`, `header_row`, `pdf_strategy`,
  `schema_fingerprint`).
- `expected_ingest.py` — expected `RawTable`/`CsvDialect` previews, expected
  canonical DataFrame rows (with pre-computed `transaction_hash`), the PDF
  review contract, and the expected schema fingerprints.

## Cases

| Input | Source | Proves |
|-------|--------|--------|
| `raw/itau_csv_br.csv` | CSV | BR locale, `;` delimiter, signed amount; includes a `PGTO FATURA` internal-transfer row. `sniff_csv` → `EXPECTED_ITAU_CSV_DIALECT`; `read_csv` → `EXPECTED_ITAU_CSV_RAWTABLE`; canonical → `EXPECTED_ITAU_CANONICAL`. |
| `raw/santander_xlsx_titlerow.xlsx` | XLSX | Title row above the header (so `read_excel(..., header_row=2)`), debit/credit columns. → `EXPECTED_SANTANDER_XLSX_RAWTABLE` / `EXPECTED_SANTANDER_CANONICAL`. |
| `raw/bank_pdf_multiline.pdf` | PDF | Free-text layout with a multi-line description. `read_pdf` **must not raise** and **must** return `needs_manual_review=True` with non-empty `warnings` → `EXPECTED_PDF_RESULT`. |
| `raw/itau_reuse.csv` | CSV | Same columns as `itau_csv_br.csv`, so `compute_schema_fingerprint` equals `ITAU_SCHEMA_FINGERPRINT` and `find_profile_by_fingerprint` returns the `itau_ingest` profile (one-click "Reaproveitar mapeamento salvo"). |

## The ingest → transform bridge

`csv_mapper.process_csv` is unchanged and still parses delimited *bytes*. For
XLS/PDF sources the Phase-2 flow is:

```
bytes --read_excel/read_pdf--> RawTable --raw_table_to_csv_bytes--> csv bytes --process_csv--> canonical DF
```

Because every fixture here fits within `SAMPLE_ROW_LIMIT`, serializing a
fixture's `RawTable` reproduces its full data, so the expected canonical rows
are exactly what `process_csv` must yield.

## Fingerprint formula (contract)

```
schema_fingerprint = sha256("\n".join(sorted(columns)).encode("utf-8")).hexdigest()
```

Order-independent (only the sorted column set matters), so the same bank
export reused month to month fingerprints identically. Verified in
`expected_ingest.py` against the live `_compute_transaction_hash` and the
documented fingerprint formula.
