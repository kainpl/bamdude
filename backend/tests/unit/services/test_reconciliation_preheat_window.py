"""How long a dispatched job is allowed to take before the sweep calls it an orphan.

A fixed 180 seconds was measured against a swap farm: plate change, then heat-up,
~1m50s. The preheat stage breaks that measurement. It sits between the upload and
``start_print`` and holds the printer at temperature, and its two settings are
``preheat_max_wait_seconds`` (900) plus ``preheat_soak_seconds`` (300) — twenty
minutes by default, ninety at the settings' limits.

⚠️ The wait is the larger term and it is NOT an error path: on a printer with a
chamber sensor but no heater (X1C, P2S) the chamber warms radiantly from the bed,
which takes 15–30 minutes, so the wait routinely runs to its limit. Sizing the
window off the soak alone — the obvious reading of "how long does preheat hold
the printer" — would be short by the bigger half.

The failure this prevents: the sweep closes an archive whose print is still
heating, marks it completed, and the print that then starts has no archive.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.preheat import planned_stage_seconds
from backend.app.services.print_reconciliation import _JUST_DISPATCHED_SECONDS, _grace_seconds


def _settings(**values):
    """Patch the two settings readers preheat uses, nothing else."""
    ints = {"preheat_max_wait_seconds": 900, "preheat_soak_seconds": 300, **values}

    async def get_int(_db, key, default):
        return ints.get(key, default)

    async def get_bool(_db, key, default):
        return values.get(key, default)

    return patch.multiple(
        "backend.app.services.preheat",
        _get_int_setting=AsyncMock(side_effect=get_int),
        _get_bool_setting=AsyncMock(side_effect=get_bool),
    )


class TestTheStageDuration:
    @pytest.mark.asyncio
    async def test_it_is_both_settings_added_up(self) -> None:
        with _settings(preheat_enabled=True):
            assert await planned_stage_seconds(None) == 1200

    @pytest.mark.asyncio
    async def test_the_wait_is_the_half_that_dominates(self) -> None:
        """Pinned as its own case because the soak is the one with "soak" in the
        name, and reaching for it alone is the natural mistake."""
        with _settings(preheat_enabled=True, preheat_soak_seconds=0):
            assert await planned_stage_seconds(None) == 900

    @pytest.mark.asyncio
    async def test_it_is_zero_when_the_stage_will_not_run(self) -> None:
        """So the caller can add it unconditionally instead of branching."""
        with _settings(preheat_enabled=False):
            assert await planned_stage_seconds(None) == 0


class TestPerPrintOverrides:
    @pytest.mark.asyncio
    async def test_on_forces_the_window_open_with_the_global_off(self) -> None:
        """⚠️ The case a per-printer answer would get wrong. A single job can turn
        preheat on for itself, and it is then the only job on the farm that needs
        the longer window."""
        with _settings(preheat_enabled=False):
            assert await planned_stage_seconds(None, override="on") == 1200

    @pytest.mark.asyncio
    async def test_off_keeps_it_shut_with_the_global_on(self) -> None:
        with _settings(preheat_enabled=True):
            assert await planned_stage_seconds(None, override="off") == 0

    @pytest.mark.asyncio
    async def test_anything_unrecognised_reads_as_inherit(self) -> None:
        """The column is free text; a typo must not silently mean "off"."""
        with _settings(preheat_enabled=True):
            assert await planned_stage_seconds(None, override="") == 1200
            assert await planned_stage_seconds(None, override="INHERIT") == 1200


class _Db:
    def __init__(self, override=None, raises=False):
        self._override, self._raises = override, raises

    async def scalar(self, _query):
        if self._raises:
            raise RuntimeError("connection went away")
        return self._override


class TestTheWindowTheSweepUses:
    @pytest.mark.asyncio
    async def test_it_is_the_base_plus_the_stage(self) -> None:
        with _settings(preheat_enabled=True):
            assert await _grace_seconds(_Db(), SimpleNamespace(id=1)) == _JUST_DISPATCHED_SECONDS + 1200

    @pytest.mark.asyncio
    async def test_without_preheat_it_is_exactly_what_it_always_was(self) -> None:
        """No behaviour change for a farm that does not use the stage — the
        measured 180 stands on its own."""
        with _settings(preheat_enabled=False):
            assert await _grace_seconds(_Db(), SimpleNamespace(id=1)) == _JUST_DISPATCHED_SECONDS

    @pytest.mark.asyncio
    async def test_the_archives_own_override_is_what_gets_read(self) -> None:
        with _settings(preheat_enabled=False):
            assert await _grace_seconds(_Db(override="on"), SimpleNamespace(id=1)) == _JUST_DISPATCHED_SECONDS + 1200

    @pytest.mark.asyncio
    async def test_a_failure_falls_back_instead_of_killing_the_sweep(self) -> None:
        """⚠️ Sizing a window wrong delays one cleanup. Raising here abandons
        reconciliation for the whole printer, which is what the sweep exists to
        do."""
        with _settings(preheat_enabled=True):
            assert await _grace_seconds(_Db(raises=True), SimpleNamespace(id=7)) == _JUST_DISPATCHED_SECONDS
