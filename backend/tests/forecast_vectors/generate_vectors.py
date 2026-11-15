"""Freeze golden rate vectors by EXECUTING the implementation.

Task 1 of the forecast-server-side plan: before any refactor, the then-shipped
``backend.app.services.stock_forecast_alerts.history_rate`` / ``delta_rate``
(themselves a deliberate line-by-line port of ``ForecastPanel.tsx``) were run
on crafted inputs and their outputs frozen to ``rate_vectors.json``. The
``forecast_engine`` must reproduce these numbers exactly — the frozen JSON is
the arbiter; if a vector fails, the ENGINE is wrong, never the vector.

Task 2 repoint (per the T1 review, Minor 2): the refactor DELETED those shipped
bodies — the math's one owner is now ``forecast_engine``, whose helpers take
the SQL layer's aggregates (day buckets / clamped totals) instead of ORM rows.
This script now runs the engine over the same recorded inputs, translating them
exactly the way the SQL layer would (UTC-day bucketing; per-spool baseline
clamp + oldest ``created_at``). The repoint was verified to reproduce every
frozen vector exactly before landing (only the ``_provenance`` note moved on);
the committed JSON is IMMUTABLE — it is the sole remaining record of the
pre-refactor implementation's arithmetic.

Nothing in here derives an expected value by hand. The inputs are designed, the
outputs are measured.

Running the script VERIFIES by default (T2 review, Important 1 — the silent
regeneration habit-path is closed): it recomputes every vector through the
engine and diffs against the committed JSON, exiting non-zero on divergence.
The frozen JSON is the truth and the engine is wrong — never the vector.
Overwriting the file requires the explicit ``--write`` flag, whose help text
says why that is almost never the right move:

    python -m backend.tests.forecast_vectors.generate_vectors           # verify (exit 0/1)
    python -m backend.tests.forecast_vectors.generate_vectors --write   # retire the old truth

The JSON is committed together with this script so every expected number has an
executable provenance.

Delta-tier change of record (2026-11-15, upstream 1d86cff5a / #3407): the fallback
rate stopped being "every gram ever consumed / days since the oldest spool row was
created". Each spool's consumption is now spread from its first use to its last
and counted over the 90-day window (``forecast_engine.delta_rate`` over
``usage_span``). The seven delta vectors frozen in Task 1 describe spools in use
from the day they were entered until now, where the two formulas agree — their
inputs and expected rates were regenerated UNCHANGED, byte for byte — and six
vectors were added for the cases the old formula got wrong (shelf time before
first use, use that ended before the window, a span across the window's edge, a
spool used up in a day, a SKU in use for longer than the window, last use in the
future). ``--write`` was used once, for that.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from backend.app.services.forecast_engine import delta_rate, history_rate, usage_span

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
    """Stand-in for a Spool row — the delta tier reads exactly these attributes
    (``first_use`` is the spool's earliest usage record, which the SQL layer
    looks up beside the row)."""

    weight_used: float
    weight_used_baseline: float
    created_at: datetime
    first_use: datetime | None = None
    last_used: datetime | None = None
    archived_at: datetime | None = None


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
# Each case: (name, comment, [spool]) where a spool is
# (weight_used, weight_used_baseline, created_days_ago) and, optionally,
# (..., first_use_days_ago, last_used_days_ago, archived_days_ago) — None for a
# stamp the spool does not carry.

_DELTA_CASES: list[tuple[str, str, list[tuple]]] = [
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
    # ── Added with the span formula (upstream 1d86cff5a, #3407) ──
    (
        "shelf_time_before_first_use_is_not_use",
        "Entered 150 days ago, first printed with 10 days ago: 100 g over 10 days, not over 150.",
        [(100.0, 0.0, 150.0, 10.0, None, None)],
    ),
    (
        "use_that_ended_before_the_window_measures_zero",
        "600 g used between 400 and 300 days ago and nothing since: nothing was used in the last 90 days.",
        [(600.0, 0.0, 400.0, None, 300.0, None)],
    ),
    (
        "a_span_across_the_window_edge_counts_its_inside_part",
        "900 g spread evenly from day -135 to day -45: half lies inside the 90-day window, 450 g / 90 d.",
        [(900.0, 0.0, 135.0, None, 45.0, None)],
    ),
    (
        "a_spool_used_up_in_one_day_counts_whole_at_that_day",
        "200 g first and last used within two hours, 5 days ago: all of it, observed for 5 days.",
        [(200.0, 0.0, 30.0, 5.0, 5.0 - 2 / 24, None)],
    ),
    (
        "a_spool_used_up_before_the_window_keeps_the_window_whole",
        "One spool emptied 200 days ago, another opened 10 days ago: the SKU was in use all along, "
        "so the new spool's 100 g is 100 g per 90 days — not per 10.",
        [(1000.0, 0.0, 300.0, None, 200.0, 200.0), (100.0, 0.0, 30.0, 10.0, None, None)],
    ),
    (
        "a_last_use_in_the_future_ends_now",
        "A clock that ran ahead stamped last use tomorrow: the span ends now, 100 g over 10 days.",
        [(100.0, 0.0, 10.0, None, -1.0, None)],
    ),
]


def _day_buckets(records: list[_Record]) -> list[tuple[date, float]]:
    """Bucket records by UTC calendar day, exactly as the engine's SQL layer
    does — the same translation the pre-refactor ``history_rate`` performed
    internally, so the frozen expectations carry over unchanged."""
    by_day: dict[date, float] = {}
    for r in records:
        day = r.created_at.date()
        by_day[day] = by_day.get(day, 0.0) + r.weight_used
    return sorted(by_day.items())


def build_vectors() -> dict:
    """Run the engine's rate functions over every case and return the vector
    document (byte-identical to the Task 1 freeze of the pre-refactor code)."""
    history = []
    for name, comment, events in _HISTORY_CASES:
        records = [
            _Record(spool_id=sid, weight_used=grams, created_at=_at(days_ago)) for days_ago, grams, sid in events
        ]
        estimate = history_rate(_day_buckets(records), NOW)
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
        stand_ins = []
        for used, baseline, days_ago, *stamps in spools:
            first_use, last_used, archived = (*stamps, None, None, None)[:3]
            stand_ins.append(
                _Spool(
                    weight_used=used,
                    weight_used_baseline=baseline,
                    created_at=_at(days_ago),
                    first_use=None if first_use is None else _at(first_use),
                    last_used=None if last_used is None else _at(last_used),
                    archived_at=None if archived is None else _at(archived),
                )
            )
        # What the engine's SQL layer delivers: one span per spool that consumed
        # anything, the baseline clamp PER SPOOL (never sum-then-clamp).
        spans = [
            span
            for s in stand_ins
            if (
                span := usage_span(
                    s.weight_used - s.weight_used_baseline,
                    first_use=s.first_use,
                    created_at=s.created_at,
                    last_used=s.last_used,
                    archived_at=s.archived_at,
                    now=NOW,
                )
            )
            is not None
        ]
        rate = delta_rate(spans, NOW)
        delta.append(
            {
                "name": name,
                "comment": comment,
                "spools": [
                    {
                        "weight_used": s.weight_used,
                        "weight_used_baseline": s.weight_used_baseline,
                        "created_at": s.created_at.isoformat(),
                        # Only the stamps a spool carries: the Task 1 vectors
                        # stay byte-identical.
                        **{
                            key: value.isoformat()
                            for key, value in (
                                ("first_use", s.first_use),
                                ("last_used", s.last_used),
                                ("archived_at", s.archived_at),
                            )
                            if value is not None
                        },
                    }
                    for s in stand_ins
                ],
                "expected_rate": rate,
            }
        )

    return {
        "_provenance": (
            "History vectors and the first seven delta vectors: frozen in Task 1 by executing the "
            "then-shipped backend.app.services.stock_forecast_alerts.{history_rate,delta_rate}, and "
            "reproduced unchanged by backend.app.services.forecast_engine ever since. The delta vectors "
            "after them were measured on the engine when its delta tier became the span formula "
            "(2026-11-15, upstream 1d86cff5a / #3407). Do not edit by hand; regenerate only when the "
            "engine's math is the thing that deliberately changed: "
            "python -m backend.tests.forecast_vectors.generate_vectors"
        ),
        "now": NOW.isoformat(),
        "history": history,
        "delta": delta,
    }


def main(argv: list[str] | None = None) -> int:
    """Verify by default; overwrite only under the explicit ``--write`` flag.

    The default mode is the tripwire the T1 content-comparison test used to be:
    a habitual run can no longer replace the frozen truth with engine output —
    it can only report whether the engine still reproduces it.
    """
    parser = argparse.ArgumentParser(
        prog="python -m backend.tests.forecast_vectors.generate_vectors",
        description=(
            "Verify (default) that the forecast engine reproduces the frozen golden vectors in "
            "rate_vectors.json, or rewrite them with --write."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "OVERWRITE rate_vectors.json with the engine's output. The committed JSON's provenance is the "
            "SHIPPED pre-refactor implementation (Task 1, frozen by execution); regenerating from the engine "
            "replaces that truth with the thing under test. Legitimate ONLY when the engine's math has "
            "deliberately changed and the old vectors' authority is being retired — never to make a red "
            "verification green."
        ),
    )
    args = parser.parse_args(argv)

    vectors = build_vectors()

    if args.write:
        VECTORS_PATH.write_text(json.dumps(vectors, indent=2) + "\n", encoding="utf-8")
        measured_h = sum(1 for v in vectors["history"] if v["expected"] is not None)
        measured_d = sum(1 for v in vectors["delta"] if v["expected_rate"] is not None)
        print(
            f"Wrote {VECTORS_PATH.name}: {len(vectors['history'])} history vectors "
            f"({measured_h} measured, {len(vectors['history']) - measured_h} refusals), "
            f"{len(vectors['delta'])} delta vectors "
            f"({measured_d} measured, {len(vectors['delta']) - measured_d} refusals)."
        )
        return 0

    # ``_provenance`` stays outside the diff on purpose: the committed note
    # records the Task 1 freeze; the in-memory one describes a hypothetical
    # rewrite. The vectors themselves are the truth being verified.
    frozen = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    regenerated = json.loads(json.dumps(vectors))
    mismatched = [section for section in ("now", "history", "delta") if regenerated[section] != frozen[section]]
    if mismatched:
        print(
            f"MISMATCH in {', '.join(mismatched)}: the engine no longer reproduces the frozen vectors.\n"
            f"{VECTORS_PATH.name} is the truth (frozen from the SHIPPED pre-refactor implementation) and the\n"
            "engine is wrong — fix the engine, never the vector. --write exists only for a deliberate math\n"
            "change that retires the old vectors' authority.",
            file=sys.stderr,
        )
        return 1
    print(f"verified: the engine reproduces every frozen vector exactly (now/history/delta match {VECTORS_PATH.name}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
