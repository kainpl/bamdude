"""Smart-plug power alerts: the rule, and the sweep that applies it.

A plug may carry a high and a low watt limit. The alert is sent once when the
reading leaves the range and is armed again only after it has come back inside
-- not every few minutes while it stays out.

The sweep runs in the measurement-history loop, on what that loop just wrote:
the newest usable row of ``smart_plug_power_history``. That history is written
above the plug drivers, so every plug type is covered and nothing here reads a
device. It used to run inside the plug-status request, which made the alert
exist only while somebody had the page open.

Which side a plug is on is process memory. After a restart a plug still outside
its range is announced once more; a silent miss would be the worse failure.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select

from backend.app.i18n import current_language, t
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
from backend.app.services.measurement_buffer import measurement_buffer
from backend.app.services.meter_source import naive_utc
from backend.app.services.notification_service import notification_service

logger = logging.getLogger(__name__)

INSIDE = "inside"
HIGH = "high"
LOW = "low"

# A reading older than this says nothing about now. Wide enough for a plug that
# reports only on change (a Zigbee plug at a steady load reports every 15
# minutes at the latest), narrow enough that a plug gone quiet raises nothing.
FRESH = timedelta(minutes=20)

# The least time between two alerts of one plug, whatever it crossed.
COOLDOWN = timedelta(minutes=5)

# plug id -> the side its last ANNOUNCED reading was on.
_side: dict[int, str] = {}


def forget_all() -> None:
    _side.clear()


def forget(plug_id: int) -> None:
    _side.pop(plug_id, None)


def side_of(power: float, state: str | None, *, low: float | None, high: float | None) -> str:
    """Which side of its range a reading is on.

    Low is asked only of a plug known to be switched ON: a plug that is off
    draws nothing by design, and one whose switch state is unknown cannot be
    told apart from it.
    """
    if high is not None and power > high:
        return HIGH
    if low is not None and power < low and (state or "").upper() == "ON":
        return LOW
    return INSIDE


async def _newest_reading(db, plug_id: int, since) -> float | None:
    return (
        await db.execute(
            select(SmartPlugPowerHistory.power)
            .where(
                SmartPlugPowerHistory.plug_id == plug_id,
                SmartPlugPowerHistory.unusable_reason.is_(None),
                SmartPlugPowerHistory.recorded_at >= since,
            )
            .order_by(SmartPlugPowerHistory.recorded_at.desc(), SmartPlugPowerHistory.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def run_power_alerts(db) -> int:
    """One tick. Returns how many alerts were sent.

    Every plug is handled on its own and every send is wrapped: this runs in the
    loop that also writes and prunes measurement history, and an exception here
    would take those with it.
    """
    now = naive_utc()
    plugs = (
        (
            await db.execute(
                select(SmartPlug).where(SmartPlug.enabled.is_(True), SmartPlug.power_alert_enabled.is_(True))
            )
        )
        .scalars()
        .all()
    )
    sent = 0
    for plug in plugs:
        try:
            power = await _newest_reading(db, plug.id, now - FRESH)
            if power is None:
                continue
            state = measurement_buffer.switch_state(plug.id) or plug.last_state
            side = side_of(power, state, low=plug.power_alert_low, high=plug.power_alert_high)
            if side == INSIDE:
                _side.pop(plug.id, None)
                continue
            if _side.get(plug.id) == side:
                continue
            last = naive_utc(plug.power_alert_last_triggered) if plug.power_alert_last_triggered else None
            if last is not None and now - last < COOLDOWN:
                continue  # still owed: nothing is recorded, the next tick asks again
            threshold = plug.power_alert_high if side == HIGH else plug.power_alert_low
            lang = current_language()
            await notification_service.on_printer_error(
                plug.printer_id,
                plug.name,
                t(lang, "power_alerts", f"{side}_type"),
                db,
                t(lang, "power_alerts", f"{side}_detail", power=f"{power:.1f}", threshold=f"{threshold:.1f}"),
            )
            # Recorded only after the send: a failed one is tried again.
            _side[plug.id] = side
            plug.power_alert_last_triggered = now
            await db.commit()
            sent += 1
            logger.info("Power alert for plug %s: %s W is %s its limit of %s W", plug.id, power, side, threshold)
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            logger.warning("Power alert for plug %s not sent: %s", plug.id, exc)
            await db.rollback()
    return sent
