"""``filament_low`` fires once per run-down, not once per print (m117).

The event existed from the beginning — a provider column, a settings switch, en+uk
templates — and had no caller anywhere in BamDude, nor in upstream, where it is
defined twice and called zero times. Wiring it to the point where a finished
print's consumption lands on the spool is the easy half. The half that decides
whether anyone keeps the notification switched on is the memory: a spool sitting
below its threshold must not announce itself after every print.

The arithmetic is pinned against the Inventory page's, deliberately. A warning
that disagrees with the number on screen is worse than no warning.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.services.usage_tracker import _global_tray_id, _warn_if_low_stock


async def _spool(db_session, **kwargs) -> Spool:
    defaults = {
        "material": "PLA",
        "label_weight": 1000,
        "weight_used": 0.0,
        "color_name": "Jade White",
    }
    defaults.update(kwargs)
    spool = Spool(**defaults)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


async def _warn(db_session, spool, printer_id: int = 1, tray: int = 0):
    """Run the check with the notification captured."""
    sent = AsyncMock()
    with patch("backend.app.services.notification_service.notification_service.on_filament_low", sent):
        await _warn_if_low_stock(db_session, spool, printer_id, tray)
    return sent


class TestTheGlobalTrayId:
    """``ams_id * 4 + tray_id`` is only right for a plain AMS. An AMS-HT unit
    holds one spool and *is* its own global ID, and the external spools live at
    254/255 behind a sentinel ams_id. Getting this wrong is silent — the warning
    simply names a slot that does not exist.
    """

    def test_a_plain_ams_tray(self) -> None:
        assert _global_tray_id(0, 0) == 0
        assert _global_tray_id(1, 1) == 5  # B2

    def test_an_ams_ht_unit_is_its_own_id(self) -> None:
        assert _global_tray_id(128, 0) == 128
        assert _global_tray_id(129, 0) == 129

    def test_the_external_spools(self) -> None:
        assert _global_tray_id(255, 0) == 254  # Ext-L
        assert _global_tray_id(255, 1) == 255  # Ext-R

    def test_it_inverts_the_modules_own_decomposition(self) -> None:
        """Round-trip against the split the 3MF paths do."""
        for global_id in (0, 3, 5, 11, 128, 131, 254, 255):
            if global_id >= 254:
                ams_id, tray_id = 255, global_id - 254
            elif global_id >= 128:
                ams_id, tray_id = global_id, 0
            else:
                ams_id, tray_id = global_id // 4, global_id % 4
            assert _global_tray_id(ams_id, tray_id) == global_id


class TestTheThreshold:
    @pytest.mark.asyncio
    async def test_a_full_spool_is_silent(self, db_session) -> None:
        spool = await _spool(db_session, weight_used=100.0)  # 90% left
        assert (await _warn(db_session, spool)).await_count == 0
        assert spool.low_stock_notified is False

    @pytest.mark.asyncio
    async def test_the_default_threshold_is_twenty_percent(self, db_session) -> None:
        """Matches the Inventory page's fallback when the setting is unset."""
        spool = await _spool(db_session, weight_used=810.0)  # 19% left
        assert (await _warn(db_session, spool)).await_count == 1

    @pytest.mark.asyncio
    async def test_exactly_at_the_threshold_is_not_low(self, db_session) -> None:
        """The page uses a strict ``<``; so does this, or a spool would be
        painted normal on screen while a warning says otherwise."""
        spool = await _spool(db_session, weight_used=800.0)  # exactly 20%
        assert (await _warn(db_session, spool)).await_count == 0

    @pytest.mark.asyncio
    async def test_the_global_setting_is_honoured(self, db_session) -> None:
        db_session.add(Settings(key="low_stock_threshold", value="50"))
        spool = await _spool(db_session, weight_used=550.0)  # 45% left
        await db_session.commit()
        assert (await _warn(db_session, spool)).await_count == 1

    @pytest.mark.asyncio
    async def test_the_per_spool_override_beats_the_global(self, db_session) -> None:
        db_session.add(Settings(key="low_stock_threshold", value="50"))
        spool = await _spool(db_session, weight_used=550.0, low_stock_threshold_pct=10)
        await db_session.commit()
        assert (await _warn(db_session, spool)).await_count == 0, "45% left is fine for a spool set to warn at 10%"

    @pytest.mark.asyncio
    async def test_an_archived_spool_says_nothing(self, db_session) -> None:
        spool = await _spool(db_session, weight_used=990.0, archived_at=datetime.now(timezone.utc))
        assert (await _warn(db_session, spool)).await_count == 0

    @pytest.mark.asyncio
    async def test_a_spool_with_no_label_weight_is_skipped(self, db_session) -> None:
        """Nothing to take a percentage of — never divide by it either."""
        spool = await _spool(db_session, label_weight=0, weight_used=50.0)
        assert (await _warn(db_session, spool)).await_count == 0


class TestTheDeduplication:
    @pytest.mark.asyncio
    async def test_a_low_spool_warns_once_not_once_per_print(self, db_session) -> None:
        spool = await _spool(db_session, weight_used=850.0)

        first = await _warn(db_session, spool)
        second = await _warn(db_session, spool)
        third = await _warn(db_session, spool)

        assert first.await_count == 1
        assert second.await_count == 0
        assert third.await_count == 0

    @pytest.mark.asyncio
    async def test_climbing_back_above_the_line_re_arms_it(self, db_session) -> None:
        """A refill has to make the next run-down announce itself again."""
        spool = await _spool(db_session, weight_used=850.0)
        assert (await _warn(db_session, spool)).await_count == 1

        spool.weight_used = 0.0  # refilled
        await db_session.commit()
        assert (await _warn(db_session, spool)).await_count == 0
        assert spool.low_stock_notified is False, "the flag must be cleared, not merely skipped"

        spool.weight_used = 850.0  # run down again
        await db_session.commit()
        assert (await _warn(db_session, spool)).await_count == 1

    @pytest.mark.asyncio
    async def test_the_flag_survives_a_restart(self, db_session) -> None:
        """Persisted, not in-memory: a reboot must not re-warn about every low
        spool on the farm."""
        spool = await _spool(db_session, weight_used=850.0)
        await _warn(db_session, spool)
        await db_session.commit()

        db_session.expunge_all()
        reloaded = await db_session.get(Spool, spool.id)
        assert reloaded.low_stock_notified is True
        assert (await _warn(db_session, reloaded)).await_count == 0


class TestTheMessage:
    @pytest.mark.asyncio
    async def test_it_carries_the_slot_label_and_the_remaining_percent(self, db_session, printer_factory) -> None:
        printer = await printer_factory(name="A1M-TL")
        spool = await _spool(db_session, weight_used=850.0)

        sent = await _warn(db_session, spool, printer_id=printer.id, tray=5)

        kwargs = sent.await_args.kwargs
        assert kwargs["printer_name"] == "A1M-TL"
        assert kwargs["slot"] == "B2", "global tray 5 is unit B, tray 2 — the label every other message uses"
        assert kwargs["remaining_percent"] == 15
        assert kwargs["color"] == "Jade White"

    @pytest.mark.asyncio
    async def test_the_external_spool_gets_its_own_label(self, db_session, printer_factory) -> None:
        printer = await printer_factory()
        spool = await _spool(db_session, weight_used=850.0)

        sent = await _warn(db_session, spool, printer_id=printer.id, tray=254)

        assert sent.await_args.kwargs["slot"] == "Ext-L"

    @pytest.mark.asyncio
    async def test_a_failing_notification_does_not_lose_the_consumption(self, db_session) -> None:
        """Tracking what was consumed is the job; announcing it is not allowed
        to take the write down with it."""
        spool = await _spool(db_session, weight_used=850.0)

        boom = AsyncMock(side_effect=RuntimeError("telegram is down"))
        with patch("backend.app.services.notification_service.notification_service.on_filament_low", boom):
            await _warn_if_low_stock(db_session, spool, 1, 0)

        assert spool.weight_used == 850.0
