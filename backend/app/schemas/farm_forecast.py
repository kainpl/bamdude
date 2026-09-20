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
    def of(cls, now: datetime, farm: FarmForecast) -> FarmForecastOut:
        """The one place the simulation's seconds become wall-clock dates —
        both routes that answer a farm header go through it."""
        return cls(
            free_at=now + timedelta(seconds=farm.free_seconds),
            free_seconds=farm.free_seconds,
            unknown_prints=farm.unknown_prints,
            printers=[
                PrinterForecastOut(
                    printer_id=p.printer_id,
                    free_at=now + timedelta(seconds=p.free_seconds),
                    free_seconds=p.free_seconds,
                    unknown_prints=p.unknown_prints,
                )
                for p in farm.printers
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
    rows: list[RowForecastOut] = []


class OrderForecastOut(BaseModel):
    project_id: int
    now_eta: UTCDatetime
    now_seconds: int | None
    after_eta: UTCDatetime
    after_seconds: int | None
    machine_seconds: int | None
    unknown_prints: int
    unroutable_prints: int
    ahead_count: int
    assumptions: list[str]


class OrderForecastDetailOut(OrderForecastOut):
    lines: list[LineForecastOut] = []


class ForecastBatchOut(BaseModel):
    farm: FarmForecastOut
    orders: list[OrderForecastOut]
