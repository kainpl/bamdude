"""B oracle uses the same exact normalisation and accepted data as runtime."""

from backend.app.core.pg_schema_contract import (
    canonical,
    capture,
    catalogue_state,
    compare,
    default_key,
    retained_pairs,
    snapshot,
    unexpected,
)

__all__ = [
    "canonical",
    "capture",
    "catalogue_state",
    "compare",
    "default_key",
    "retained_pairs",
    "snapshot",
    "unexpected",
]
