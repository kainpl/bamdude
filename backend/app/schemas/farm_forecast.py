"""Wire shapes of the farm forecast (spec 2026-09-06, Slice B)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel

from backend.app.schemas.print_queue import UTCDatetime

if TYPE_CHECKING:
    from backend.app.services.farm_forecast import FarmForecast


class PrinterForecastOut(BaseModel):
    printer_id: int
    free_at: UTCDatetime
    free_seconds: int
    unknown_prints: int = 0


class FarmForecastOut(BaseModel):
    free_at: UTCDatetime
    free_seconds: int
    #: Rows of the snapshot with no estimate — why «free at» can read zero
    #: while printers are busy (ruling 2026-09-07, final review I2).
    unknown_prints: int = 0
    #: One row per machine of the snapshot — what the «free at» sorts of the
    #: printers and queue pages order by. Same simulation, read per printer.
    printers: list[PrinterForecastOut] = []

    @classmethod
    def of(cls, now: datetime, farm: FarmForecast, printer_scope: frozenset[int] | None = None) -> FarmForecastOut:
        """The one place the simulation's seconds become wall-clock dates —
        both routes that answer a farm header go through it.

        ``printer_scope`` (N62): the farm is simulated whole — another team's
        queue still takes its printers — and the answer is read for the printers
        the caller reaches: their rows, their «free at» (the latest of them) and
        their unestimated prints."""
        machines = farm.printers
        free_seconds, unknown = farm.free_seconds, farm.unknown_prints
        if printer_scope is not None:
            machines = [p for p in farm.printers if p.printer_id in printer_scope]
            free_seconds = max((p.free_seconds for p in machines), default=0)
            unknown = sum(p.unknown_prints for p in machines)
        return cls(
            free_at=now + timedelta(seconds=free_seconds),
            free_seconds=free_seconds,
            unknown_prints=unknown,
            printers=[
                PrinterForecastOut(
                    printer_id=p.printer_id,
                    free_at=now + timedelta(seconds=p.free_seconds),
                    free_seconds=p.free_seconds,
                    unknown_prints=p.unknown_prints,
                )
                for p in machines
            ],
        )


class RowForecastOut(BaseModel):
    plate_id: int
    proposed_split: dict[int, int] | None = None


class LineForecastOut(BaseModel):
    line_id: int
    now_eta: UTCDatetime
    now_seconds: int | None
    after_eta: UTCDatetime
    after_seconds: int | None
    unknown_prints: int
    unroutable_prints: int
    eta_complete: bool
    rows: list[RowForecastOut] = []


class EstimateReasonOut(BaseModel):
    """Why an estimate is not whole (WS-13 E1 OR5 / ES3) — a code from a closed list
    and its count (``None`` where a count means nothing, e.g. ``truncated``)."""

    code: str
    count: int | None = None


class ModelHoursOut(BaseModel):
    """OR6 — one printer model's share of an order's plan."""

    model: str | None
    prints: int
    seconds: int | None
    accepting_printers: int


class OrderForecastOut(BaseModel):
    project_id: int
    now_eta: UTCDatetime
    now_seconds: int | None
    after_eta: UTCDatetime
    after_seconds: int | None
    machine_seconds: int | None
    unknown_prints: int
    unroutable_prints: int
    eta_complete: bool
    ahead_count: int
    assumptions: list[str]
    #: WS-13 E1 OR5 — the production estimate's completeness; empty = whole.
    incomplete_reasons: list[EstimateReasonOut] = []
    #: OR4 — the forecast lands after the deadline day (the /deadlines rule).
    late: bool = False


class OrderForecastDetailOut(OrderForecastOut):
    lines: list[LineForecastOut] = []
    #: OR6 — machine-hours per printer model of the plan.
    by_model: list[ModelHoursOut] = []


class ForecastBatchOut(BaseModel):
    farm: FarmForecastOut
    orders: list[OrderForecastOut]
