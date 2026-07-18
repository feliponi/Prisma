"""Incremental-import service: pre-import visibility + legitimate-collision handling.

This module adds ONLY what was missing around the existing, unchanged dedup
mechanism (transaction_hash + INSERT OR IGNORE):

  (a) Visibility: compute the new-vs-duplicate split BEFORE inserting, by
      diffing incoming hashes against those already stored for the account in
      the incoming date range.
  (b) Legitimate collisions: two genuinely distinct transactions that share
      (account_id, date, amount, currency, normalized description) collapse to
      one hash; the second would be silently dropped. We surface these groups
      and, when the user marks a group "distinct", disambiguate deterministically
      by appending an occurrence ordinal ("|#2", "|#3", …) to the hash INPUT so
      re-importing the SAME file yields the SAME hashes and stays idempotent.

The hash formula itself is NOT changed and NO second dedup mechanism is added.
No Streamlit imports here — pure logic, so it stays unit-testable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from csv_mapper import _compute_transaction_hash
from text_utils import normalize_description

logger = logging.getLogger(__name__)


@dataclass
class CollisionGroup:
    """A set of >1 incoming rows that share one transaction_hash within a file.

    These are candidate legitimate duplicates (e.g. two identical coffees on the
    same day). The user decides per group whether they are one transaction
    (import once) or genuinely distinct (import all, disambiguated by ordinal).
    """

    transaction_hash: str
    row_indices: list[int]
    date_iso: str
    amount: float
    currency: str
    description: str

    @property
    def count(self) -> int:
        return len(self.row_indices)


@dataclass
class ImportPlan:
    """Pre-import summary computed BEFORE any write."""

    account_id: str
    total_rows: int
    new_rows: int
    duplicate_rows: int
    internal_transfer_rows: int
    min_date: str | None
    max_date: str | None
    overlaps_existing: bool
    existing_min_date: str | None
    existing_max_date: str | None
    collision_groups: list[CollisionGroup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_import_plan(
    incoming: pd.DataFrame,
    existing_hashes: set[str],
    existing_range: tuple[str, str] | None,
    account_id: str,
    extra_warnings: list[str] | None = None,
) -> ImportPlan:
    """Summarize an incoming canonical frame against what is already stored.

    Args:
        incoming: Canonical DataFrame from `csv_mapper.process_csv` (all files
            for one account concatenated), with its computed `transaction_hash`.
        existing_hashes: Hashes already stored for this account within the
            incoming date range (from `db.existing_hashes`).
        existing_range: The account's current (min_date, max_date), or None.
        account_id: The target account.
        extra_warnings: Parse/ingestion warnings to surface (e.g. PDF review).

    Returns:
        An `ImportPlan` with counts, date-range/overlap info and collision groups.
    """
    warnings = list(extra_warnings or [])
    total = len(incoming)
    if total == 0:
        return ImportPlan(
            account_id=account_id, total_rows=0, new_rows=0, duplicate_rows=0,
            internal_transfer_rows=0, min_date=None, max_date=None,
            overlaps_existing=False, existing_min_date=None, existing_max_date=None,
            warnings=warnings,
        )

    date_iso = incoming["date"].dt.strftime("%Y-%m-%d")
    min_date, max_date = date_iso.min(), date_iso.max()
    internal_rows = int(incoming["is_internal_transfer"].astype(bool).sum())

    # New vs duplicate against the DB (using current, undisambiguated hashes).
    hashes = incoming["transaction_hash"]
    duplicate_rows = int(hashes.isin(existing_hashes).sum())
    new_rows = total - duplicate_rows

    # Intra-file collisions: same hash appearing more than once in THIS batch.
    collision_groups: list[CollisionGroup] = []
    for tx_hash, group in incoming.groupby("transaction_hash"):
        if len(group) > 1:
            first = group.iloc[0]
            collision_groups.append(
                CollisionGroup(
                    transaction_hash=tx_hash,
                    row_indices=group.index.tolist(),
                    date_iso=first["date"].strftime("%Y-%m-%d"),
                    amount=float(first["amount"]),
                    currency=first["currency"],
                    description=first["description"],
                )
            )

    overlaps = False
    ex_min = ex_max = None
    if existing_range is not None:
        ex_min, ex_max = existing_range
        overlaps = not (max_date < ex_min or min_date > ex_max)

    return ImportPlan(
        account_id=account_id,
        total_rows=total,
        new_rows=new_rows,
        duplicate_rows=duplicate_rows,
        internal_transfer_rows=internal_rows,
        min_date=min_date,
        max_date=max_date,
        overlaps_existing=overlaps,
        existing_min_date=ex_min,
        existing_max_date=ex_max,
        collision_groups=collision_groups,
        warnings=warnings,
    )


def apply_collision_decisions(
    incoming: pd.DataFrame, distinct_hashes: set[str]
) -> pd.DataFrame:
    """Disambiguate the collision groups the user marked as genuinely distinct.

    For each hash in `distinct_hashes`, the 2nd, 3rd, … occurrences (in stable
    file order) get a NEW hash computed from the same key with an occurrence
    ordinal appended to the hash input:

        sha256(f"{account_id}|{date_iso}|{amount}|{currency}|{desc_norm}|#{k}")

    The 1st occurrence keeps its original hash. Because file order is stable,
    re-importing the SAME file reproduces the SAME ordinals and therefore the
    SAME hashes — so idempotency (INSERT OR IGNORE -> 0 new rows) is preserved.
    Groups NOT in `distinct_hashes` are left collapsed (treated as one).

    Args:
        incoming: The canonical DataFrame (indexed as built by process_csv +
            concatenation; this function relies only on row order per group).
        distinct_hashes: Hashes of collision groups to treat as distinct.

    Returns:
        A copy of `incoming` with disambiguated `transaction_hash` values.
    """
    if not distinct_hashes:
        return incoming

    result = incoming.copy()
    hash_col = result.columns.get_loc("transaction_hash")

    for tx_hash in distinct_hashes:
        group = result[result["transaction_hash"] == tx_hash]
        if len(group) <= 1:
            continue
        for occurrence, (row_pos, row) in enumerate(group.iterrows(), start=1):
            if occurrence == 1:
                continue  # first keeps the original hash
            desc_norm = normalize_description(str(row["description"]))
            date_iso = row["date"].strftime("%Y-%m-%d")
            # Append the ordinal to the description component so the hashed key
            # becomes "account_id|date|amount|currency|desc_norm|#k" exactly.
            new_hash = _compute_transaction_hash(
                row["account_id"], date_iso, row["amount"], row["currency"],
                f"{desc_norm}|#{occurrence}",
            )
            result.iat[result.index.get_loc(row_pos), hash_col] = new_hash

    return result
