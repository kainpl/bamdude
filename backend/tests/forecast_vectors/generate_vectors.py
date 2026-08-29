"""Freeze golden rate vectors by EXECUTING the shipped implementation.

Task 1 of the forecast-server-side plan: before any refactor, the current
``backend.app.services.stock_forecast_alerts.history_rate`` / ``delta_rate``
(themselves a deliberate line-by-line port of ``ForecastPanel.tsx``) are run on
crafted inputs and their outputs frozen to ``rate_vectors.json``. The new
``forecast_engine`` must reproduce these numbers exactly — the frozen JSON is
the arbiter; if a vector fails, the ENGINE is wrong, never the vector.

Nothing in here derives an expected value by hand. The inputs are designed, the
outputs are measured.

Regenerate (from the repo root — only legitimate when the SHIPPED module is the
thing that deliberately changed):

    python -m backend.tests.forecast_vectors.generate_vectors

The JSON is committed together with this script so every expected number has an
executable provenance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.app.services.stock_forecast_alerts import delta_rate, history_rate

# Midnight, deliberately: bucket days are UTC-midnight anchored, so with NOW on
# a midnight every whole ``days_ago`` lands exactly on a bucket boundary and the
# half-life vector's observation is exactly 30.0 days old — not 29.5-something.
NOW = datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc)

VECTORS_PATH = Path(__file__).with_name("rate_vectors.json")


@dataclass
class _Record:
    """Stand-in for a SpoolUsageHistory row — ``history_rate`` reads exactly
    these attributes (``created_at`` naive UTC, as the DB stores it)."""

    spool_id: int
    weight_used: float
    created_at: datetime


@dataclass
class _Spool:
    """Stand-in for a Spool row — ``delta_rate`` reads exactly these attributes."""

    weight_used: float
    weight_used_baseline: float
    created_at: datetime


def _at(days_ago: float) -> datetime:
    """A naive-UTC timestamp ``days_ago`` before NOW."""
    return (NOW - timedelta(days=days_ago)).replace(tzinfo=None)


# ── History-tier inputs ──────────────────────────────────────────────────────
# Each case: (name, comment, [(days_ago, grams, spool_id)]).
# Fractional days_ago values put events at intra-day times on purpose — the day
# BUCKET is what the math sees, and the vectors must prove that.

_HISTORY_CASES: list[tuple[str, str, list[tuple[float, float, int]]]] = [
    (
        "two_adjacent_days",
        "The minimal measurable history: one inter-day observation, one day apart.",
        [(1.6, 30.0, 1), (0.4, 50.0, 1)],
    ),
    (
        "two_days_with_a_five_day_gap",
        "One observation across a 5-day gap — the gap divides the newer day's grams.",
        [(7.0, 100.0, 1), (2.0, 100.0, 1)],
    ),
    (
        "six_events_across_four_days_mixed_grams",
        "Six events, three spools, four distinct days, uneven grams and gaps.",
        [(9.5, 10.0, 1), (9.2, 15.0, 2), (6.5, 40.0, 1), (4.3, 5.0, 3), (4.1, 80.0, 1), (1.2, 33.0, 2)],
    ),
    (
        "decay_curve_0_15_30_60_90_days_old",
        "Same grams at 0/15/30/60/90 days old — the frozen mean and spread encode the decay curve.",
        [(90.0, 60.0, 1), (60.0, 60.0, 1), (30.0, 60.0, 1), (15.0, 60.0, 1), (0.0, 60.0, 1)],
    ),
    (
        "single_day_refuses",
        "Two events on one calendar day: no gap to measure, the tier must refuse.",
        [(2.6, 50.0, 1), (2.5, 100.0, 1)],
    ),
    (
        "single_record_refuses",
        "One event alone can never yield a rate.",
        [(3.0, 100.0, 1)],
    ),
    (
        "interleaved_spools_one_sku",
        "Two spools of one SKU interleaved — same-day events sum across spools before bucketing.",
        [(4.0, 20.0, 1), (3.9, 30.0, 2), (3.0, 10.0, 2), (1.0, 45.0, 1)],
    ),
    (
        "half_life_pair_weight_at_thirty_days",
        "One observation exactly 30.0 days old (weight 1/2) against one 0 days old (weight 1) — "
        "the frozen mean pins the 30-day half-life without reaching into internals.",
        [(31.0, 999.0, 1), (30.0, 10.0, 1), (0.0, 600.0, 1)],
    ),
    (
        "zero_gram_events_give_a_zero_rate",
        "Zero-weight events on two days: a measured rate of 0.0 with 0.0 spread — NOT a refusal.",
        [(3.0, 0.0, 1), (1.0, 0.0, 1)],
    ),
]

# ── Delta-tier inputs ────────────────────────────────────────────────────────
# Each case: (name, comment, [(weight_used, weight_used_baseline, created_days_ago)]).

_DELTA_CASES: list[tuple[str, str, list[tuple[float, float, float]]]] = [
    (
        "one_spool_ten_days",
        "100 g over 10 days — the plain fallback.",
        [(100.0, 0.0, 10.0)],
    ),
    (
        "baseline_aware_reset",
        "'Reset usage to 0': only post-reset grams count (900 used, 800 baseline over 10 days).",
        [(900.0, 800.0, 10.0)],
    ),
    (
        "oldest_spool_anchors_the_window",
        "Two spools: the OLDEST created_at divides the group's total.",
        [(60.0, 0.0, 20.0), (40.0, 0.0, 10.0)],
    ),
    (
        "zero_consumption_refuses",
        "Nothing consumed — no rate.",
        [(0.0, 0.0, 10.0)],
    ),
    (
        "younger_than_a_day_refuses",
        "A group younger than one day has no measurable window.",
        [(50.0, 0.0, 0.125)],
    ),
    (
        "over_reset_clamps_to_zero_and_refuses",
        "baseline above weight_used clamps to 0 consumed, so alone it refuses.",
        [(50.0, 100.0, 10.0)],
    ),
    (
        "over_reset_spool_does_not_poison_the_group",
        "An over-reset spool contributes 0, not a negative — the healthy spool's grams survive intact.",
        [(100.0, 0.0, 10.0), (50.0, 100.0, 5.0)],
    ),
]


def build_vectors() -> dict:
    """Run the shipped functions over every case and return the vector document."""
    history = []
    for name, comment, events in _HISTORY_CASES:
        records = [
            _Record(spool_id=sid, weight_used=grams, created_at=_at(days_ago)) for days_ago, grams, sid in events
        ]
        estimate = history_rate(records, NOW)
        history.append(
            {
                "name": name,
                "comment": comment,
                "records": [
                    {"spool_id": r.spool_id, "created_at": r.created_at.isoformat(), "weight_used": r.weight_used}
                    for r in records
                ],
                "expected": None if estimate is None else {"rate": estimate.rate, "std_dev": estimate.std_dev},
            }
        )

    delta = []
    for name, comment, spools in _DELTA_CASES:
        stand_ins = [
            _Spool(weight_used=used, weight_used_baseline=baseline, created_at=_at(days_ago))
            for used, baseline, days_ago in spools
        ]
        rate = delta_rate(stand_ins, NOW)
        delta.append(
            {
                "name": name,
                "comment": comment,
                "spools": [
                    {
                        "weight_used": s.weight_used,
                        "weight_used_baseline": s.weight_used_baseline,
                        "created_at": s.created_at.isoformat(),
                    }
                    for s in stand_ins
                ],
                "expected_rate": rate,
            }
        )

    return {
        "_provenance": (
            "Generated by backend/tests/forecast_vectors/generate_vectors.py against the SHIPPED "
            "backend.app.services.stock_forecast_alerts.{history_rate,delta_rate}. Do not edit by hand; "
            "regenerate only when the shipped math itself is the thing that deliberately changed: "
            "python -m backend.tests.forecast_vectors.generate_vectors"
        ),
        "now": NOW.isoformat(),
        "history": history,
        "delta": delta,
    }


def main() -> None:
    vectors = build_vectors()
    VECTORS_PATH.write_text(json.dumps(vectors, indent=2) + "\n", encoding="utf-8")
    measured_h = sum(1 for v in vectors["history"] if v["expected"] is not None)
    measured_d = sum(1 for v in vectors["delta"] if v["expected_rate"] is not None)
    print(
        f"Wrote {VECTORS_PATH.name}: {len(vectors['history'])} history vectors "
        f"({measured_h} measured, {len(vectors['history']) - measured_h} refusals), "
        f"{len(vectors['delta'])} delta vectors "
        f"({measured_d} measured, {len(vectors['delta']) - measured_d} refusals)."
    )


if __name__ == "__main__":
    main()
