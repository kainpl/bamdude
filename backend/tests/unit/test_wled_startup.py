"""Startup WLED initialization (N02, upstream #3124 ``test_wled_startup.py``, adapted).

Ours also loads each printer's own *Require plate clear* (A1) and shows offline for an ARCHIVED printer, which our
startup never connects (A12).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.main import initialize_wled_printers
from backend.app.services.wled import WLEDManager


def _db(rows):
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock())
    db.execute.return_value.all.return_value = rows
    return db


@pytest.mark.asyncio
async def test_startup_sets_inactive_and_archived_printers_offline():
    inactive = {"enabled": True, "base_url": "http://wled.local", "presets": {"offline": 9}}
    active = {"enabled": True, "base_url": "http://wled.local", "presets": {"offline": 10}}
    archived = {"enabled": True, "base_url": "http://wled.local", "presets": {"offline": 11}}
    db = _db([(1, inactive, True, False, False), (2, active, False, True, False), (3, archived, True, True, True)])
    with patch("backend.app.main.wled_manager") as manager:
        await initialize_wled_printers(db)
    assert manager.configure_printer.call_count == 3
    manager.configure_printer.assert_any_call(1, inactive)
    manager.configure_printer.assert_any_call(2, active)
    manager.configure_printer.assert_any_call(3, archived)
    assert sorted(c.args for c in manager.set_require_plate_clear.call_args_list) == [(1, True), (2, False), (3, True)]
    assert sorted(c.args for c in manager.handle_offline.call_args_list) == [(1,), (3,)]


@pytest.mark.asyncio
async def test_startup_skips_missing_disabled_and_unmapped_wled_configs():
    db = _db(
        [
            (1, None, True, False, False),
            (2, {"enabled": False, "base_url": "http://wled.local", "presets": {"offline": 9}}, True, False, False),
            (3, {"enabled": True, "base_url": "http://wled.local", "presets": {}}, True, False, True),
        ]
    )
    manager = WLEDManager()
    with patch("backend.app.main.wled_manager", manager):
        await initialize_wled_printers(db)
    assert all(runtime.send_task is None for runtime in manager._runtime.values())
    assert manager._client is None
