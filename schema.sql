-- SQLite DDL for the Personal Finance MVP.
-- Idempotent import model: transactions.transaction_hash is the PK, so
-- re-importing the same CSV is a no-op via `INSERT OR IGNORE`.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    account_id   TEXT PRIMARY KEY,
    account_type TEXT NOT NULL CHECK (account_type IN ('bank_account', 'credit_card')),
    bank_name    TEXT NOT NULL
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
    is_internal_transfer     INTEGER NOT NULL DEFAULT 0 CHECK (is_internal_transfer IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions (account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_account_type ON transactions (account_type);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (date);
CREATE INDEX IF NOT EXISTS idx_transactions_currency ON transactions (currency);
