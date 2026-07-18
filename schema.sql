-- SQLite DDL for the Personal Finance MVP.
-- Idempotent import model: transactions.transaction_hash is the PK, so
-- re-importing the same CSV is a no-op via `INSERT OR IGNORE`.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    account_id   TEXT PRIMARY KEY,
    account_type TEXT NOT NULL CHECK (account_type IN ('bank_account', 'credit_card')),
    bank_name    TEXT NOT NULL,
    -- Balance as of the END of opening_balance_date (i.e. it already includes
    -- every transaction up to and including that date).
    opening_balance      REAL NOT NULL DEFAULT 0.0,
    opening_balance_date TEXT,  -- ISO date: the account's tracking start date
    currency             TEXT CHECK (currency IN ('BRL', 'EUR'))  -- the account's own currency
);

CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_hash     TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL REFERENCES accounts (account_id),
    account_type         TEXT NOT NULL CHECK (account_type IN ('bank_account', 'credit_card')),
    date                 TEXT NOT NULL,  -- ISO 8601 date, e.g. "2024-03-01"
    amount                REAL NOT NULL,  -- canonical sign: spend negative, income/refund positive
    currency              TEXT NOT NULL CHECK (currency IN ('BRL', 'EUR')),
    description            TEXT NOT NULL,
    category                TEXT NOT NULL DEFAULT 'Uncategorized' REFERENCES categories (name),
    is_internal_transfer     INTEGER NOT NULL DEFAULT 0 CHECK (is_internal_transfer IN (0, 1)),
    -- Provenance of the category: 'llm' (auto), 'manual' (user edit, never
    -- overwritten by a later LLM run), or 'rule' (forced, e.g. internal transfer).
    category_source          TEXT NOT NULL DEFAULT 'llm' CHECK (category_source IN ('llm', 'manual', 'rule')),
    -- 1 when date <= the account's opening_balance_date: already baked into the
    -- opening balance, so EXCLUDED from running balance and spend metrics by
    -- default (kept, never discarded). Recomputed when the opening date changes.
    is_before_tracking       INTEGER NOT NULL DEFAULT 0 CHECK (is_before_tracking IN (0, 1)),
    -- User-editable display label. The ORIGINAL `description` is IMMUTABLE (it
    -- feeds transaction_hash); effective display = COALESCE(description_override, description).
    description_override     TEXT,
    -- Free-text personal note. LOCAL-ONLY: never sent to the LLM.
    notes                    TEXT
);

-- Manually-tracked non-liquid assets (e.g. ETFs, investments), kept STRICTLY
-- separate from the transactional cash flow. Each asset carries its own
-- currency; BRL and EUR are never mixed or converted.
CREATE TABLE IF NOT EXISTS assets (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    currency TEXT NOT NULL CHECK (currency IN ('BRL', 'EUR'))
);

-- Historical valuation snapshots per asset. The composite PK enforces one
-- snapshot per asset per day, so re-logging the same date UPSERTS the balance
-- instead of duplicating it.
CREATE TABLE IF NOT EXISTS asset_valuation_history (
    asset_id INTEGER NOT NULL REFERENCES assets (id),
    date     TEXT NOT NULL,          -- ISO 8601 date, e.g. "2024-03-01"
    balance  REAL NOT NULL,          -- valuation in the asset's own currency
    PRIMARY KEY (asset_id, date)
);

-- Planned budget per (category, currency). BRL and EUR are never mixed.
CREATE TABLE IF NOT EXISTS budgets (
    category        TEXT NOT NULL REFERENCES categories (name),
    currency        TEXT NOT NULL CHECK (currency IN ('BRL', 'EUR')),
    planned_amount  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (category, currency)
);

CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions (account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_account_type ON transactions (account_type);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date);
CREATE INDEX IF NOT EXISTS idx_transactions_currency ON transactions (currency);
CREATE INDEX IF NOT EXISTS idx_transactions_category ON transactions (category);
