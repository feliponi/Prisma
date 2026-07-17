"""Backend module for CSV bank-statement mapping configuration and sanitization.

Handles dynamic column mapping between arbitrary bank CSV exports and the
canonical internal data model (date, amount, description, category), persists
per-bank mapping profiles as JSON, and sanitizes dirty CSV data (mixed date
formats, Brazilian/US decimal separators, empty rows, embedded headers/footers).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

MAPPINGS_DIR = Path("mappings")
CANONICAL_COLUMNS = ["date", "amount", "description", "category"]
DEFAULT_CATEGORY = "Uncategorized"

# Candidate date formats tried in order when parsing the `date` column.
DATE_FORMATS = [
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%m/%d/%Y",
    "%d/%m/%y",
    "%Y/%m/%d",
]


@dataclass
class BankMappingConfig:
    """Represents a saved column mapping profile for a specific bank."""

    bank_name: str
    date_column: str
    amount_column: str
    description_column: str
    date_format_hint: str | None = None
    decimal_separator: str = "auto"  # "auto", "," or "."
    csv_delimiter: str = ","
    encoding: str = "utf-8"
    skip_rows: int = 0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "bank_name": self.bank_name,
            "date_column": self.date_column,
            "amount_column": self.amount_column,
            "description_column": self.description_column,
            "date_format_hint": self.date_format_hint,
            "decimal_separator": self.decimal_separator,
            "csv_delimiter": self.csv_delimiter,
            "encoding": self.encoding,
            "skip_rows": self.skip_rows,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BankMappingConfig":
        return cls(
            bank_name=data["bank_name"],
            date_column=data["date_column"],
            amount_column=data["amount_column"],
            description_column=data["description_column"],
            date_format_hint=data.get("date_format_hint"),
            decimal_separator=data.get("decimal_separator", "auto"),
            csv_delimiter=data.get("csv_delimiter", ","),
            encoding=data.get("encoding", "utf-8"),
            skip_rows=data.get("skip_rows", 0),
            extra=data.get("extra", {}),
        )


def _sanitize_filename(bank_name: str) -> str:
    """Convert a bank name into a filesystem-safe slug for the config file."""
    slug = "".join(c if c.isalnum() else "_" for c in bank_name.strip().lower())
    return slug.strip("_") or "unnamed_bank"


def get_mapping_path(bank_name: str, mappings_dir: Path = MAPPINGS_DIR) -> Path:
    """Return the JSON config path for a given bank name."""
    return mappings_dir / f"{_sanitize_filename(bank_name)}_config.json"


def list_saved_mappings(mappings_dir: Path = MAPPINGS_DIR) -> list[str]:
    """List bank names for which a saved mapping profile exists."""
    if not mappings_dir.exists():
        return []
    names = []
    for path in sorted(mappings_dir.glob("*_config.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            names.append(data.get("bank_name", path.stem))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read mapping file %s: %s", path, exc)
    return names


def save_mapping(config: BankMappingConfig, mappings_dir: Path = MAPPINGS_DIR) -> Path:
    """Persist a bank mapping configuration as a JSON file.

    Raises:
        OSError: if the mappings directory cannot be created or written to.
    """
    mappings_dir.mkdir(parents=True, exist_ok=True)
    path = get_mapping_path(config.bank_name, mappings_dir)
    path.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved mapping profile for bank '%s' to %s", config.bank_name, path)
    return path


def load_mapping(bank_name: str, mappings_dir: Path = MAPPINGS_DIR) -> BankMappingConfig | None:
    """Load a previously saved mapping configuration, or None if not found."""
    path = get_mapping_path(bank_name, mappings_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return BankMappingConfig.from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        logger.error("Failed to load mapping for '%s': %s", bank_name, exc)
        return None


def _parse_date_series(series: pd.Series) -> pd.Series:
    """Parse a string date series trying several known formats before a
    generic fallback. Unparseable values become NaT."""
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    remaining_mask = series.notna()

    for fmt in DATE_FORMATS:
        if not remaining_mask.any():
            break
        candidates = series[remaining_mask]
        attempt = pd.to_datetime(candidates, format=fmt, errors="coerce")
        success_mask = attempt.notna()
        success_index = candidates.index[success_mask]
        parsed.loc[success_index] = attempt[success_mask]
        remaining_mask.loc[success_index] = False

    if remaining_mask.any():
        fallback = pd.to_datetime(series[remaining_mask], errors="coerce", dayfirst=True)
        parsed.loc[fallback.index] = fallback

    return parsed


def _normalize_amount_value(raw_value: object, decimal_separator: str) -> str:
    """Normalize a single amount string to a plain float-parsable string,
    handling Brazilian (1.000,50) and US (1,000.50) thousand/decimal styles."""
    if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
        return ""
    value = str(raw_value).strip()
    value = value.replace("R$", "").replace("$", "").strip()
    value = value.replace(" ", "")

    is_negative = False
    if value.startswith("(") and value.endswith(")"):
        is_negative = True
        value = value[1:-1]

    if decimal_separator == "auto":
        has_comma = "," in value
        has_dot = "." in value
        if has_comma and has_dot:
            # Whichever separator appears last is the decimal separator.
            if value.rfind(",") > value.rfind("."):
                value = value.replace(".", "").replace(",", ".")
            else:
                value = value.replace(",", "")
        elif has_comma:
            # Comma-only: treat as decimal separator (Brazilian style).
            value = value.replace(".", "").replace(",", ".")
        # dot-only or neither: already float-compatible.
    elif decimal_separator == ",":
        value = value.replace(".", "").replace(",", ".")
    elif decimal_separator == ".":
        value = value.replace(",", "")

    if is_negative and not value.startswith("-"):
        value = f"-{value}"

    return value


def _parse_amount_series(series: pd.Series, decimal_separator: str) -> pd.Series:
    """Parse an amount column of mixed string formats into floats."""
    string_series = series.astype(str)
    normalized = string_series.apply(lambda v: _normalize_amount_value(v, decimal_separator))
    return pd.to_numeric(normalized, errors="coerce")


def _drop_junk_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Drop fully empty rows and rows that look like embedded headers/footers
    (e.g. repeated header text or summary lines with no valid amount)."""
    df = df.dropna(how="all")
    df = df.dropna(subset=["date", "amount"], how="all")
    return df


def process_csv(
    file_or_buffer,
    config: BankMappingConfig,
) -> pd.DataFrame:
    """Read a raw bank CSV and transform it into the canonical DataFrame.

    Args:
        file_or_buffer: Path, file-like object, or buffer accepted by pandas.read_csv.
        config: The bank mapping configuration describing source columns and formats.

    Returns:
        A sanitized DataFrame with columns [date, amount, description, category].

    Raises:
        ValueError: if required mapped columns are missing from the CSV.
        pandas.errors.ParserError: if the CSV cannot be parsed at all.
    """
    try:
        raw_df = pd.read_csv(
            file_or_buffer,
            delimiter=config.csv_delimiter,
            encoding=config.encoding,
            skiprows=config.skip_rows,
            dtype=str,
            engine="python",
            skip_blank_lines=True,
        )
    except (pd.errors.ParserError, UnicodeDecodeError, OSError) as exc:
        logger.error("Failed to read CSV for bank '%s': %s", config.bank_name, exc)
        raise

    raw_df.columns = [str(c).strip() for c in raw_df.columns]

    required = [config.date_column, config.amount_column, config.description_column]
    missing = [c for c in required if c not in raw_df.columns]
    if missing:
        raise ValueError(
            f"Mapped columns not found in CSV for bank '{config.bank_name}': {missing}. "
            f"Available columns: {list(raw_df.columns)}"
        )

    df = pd.DataFrame()
    df["date"] = _parse_date_series(raw_df[config.date_column].astype(str).str.strip())
    df["amount"] = _parse_amount_series(raw_df[config.amount_column], config.decimal_separator)
    df["description"] = raw_df[config.description_column].astype(str).str.strip()
    df["category"] = DEFAULT_CATEGORY

    df = _drop_junk_rows(df)
    df = df[df["description"].str.len() > 0]
    df = df.reset_index(drop=True)

    logger.info(
        "Processed CSV for bank '%s': %d valid transactions extracted", config.bank_name, len(df)
    )
    return df
