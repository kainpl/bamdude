"""Unit tests for the preheat / heat-soak stage (upstream Bambuddy #1468).

The stage is a standalone coroutine in ``services/preheat.py`` (BamDude runs it
from ``background_dispatch``, not upstream's ``PrintScheduler`` method). These
tests patch the settings readers + ``printer_manager`` + ``asyncio.sleep`` so the
wait/soak loops don't actually block, and assert on which client commands fired.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import preheat
from backend.app.services.preheat import (
    _derive_chamber_target,
    _normalize_filament_type,
    preheat_and_soak,
)


def _make_client():
    client = MagicMock()
    client.set_bed_temperature = MagicMock(return_value=True)
    client.set_chamber_temperature = MagicMock(return_value=True)
    client.set_airduct_mode = MagicMock(return_value=True)
    return client


def _make_db():
    """DB stand-in whose ``commit()`` is awaitable — ``preheat_and_soak`` now
    commits to release the pooled connection before the heat-soak wait (#2572)."""
    db = MagicMock()
    db.commit = AsyncMock()
    return db


def _make_state(bed=100.0, chamber=60.0, airduct_mode=0, ams=None):
    return SimpleNamespace(
        temperatures={"bed": bed, "chamber": chamber},
        airduct_mode=airduct_mode,
        raw_data={"ams": ams if ams is not None else []},
    )


def _make_printer(model, pid=7):
    return SimpleNamespace(id=pid, model=model)


def _make_archive(bed_temperature=100):
    return SimpleNamespace(bed_temperature=bed_temperature)


def _ams(*tray_types):
    """Build a raw_data['ams'] list with one AMS unit whose trays carry the given types."""
    return [{"tray": [{"tray_type": t} for t in tray_types]}]


# --- pure helpers -----------------------------------------------------------


def test_normalize_filament_type():
    assert _normalize_filament_type("PLA Basic") == "PLA"
    assert _normalize_filament_type("PA-CF") == "PA-CF"
    assert _normalize_filament_type("abs") == "ABS"
    assert _normalize_filament_type("") == ""


def test_derive_chamber_target_max_across_slots():
    printer = _make_printer("H2D")
    targets = {"PLA": 0, "PA": 50, "DEFAULT": 0}
    with patch.object(preheat.printer_manager, "get_status", return_value=_make_state(ams=_ams("PLA Basic", "PA-CF"))):
        # PA-CF isn't in the map → falls to DEFAULT (0); PLA→0; but PA-CF normalises to
        # "PA-CF" which is absent so 0, PLA→0 → best 0. Add PA to prove max.
        assert _derive_chamber_target(printer, targets) == 0
    with patch.object(preheat.printer_manager, "get_status", return_value=_make_state(ams=_ams("PLA", "PA"))):
        assert _derive_chamber_target(printer, targets) == 50  # PA's 50 is binding over PLA's 0


def test_derive_chamber_target_no_ams_is_zero():
    printer = _make_printer("H2D")
    with patch.object(preheat.printer_manager, "get_status", return_value=_make_state(ams=[])):
        assert _derive_chamber_target(printer, {"PLA": 0, "DEFAULT": 0}) == 0
    with patch.object(preheat.printer_manager, "get_status", return_value=None):
        assert _derive_chamber_target(printer, {"PLA": 0, "DEFAULT": 0}) == 0


@pytest.mark.asyncio
async def test_get_preheat_filament_targets_defaults_and_parse():
    db = MagicMock()
    with patch.object(preheat, "_get_setting_str", AsyncMock(return_value=None)):
        defaults = await preheat._get_preheat_filament_targets(db)
    assert defaults["PA"] == 50 and defaults["PLA"] == 0 and defaults["DEFAULT"] == 0
    with patch.object(preheat, "_get_setting_str", AsyncMock(return_value='{"abs": 55}')):
        parsed = await preheat._get_preheat_filament_targets(db)
    assert parsed["ABS"] == 55 and parsed["DEFAULT"] == 0  # DEFAULT always injected
    with patch.object(preheat, "_get_setting_str", AsyncMock(return_value="not json")):
        fallback = await preheat._get_preheat_filament_targets(db)
    assert fallback["PA"] == 50  # malformed → bundled defaults


# --- preheat_and_soak: override resolution ----------------------------------


@pytest.mark.asyncio
async def test_override_off_skips_entirely():
    client = _make_client()
    with patch.object(preheat.printer_manager, "get_client", return_value=client):
        await preheat_and_soak(MagicMock(), _make_printer("H2D"), _make_archive(), options={"preheat_override": "off"})
    client.set_bed_temperature.assert_not_called()


@pytest.mark.asyncio
async def test_inherit_global_off_skips():
    client = _make_client()
    with (
        patch.object(preheat, "_get_bool_setting", AsyncMock(return_value=False)),
        patch.object(preheat.printer_manager, "get_client", return_value=client),
    ):
        await preheat_and_soak(
            MagicMock(), _make_printer("H2D"), _make_archive(), options={"preheat_override": "inherit"}
        )
    client.set_bed_temperature.assert_not_called()


@pytest.mark.asyncio
async def test_on_runs_despite_global_off():
    client = _make_client()
    with (
        patch.object(preheat, "_get_bool_setting", AsyncMock(return_value=False)),
        patch.object(
            preheat, "_get_int_setting", AsyncMock(side_effect=lambda _d, _k, default: 0 if "soak" in _k else 1)
        ),
        patch.object(preheat, "_get_preheat_filament_targets", AsyncMock(return_value={"PLA": 0, "DEFAULT": 0})),
        patch.object(preheat.printer_manager, "get_client", return_value=client),
        patch.object(preheat.printer_manager, "get_status", return_value=_make_state()),
        patch.object(preheat.asyncio, "sleep", AsyncMock()),
    ):
        await preheat_and_soak(
            _make_db(), _make_printer("H2D"), _make_archive(bed_temperature=100), options={"preheat_override": "on"}
        )
    client.set_bed_temperature.assert_called_once_with(100)


# --- preheat_and_soak: chamber target + tiers -------------------------------


@pytest.mark.asyncio
async def test_explicit_chamber_override_beats_filament_map():
    client = _make_client()
    with (
        patch.object(preheat, "_get_bool_setting", AsyncMock(return_value=True)),
        patch.object(
            preheat, "_get_int_setting", AsyncMock(side_effect=lambda _d, _k, default: 0 if "soak" in _k else 1)
        ),
        patch.object(preheat.printer_manager, "get_client", return_value=client),
        patch.object(preheat.printer_manager, "get_status", return_value=_make_state(chamber=60)),
        patch.object(preheat.asyncio, "sleep", AsyncMock()),
    ):
        # H2D is a chamber-heater model → explicit target 55 fires M141 at 55.
        await preheat_and_soak(
            _make_db(),
            _make_printer("H2D"),
            _make_archive(),
            options={"preheat_override": "on", "preheat_chamber_target_override": 55},
        )
    client.set_chamber_temperature.assert_called_once_with(55)


@pytest.mark.asyncio
async def test_sensor_only_model_no_m141():
    """X1C reports chamber temp but has no active heater → M141 must NOT fire."""
    client = _make_client()
    with (
        patch.object(preheat, "_get_bool_setting", AsyncMock(return_value=True)),
        patch.object(
            preheat, "_get_int_setting", AsyncMock(side_effect=lambda _d, _k, default: 0 if "soak" in _k else 1)
        ),
        patch.object(preheat.printer_manager, "get_client", return_value=client),
        patch.object(preheat.printer_manager, "get_status", return_value=_make_state(chamber=60)),
        patch.object(preheat.asyncio, "sleep", AsyncMock()),
    ):
        await preheat_and_soak(
            _make_db(),
            _make_printer("X1C"),
            _make_archive(),
            options={"preheat_override": "on", "preheat_chamber_target_override": 50},
        )
    client.set_bed_temperature.assert_called_once()
    client.set_chamber_temperature.assert_not_called()  # sensor-only: no heater


@pytest.mark.asyncio
async def test_airduct_flip_to_heating_on_airduct_model():
    client = _make_client()
    with (
        patch.object(preheat, "_get_bool_setting", AsyncMock(return_value=True)),
        patch.object(
            preheat, "_get_int_setting", AsyncMock(side_effect=lambda _d, _k, default: 0 if "soak" in _k else 1)
        ),
        patch.object(preheat.printer_manager, "get_client", return_value=client),
        # current airduct = cooling (0); target > 0 wants heating (1) → flip fires.
        patch.object(preheat.printer_manager, "get_status", return_value=_make_state(chamber=60, airduct_mode=0)),
        patch.object(preheat.asyncio, "sleep", AsyncMock()),
    ):
        await preheat_and_soak(
            _make_db(),
            _make_printer("H2D"),
            _make_archive(),
            options={"preheat_override": "on", "preheat_chamber_target_override": 50},
        )
    client.set_airduct_mode.assert_called_once_with("heating")


@pytest.mark.asyncio
async def test_airduct_not_flipped_when_already_correct():
    client = _make_client()
    with (
        patch.object(preheat, "_get_bool_setting", AsyncMock(return_value=True)),
        patch.object(
            preheat, "_get_int_setting", AsyncMock(side_effect=lambda _d, _k, default: 0 if "soak" in _k else 1)
        ),
        patch.object(preheat.printer_manager, "get_client", return_value=client),
        # already heating (1) + target > 0 → no flip (idempotent).
        patch.object(preheat.printer_manager, "get_status", return_value=_make_state(chamber=60, airduct_mode=1)),
        patch.object(preheat.asyncio, "sleep", AsyncMock()),
    ):
        await preheat_and_soak(
            _make_db(),
            _make_printer("H2D"),
            _make_archive(),
            options={"preheat_override": "on", "preheat_chamber_target_override": 50},
        )
    client.set_airduct_mode.assert_not_called()


# --- preheat_and_soak: best-effort guards -----------------------------------


@pytest.mark.asyncio
async def test_missing_bed_temp_skips():
    client = _make_client()
    with (
        patch.object(preheat, "_get_bool_setting", AsyncMock(return_value=True)),
        patch.object(preheat, "_get_int_setting", AsyncMock(side_effect=lambda _d, _k, default: 1)),
        patch.object(preheat, "_get_preheat_filament_targets", AsyncMock(return_value={"PLA": 0, "DEFAULT": 0})),
        patch.object(preheat.printer_manager, "get_client", return_value=client),
    ):
        await preheat_and_soak(
            MagicMock(), _make_printer("H2D"), _make_archive(bed_temperature=None), options={"preheat_override": "on"}
        )
    client.set_bed_temperature.assert_not_called()  # no bed metadata → skip


@pytest.mark.asyncio
async def test_cancel_check_propagates():
    """A cancel raised in the wait loop aborts the stage (not swallowed)."""
    client = _make_client()

    class _Cancel(Exception):
        pass

    def _cancel():
        raise _Cancel()

    with (
        patch.object(preheat, "_get_bool_setting", AsyncMock(return_value=True)),
        patch.object(
            preheat, "_get_int_setting", AsyncMock(side_effect=lambda _d, _k, default: 0 if "soak" in _k else 60)
        ),
        patch.object(preheat, "_get_preheat_filament_targets", AsyncMock(return_value={"PLA": 0, "DEFAULT": 0})),
        patch.object(preheat.printer_manager, "get_client", return_value=client),
        # chamber below target so the loop would keep waiting → cancel_check fires first.
        patch.object(preheat.printer_manager, "get_status", return_value=_make_state(bed=20, chamber=20)),
        patch.object(preheat.asyncio, "sleep", AsyncMock()),
        pytest.raises(_Cancel),
    ):
        await preheat_and_soak(
            _make_db(),
            _make_printer("H2D"),
            _make_archive(),
            options={"preheat_override": "on", "preheat_chamber_target_override": 50},
            cancel_check=_cancel,
        )
