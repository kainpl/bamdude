"""Zero is a real stagger interval: cap the concurrent starts, add no delay.

An operator who wants the concurrency limit without the wait had no way to ask
for it — the settings input clamped to 1 and, worse, computed the stored value
with ``parseInt(...) || 5``, so typing 0 silently became 5. This pins the whole
path underneath that input: the schema accepts it, and the scheduler reads it as
zero seconds rather than falling back to the five-minute default.
"""

import pytest

from backend.app.schemas.settings import AppSettings, AppSettingsUpdate
from backend.app.services.print_scheduler import PrintScheduler


def test_the_schema_accepts_zero():
    assert AppSettingsUpdate(stagger_interval_minutes=0).stagger_interval_minutes == 0
    assert AppSettings(stagger_interval_minutes=0).stagger_interval_minutes == 0
    # …and still defaults to five when nobody said anything.
    assert AppSettings().stagger_interval_minutes == 5


@pytest.mark.parametrize(
    ("stored", "expected_seconds"),
    [
        ("0", 0),  # ⚠️ the falsy-zero trap: `("0" or "5")` must stay "0"
        ("5", 300),
        ("", 300),  # unset falls back to the default, which is what `or` is for
        (None, 300),
    ],
)
@pytest.mark.asyncio
async def test_the_scheduler_reads_zero_as_zero_not_as_the_default(monkeypatch, stored, expected_seconds):
    scheduler = PrintScheduler()

    async def fake_bool(_db, key):
        return key == "stagger_enabled"

    async def fake_value(_db, key):
        return "2" if key == "stagger_concurrent" else stored

    monkeypatch.setattr(scheduler, "_get_bool_setting", fake_bool)
    monkeypatch.setattr(scheduler, "_get_setting_value", fake_value)

    enabled, concurrent, interval_seconds, _wait = await scheduler._get_stagger_settings(None)
    assert enabled is True
    assert concurrent == 2
    assert interval_seconds == expected_seconds


@pytest.mark.asyncio
async def test_a_zero_interval_slot_frees_on_the_next_pass(monkeypatch):
    """With no delay the slot must not linger: it holds only until the cleanup
    runs, so the cap is the only thing still limiting starts."""
    from backend.app.services import print_scheduler as ps

    scheduler = PrintScheduler()
    scheduler._register_stagger_start(7, 0)
    assert len(scheduler._stagger_slots) == 1

    # No live state for the printer: the branch that decides on elapsed time.
    monkeypatch.setattr(ps.printer_manager, "get_status", lambda _pid: None)
    scheduler._cleanup_stagger_slots(wait_for_bed=False)
    assert scheduler._stagger_slots == []

    # The same slot with a real interval is still held.
    scheduler._register_stagger_start(7, 300)
    scheduler._cleanup_stagger_slots(wait_for_bed=False)
    assert len(scheduler._stagger_slots) == 1
