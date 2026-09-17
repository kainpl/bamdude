"""Server-side aggregation of the print archive.

Replaces shipping every archive row to the browser so it can fold them there.
That was not only a payload problem: the endpoint it replaces returns at most
10 000 rows ordered newest-first, with no total and no flag, so on a busy farm
«all time» quietly meant «the last three weeks» and the calendar quietly lost
the start of its own month.

The unit of grouping in SQL is a 15-minute bucket index — ``epoch // 900`` —
which both dialects can compute. Everything local happens here, in ``zoneinfo``:
DST is then the library's problem rather than a fixed offset that is wrong twice
a year, and the two backends cannot disagree. Fifteen minutes is fine enough
that a zone offset of :30 or :45 still puts a whole bucket inside one local day
and one local hour.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, case, cast, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.db_dialect import is_postgres
from backend.app.core.timezones import day_bounds
from backend.app.models.archive import PrintArchive

BUCKET_SECONDS = 900
HOURLY_MAX_DAYS = 7

# Mirrors the frontend's own boundaries, in order. The last one is open-ended.
DURATION_BUCKETS: tuple[tuple[str, float], ...] = (
    ("<30m", 1800),
    ("30m-1h", 3600),
    ("1-2h", 7200),
    ("2-4h", 14400),
    ("4-8h", 28800),
    ("8-12h", 43200),
    ("12-24h", 86400),
    ("24h+", float("inf")),
)

FAILURE_STATUSES = ("failed", "aborted", "cancelled")
TERMINAL_STATUSES = ("completed", *FAILURE_STATUSES)

Granularity = Literal["day", "hour"]

_DAY_FORMAT = "%Y-%m-%d"
_HOUR_FORMAT = "%Y-%m-%dT%H"


def bucket_expr(column):
    """The 15-minute bucket index of a naive-UTC timestamp column.

    Both branches read the stored value as UTC, which is how it is stored on
    both backends — on PostgreSQL the column is ``TIMESTAMP WITHOUT TIME ZONE``
    and ``_strip_tz_from_params`` guarantees the values that reach it are UTC.
    """
    if is_postgres():
        return cast(func.floor(extract("epoch", column) / BUCKET_SECONDS), Integer)
    return cast(func.strftime("%s", column), Integer) / BUCKET_SECONDS


def granularity_for(date_from: date | None, date_to: date | None) -> Granularity:
    """Hourly buckets only for a bounded range of at most a week.

    That is exactly the condition under which the Stats page switches to its
    hourly heat-map; everything coarser is folded in the browser.
    """
    if date_from and date_to and (date_to - date_from).days + 1 <= HOURLY_MAX_DAYS:
        return "hour"
    return "day"


def _format_for(granularity: Granularity) -> str:
    return _DAY_FORMAT if granularity == "day" else _HOUR_FORMAT


def local_key(index: int, tz: ZoneInfo, granularity: Granularity) -> str:
    """Fold one bucket index into its local day (or hour)."""
    moment = datetime.fromtimestamp(index * BUCKET_SECONDS, tz=timezone.utc).astimezone(tz)
    return moment.strftime(_format_for(granularity))


def local_hour_of_day(index: int, tz: ZoneInfo) -> int:
    return datetime.fromtimestamp(index * BUCKET_SECONDS, tz=timezone.utc).astimezone(tz).hour


def fill_gaps(keys: Iterable[str], granularity: Granularity) -> list[str]:
    """Every key from the earliest present to the latest, gaps included.

    A day with no prints is a real answer and the chart needs it; doing this
    here means no consumer has to reinvent a calendar walk.
    """
    present = sorted(keys)
    if not present:
        return []
    fmt = _format_for(granularity)
    step = timedelta(days=1) if granularity == "day" else timedelta(hours=1)
    current = datetime.strptime(present[0], fmt)
    last = datetime.strptime(present[-1], fmt)
    out: list[str] = []
    while current <= last:
        out.append(current.strftime(fmt))
        current += step
    return out


def duration_bucket(seconds: float | None) -> str | None:
    """The frontend's bucketing rule: no positive duration means no bucket."""
    if not seconds or seconds <= 0:
        return None
    for name, upper in DURATION_BUCKETS:
        if seconds <= upper:
            return name
    return DURATION_BUCKETS[-1][0]


def split_multi(
    rows: list[dict[str, Any]],
    *,
    sep: str,
    default: str | None,
    divide: tuple[str, ...],
    whole: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Expand rows grouped by a comma-joined string into one row per part.

    ``filament_type`` and ``filament_color`` hold several values in one string,
    so SQL can only group the combination. Expanding here reproduces what the
    browser has always done — and the rule is asymmetric on purpose: measures
    (grams, seconds) are **divided evenly** among the parts, while counts are
    credited **whole** to each part.

    Grouping by the raw string first is exact under that rule, because Σ(gᵢ/k)
    over rows sharing a string is (Σgᵢ)/k.

    ``default`` is what an empty key becomes — ``"Unknown"`` for materials.
    ``None`` drops the row instead, which is what colours do: there is no
    "unknown colour" bucket on that chart.
    """
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = (row.get("key") or "").strip()
        if raw:
            parts = [p.strip() for p in raw.split(sep) if p.strip()]
        else:
            parts = []
        if not parts:
            if default is None:
                continue
            parts = [default]
        for part in parts:
            target = out.setdefault(
                part,
                {"key": part, **dict.fromkeys(whole, 0), **dict.fromkeys(divide, 0.0)},
            )
            for field in divide:
                target[field] += (row.get(field) or 0) / len(parts)
            for field in whole:
                target[field] += row.get(field) or 0
    return list(out.values())


# --- the query half -----------------------------------------------------------
#
# Every statement below is a columnar select over the same filtered set. The
# range filter rides ix_print_archives_active_created (deleted_at, created_at)
# from m168; the bucket expression itself is not indexable, so the grouping is a
# hash on PostgreSQL and a temp b-tree on SQLite over the *filtered* rows. That
# is the intended shape, and it is why the indexes came first.


def _base_filters(tz: ZoneInfo, date_from: date | None, date_to: date | None, user_id: int | None) -> list:
    """The one filter set, spelled once.

    ⚠️ The range is applied to ``created_at`` on BOTH axes, including the
    completed one. That is deliberate: the endpoint this replaces filtered on
    ``created_at`` and let the browser bucket by ``completed_at``, so a print
    created on the last day of the range and finished the next morning was
    counted. Filtering the ended axis by its own column would quietly change
    numbers that are supposed to stay identical.
    """
    filters = [
        PrintArchive.status != "archived",
        PrintArchive.deleted_at.is_(None),
    ]
    if user_id is not None:
        filters.append(PrintArchive.created_by_id == user_id)
    if date_from:
        filters.append(PrintArchive.created_at >= day_bounds(date_from, tz)[0])
    if date_to:
        filters.append(PrintArchive.created_at < day_bounds(date_to, tz)[1])
    return filters


_ENDED = func.coalesce(PrintArchive.completed_at, PrintArchive.created_at)
# ⚠️ NULLIF, not COALESCE alone: the frontend reads
# ``actual_time_seconds || print_time_seconds``, and in JavaScript a stored 0 is
# falsy and falls through to the estimate. Plain COALESCE would keep the 0.
_SECONDS = func.coalesce(func.nullif(PrintArchive.actual_time_seconds, 0), PrintArchive.print_time_seconds, 0)
_IS_DONE = case((PrintArchive.status == "completed", 1), else_=0)
_IS_FAILED = case((PrintArchive.status.in_(FAILURE_STATUSES), 1), else_=0)

_METRIC_COLUMNS = (
    func.count().label("prints"),
    func.sum(_IS_DONE).label("completed"),
    func.sum(_IS_FAILED).label("failed"),
    func.sum(func.coalesce(PrintArchive.filament_used_grams, 0.0)).label("grams"),
    func.sum(func.coalesce(PrintArchive.cost, 0.0)).label("cost"),
    func.sum(func.coalesce(PrintArchive.energy_cost, 0.0)).label("energy_cost"),
    func.sum(func.coalesce(PrintArchive.quantity, 0)).label("quantity"),
    func.sum(_SECONDS).label("seconds"),
)
_METRIC_NAMES = ("prints", "completed", "failed", "grams", "cost", "energy_cost", "quantity", "seconds")


def _empty_metrics() -> dict[str, float]:
    return {name: 0 if name in ("prints", "completed", "failed", "quantity") else 0.0 for name in _METRIC_NAMES}


def _metrics_of(row) -> dict[str, float]:
    values = dict(zip(_METRIC_NAMES, row[1:], strict=True))
    return {k: (v or 0) for k, v in values.items()}


async def _bucket_metrics(db, filters: list, column, tz: ZoneInfo, granularity: Granularity) -> dict[str, dict]:
    """One axis, folded from bucket indexes to local keys."""
    bucket = bucket_expr(column).label("bucket")
    rows = (await db.execute(select(bucket, *_METRIC_COLUMNS).where(*filters).group_by(bucket))).all()
    folded: dict[str, dict] = {}
    for row in rows:
        key = local_key(int(row[0]), tz, granularity)
        target = folded.setdefault(key, _empty_metrics())
        for name, value in _metrics_of(row).items():
            target[name] += value
    return folded


async def _hour_of_day(db, filters: list, tz: ZoneInfo) -> list[dict]:
    """The 24-cell time-of-day chart, on the ``started_at`` axis.

    A row that never started is skipped, exactly as the browser skipped it.
    Always 24 cells, zeros included — an hour with no prints is an answer.
    """
    bucket = bucket_expr(PrintArchive.started_at).label("bucket")
    rows = (
        await db.execute(
            select(bucket, func.count().label("prints"), func.sum(_IS_FAILED).label("failures"))
            .where(*filters, PrintArchive.started_at.is_not(None))
            .group_by(bucket)
        )
    ).all()
    cells = [{"hour": h, "prints": 0, "failures": 0} for h in range(24)]
    for row in rows:
        cell = cells[local_hour_of_day(int(row[0]), tz)]
        cell["prints"] += row[1] or 0
        cell["failures"] += row[2] or 0
    return cells


async def _by_printer(db, filters: list) -> list[dict]:
    rows = (
        await db.execute(
            select(
                PrintArchive.printer_id,
                func.count().label("prints"),
                func.sum(func.coalesce(PrintArchive.filament_used_grams, 0.0)).label("grams"),
                func.sum(_SECONDS).label("seconds"),
                func.sum(_IS_DONE).label("completed"),
                func.sum(_IS_FAILED).label("failed"),
            )
            .where(*filters)
            .group_by(PrintArchive.printer_id)
        )
    ).all()
    return sorted(
        (
            {
                "printer_id": row[0],
                "prints": row[1] or 0,
                "grams": float(row[2] or 0.0),
                "seconds": float(row[3] or 0.0),
                "completed": row[4] or 0,
                "failed": row[5] or 0,
            }
            for row in rows
        ),
        key=lambda r: r["prints"],
        reverse=True,
    )


async def _grouped_by_text(db, filters: list, column, *, with_seconds: bool) -> list[dict]:
    """Group by a comma-joined text column. Splitting happens in Python."""
    columns = [
        column.label("key"),
        func.count().label("prints"),
        func.sum(func.coalesce(PrintArchive.filament_used_grams, 0.0)).label("grams"),
    ]
    if with_seconds:
        columns += [
            func.sum(_SECONDS).label("seconds"),
            func.sum(_IS_DONE).label("completed"),
            func.sum(_IS_FAILED).label("failed"),
        ]
    rows = (await db.execute(select(*columns).where(*filters).group_by(column))).all()
    keys = ("key", "prints", "grams", "seconds", "completed", "failed") if with_seconds else ("key", "prints", "grams")
    return [dict(zip(keys, row, strict=True)) for row in rows]


async def _by_duration(db, filters: list) -> list[dict]:
    """Bucketed in SQL — the boundaries need the per-archive duration, so this
    cannot stay in the browser once the rows do not travel."""
    whens = [(upper >= _SECONDS, name) for name, upper in DURATION_BUCKETS[:-1]]
    labelled = case(*whens, else_=DURATION_BUCKETS[-1][0]).label("bucket")
    rows = (
        await db.execute(
            select(labelled, func.count().label("prints")).where(*filters, _SECONDS > 0).group_by(labelled)
        )
    ).all()
    counts = {row[0]: row[1] or 0 for row in rows}
    return [{"bucket": name, "prints": counts.get(name, 0)} for name, _ in DURATION_BUCKETS]


async def _totals(db, filters: list) -> dict:
    row = (
        await db.execute(
            select(
                *_METRIC_COLUMNS,
                func.sum(func.coalesce(PrintArchive.energy_kwh, 0.0)).label("energy_kwh"),
                func.count(func.distinct(PrintArchive.printer_id)).label("printers"),
            ).where(*filters)
        )
    ).one()
    values = dict(zip((*_METRIC_NAMES, "energy_kwh", "printers"), row, strict=True))
    return {k: (v or 0) for k, v in values.items()}


async def _one_record(db, filters: list, measure, name: str, *, extra: dict | None = None) -> dict | None:
    """The single best COMPLETED print by one measure.

    ⚠️ Completed only, and that is not a detail: a record is a claim about
    output. A run that was cancelled after twenty hours is not «the longest
    print», and a failure that burned 900 g is not «the heaviest». The page has
    always ranked this way.
    """
    columns = [PrintArchive.id, PrintArchive.print_name, measure.label("value")]
    extra_names = tuple(extra or ())
    columns += [(extra or {})[k].label(k) for k in extra_names]
    row = (
        await db.execute(
            select(*columns)
            .where(*filters, PrintArchive.status == "completed", measure > 0)
            .order_by(measure.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    record = {"archive_id": row[0], "print_name": row[1], name: float(row[2] or 0.0)}
    for index, key in enumerate(extra_names, start=3):
        record[key] = float(row[index] or 0.0)
    return record


async def _success_streak(db, filters: list) -> int:
    """Longest run of consecutive completed prints among the terminal ones.

    Gaps-and-islands: two row numbers over the same order, one of them
    partitioned by the flag, differ by a constant inside a run. Both dialects
    have window functions (SQLite since 3.25; Python 3.12 ships far newer).
    """
    ordered = (
        select(
            _IS_DONE.label("done"),
            # ⚠️ id is the tiebreak, not decoration: two prints that end on the
            # same timestamp otherwise order arbitrarily, and the run length
            # then depends on which the planner happened to emit first — the
            # two dialects disagreed on exactly that in a seeded check.
            func.row_number().over(order_by=(_ENDED, PrintArchive.id)).label("rn_all"),
            func.row_number().over(partition_by=_IS_DONE, order_by=(_ENDED, PrintArchive.id)).label("rn_grp"),
        )
        .where(*filters, PrintArchive.status.in_(TERMINAL_STATUSES))
        .subquery()
    )
    islands = (
        select(func.count().label("run"))
        .select_from(ordered)
        .where(ordered.c.done == 1)
        .group_by(ordered.c.rn_all - ordered.c.rn_grp)
        .subquery()
    )
    return int((await db.execute(select(func.coalesce(func.max(islands.c.run), 0)))).scalar_one() or 0)


async def collect(
    db: AsyncSession,
    *,
    tz: ZoneInfo,
    date_from: date | None,
    date_to: date | None,
    user_id: int | None,
) -> dict[str, Any]:
    """Everything the Stats page and the archive calendar fold, folded here."""
    granularity = granularity_for(date_from, date_to)
    filters = _base_filters(tz, date_from, date_to, user_id)

    started = await _bucket_metrics(db, filters, PrintArchive.created_at, tz, granularity)
    ended = await _bucket_metrics(db, filters, _ENDED, tz, granularity)
    buckets = [
        {
            "at": key,
            "started": started.get(key) or _empty_metrics(),
            "ended": ended.get(key) or _empty_metrics(),
        }
        for key in fill_gaps(set(started) | set(ended), granularity)
    ]

    materials = split_multi(
        await _grouped_by_text(db, filters, PrintArchive.filament_type, with_seconds=True),
        sep=", ",
        default="Unknown",
        divide=("grams", "seconds"),
        whole=("prints", "completed", "failed"),
    )
    colours = split_multi(
        await _grouped_by_text(db, filters, PrintArchive.filament_color, with_seconds=False),
        sep=",",
        default=None,
        divide=("grams",),
        whole=("prints",),
    )

    return {
        "timezone": str(tz),
        "granularity": granularity,
        "buckets": buckets,
        "by_hour_of_day": await _hour_of_day(db, filters, tz),
        "by_printer": await _by_printer(db, filters),
        "by_material": sorted(
            ({"material": r.pop("key"), **r} for r in materials), key=lambda r: r["grams"], reverse=True
        ),
        "by_color": sorted(({"color": r.pop("key"), **r} for r in colours), key=lambda r: r["grams"], reverse=True),
        "by_duration": await _by_duration(db, filters),
        "totals": await _totals(db, filters),
        "records": {
            "longest": await _one_record(db, filters, _SECONDS, "seconds"),
            "heaviest": await _one_record(db, filters, func.coalesce(PrintArchive.filament_used_grams, 0.0), "grams"),
            # Filament AND measured electricity, with the split beside it: the
            # page shows «filament X + energy Y» whenever electricity actually
            # moved the number, so the winner can be reconciled against its own
            # archive page.
            "costliest": await _one_record(
                db,
                filters,
                func.coalesce(PrintArchive.cost, 0.0) + func.coalesce(PrintArchive.energy_cost, 0.0),
                "total",
                extra={
                    "cost": func.coalesce(PrintArchive.cost, 0.0),
                    "energy_cost": func.coalesce(PrintArchive.energy_cost, 0.0),
                },
            ),
            "success_streak": await _success_streak(db, filters),
        },
    }
