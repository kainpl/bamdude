"""A restore test's closed printer file admission does not reach the next test (G10 A).

The restore route closes the process-wide printer file admission for good, as
production does until the restart; only lifespan startup reopens it. The test
conftest reopens it before every test the way lifespan does. Without that, every
printer file test that ran after a restore test in the same process was refused
as ``restoring`` (found on the integration run, 2026-11-16).

The two tests below run in file order in one process, which is how the full
suite runs. Under xdist they may land on different workers; there the second
proves nothing, but it cannot fail falsely.
"""

import pytest

from backend.app.services.printer_file_lanes import printer_file_runtime


@pytest.mark.asyncio
async def test_a_restore_drain_closes_printer_file_admission():
    assert await printer_file_runtime.quiesce(1)
    assert not printer_file_runtime.admitting


def test_the_next_test_starts_with_admission_open():
    assert printer_file_runtime.admitting
