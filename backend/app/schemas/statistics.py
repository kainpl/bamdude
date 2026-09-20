"""Response shape for ``GET /archives/aggregate``.

Sized by the date range rather than by the number of prints: a two-year «all
time» on a 48-printer farm is about a thousand rows here, against ten thousand
truncated archive rows before.
"""

from pydantic import BaseModel


class BucketMetrics(BaseModel):
    prints: int = 0
    completed: int = 0
    failed: int = 0
    grams: float = 0.0
    cost: float = 0.0
    energy_cost: float = 0.0
    quantity: int = 0
    seconds: float = 0.0


class Bucket(BaseModel):
    """One local day (``2026-09-08``) or hour (``2026-09-08T14``).

    Two metric groups because the page has two axes: ``started`` is keyed on
    ``created_at`` (the activity calendar, the heat-map, the weekday habits) and
    ``ended`` on ``COALESCE(completed_at, created_at)`` (the archive calendar,
    the filament timeline, the records).
    """

    at: str
    started: BucketMetrics
    ended: BucketMetrics


class HourCell(BaseModel):
    hour: int
    prints: int = 0
    failures: int = 0


class PrinterRow(BaseModel):
    printer_id: int | None = None
    prints: int = 0
    grams: float = 0.0
    seconds: float = 0.0
    completed: int = 0
    failed: int = 0


class MaterialRow(BaseModel):
    material: str
    prints: int = 0
    grams: float = 0.0
    seconds: float = 0.0
    completed: int = 0
    failed: int = 0


class ColorRow(BaseModel):
    color: str
    prints: int = 0
    grams: float = 0.0


class DurationRow(BaseModel):
    bucket: str
    prints: int = 0


class Totals(BaseModel):
    prints: int = 0
    completed: int = 0
    failed: int = 0
    grams: float = 0.0
    cost: float = 0.0
    energy_kwh: float = 0.0
    energy_cost: float = 0.0
    quantity: int = 0
    seconds: float = 0.0
    printers: int = 0


class LongestRecord(BaseModel):
    archive_id: int
    print_name: str | None = None
    seconds: float = 0.0


class HeaviestRecord(BaseModel):
    archive_id: int
    print_name: str | None = None
    grams: float = 0.0


class CostliestRecord(BaseModel):
    """Filament plus measured electricity, with the split kept.

    A print with no plug data contributes 0 energy and competes on filament
    alone — that is the honest comparison, and inventing a figure would put a
    guess on the podium.
    """

    archive_id: int
    print_name: str | None = None
    total: float = 0.0
    cost: float = 0.0
    energy_cost: float = 0.0


class Records(BaseModel):
    longest: LongestRecord | None = None
    heaviest: HeaviestRecord | None = None
    costliest: CostliestRecord | None = None
    success_streak: int = 0


class ArchiveAggregate(BaseModel):
    timezone: str
    granularity: str
    buckets: list[Bucket]
    by_hour_of_day: list[HourCell]
    by_printer: list[PrinterRow]
    by_material: list[MaterialRow]
    by_color: list[ColorRow]
    by_duration: list[DurationRow]
    totals: Totals
    records: Records


# ── The overview (KPI block) - moved from schemas/archive.py on 2026-09-17 ──


class DefectsByPrinter(BaseModel):
    """What came off one printer's plates in the period, and how much of it went in the bin."""

    printed: int
    defective: int


class ArchiveStats(BaseModel):
    total_prints: int
    successful_prints: int
    failed_prints: int
    # User/system-stopped prints (status in stopped/cancelled/skipped).
    # Defaulted so older clients that don't send this field still validate.
    cancelled_prints: int = 0
    total_print_time_hours: float
    total_filament_grams: float
    total_cost: float
    prints_by_filament_type: dict
    prints_by_printer: dict
    # Time accuracy stats
    # Average across all prints with data
    average_time_accuracy: float | None = None
    time_accuracy_by_printer: dict | None = None  # Per-printer accuracy
    # Completed prints only, keyed by printer id as a string like the other
    # per-printer maps; printers with nothing printed in the period are omitted.
    defects_by_printer: dict[str, DefectsByPrinter] = {}
    # ── Energy, answered twice ───────────────────────────────────────────
    # These used to be one pair whose meaning depended on a setting, so the
    # number on the page could not be read without opening Settings to find out
    # which question it had answered. Both are returned now and the page shows
    # both; the setting is gone.
    #
    # ⚠️ They are not two views of one figure. ``print_*`` is measured between
    # the start and end of each print and is therefore bounded by the date
    # filter like every other statistic here. ``total_*`` is what the plugs
    # themselves counted — idle, warm-up, and anything else sharing the socket
    # — and all-time it is read from their live lifetime counters, which no
    # date filter can reach. The gap between the two is the cost of standing
    # still, which is the reason anyone wants both.
    print_energy_kwh: float = 0.0
    print_energy_cost: float = 0.0
    total_energy_kwh: float = 0.0
    total_energy_cost: float = 0.0
    # Set when the date-range query in "total consumption" mode is running on
    # incomplete snapshot history - e.g. right after a fresh upgrade before the
    # hourly snapshot loop has built up a baseline. Frontend shows a tooltip.
    energy_data_warming_up: bool = False
