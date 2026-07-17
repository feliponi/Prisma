"""Shared text normalization used both for transaction-hash computation
(csv_mapper) and categorization cache keys (ai_services).

A single implementation is required here: the hash formula embeds
`description_normalized`, and the categorization cache keys off the same
normalized form so that identical merchant descriptions across different
accounts collapse to one cache entry (and one LLM call).
"""

from __future__ import annotations


def normalize_description(description: str) -> str:
    """Normalize a raw transaction description for hashing and cache lookups.

    Intended rule (Phase 2): uppercase, strip leading/trailing whitespace,
    and collapse internal whitespace runs to a single space. Must be
    deterministic and locale-independent so the same merchant string always
    normalizes identically regardless of source account.

    Args:
        description: Raw description as extracted from the CSV.

    Returns:
        The normalized description string.

    Raises:
        NotImplementedError: Phase 2 implementation pending.
    """
    raise NotImplementedError
