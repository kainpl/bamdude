"""A print sent to the RIGHT external holder charges the right holder's spool
(upstream 37502719b #3166, 991d2caca, fb69a665d).

The firmware rejects 254/255 in the flat ``ams_mapping``, so BambuStudio — and our own
dispatch — write an external holder there as ``-1`` and name the holder only in
``ams_mapping2``. We captured the flat list alone and read ``-1`` as "the external holder,
254 first": on a two-holder machine (X2D, H2D) a BambuStudio print fed from the right
holder charged the LEFT holder's spool, in both inventory backends. The capture now puts
the holder back from the command's own ``ams_mapping2``, so the dispatched mapping — still
the first authority — names it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import spoolman_charge
from backend.app.services.bambu_mqtt import BambuMQTTClient, resolve_external_spools_in_mapping
from backend.app.services.usage_tracker import _active_sessions
from backend.app.utils.tray_split import CompletionSnapshot, terminal_snapshot_fields
from backend.tests.unit.services.test_partial_print_split import (  # noqa: F401 — fixtures
    _clean_memory,
    history,
    run_internal,
    session,
    stage,
)
from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx, _make_db

# C047 part 2: the accounting tail runs inside an admitted charge (services/spoolman_charge).
pytestmark = pytest.mark.usefixtures("spoolman_charge_admitted")

LEFT, RIGHT = {"ams_id": 254, "slot_id": 0}, {"ams_id": 255, "slot_id": 0}
UNMAPPED = {"ams_id": 255, "slot_id": 255}


class TestTheHolderIsPutBack:
    def test_a_two_holder_printer_keeps_the_holder_the_command_names(self):
        assert resolve_external_spools_in_mapping([-1], [RIGHT], True) == [255]
        assert resolve_external_spools_in_mapping([-1], [LEFT], True) == [254]

    def test_a_single_holder_printer_has_one_holder_whatever_the_wire_calls_it(self):
        """The wire's 255 is the only external spool there, which the rest of BamDude calls 254."""
        assert resolve_external_spools_in_mapping([-1], [RIGHT], False) == [254]

    def test_an_ams_slot_beside_it_is_left_exactly_as_captured(self):
        assert resolve_external_spools_in_mapping([0, -1], [{"ams_id": 0, "slot_id": 0}, RIGHT], True) == [0, 255]

    def test_the_unmapped_marker_stays_minus_one(self):
        assert resolve_external_spools_in_mapping([-1, -1], [UNMAPPED, LEFT], True) == [-1, 254]

    @pytest.mark.parametrize("detail", [None, "x", [RIGHT, RIGHT], [], [{"ams_id": 3, "slot_id": 1}], [None]])
    def test_without_a_usable_detail_the_flat_list_stands(self, detail):
        assert resolve_external_spools_in_mapping([-1], detail, True) == [-1]

    def test_an_a2l_lite_slot_is_not_an_external_holder(self):
        assert resolve_external_spools_in_mapping([1], [{"ams_id": 16, "slot_id": 1}], False) == [1]

    def test_something_that_is_no_list_is_returned_untouched(self):
        assert resolve_external_spools_in_mapping(None, [RIGHT], True) is None


def studio_command(flat, detail):
    """A BambuStudio ``project_file`` as it crosses the LAN broker."""
    return {
        "print": {
            "command": "project_file",
            "sequence_id": "20011",
            "file": "Clip.gcode.3mf",
            "url": "ftp://Clip.gcode.3mf",
            "subtask_name": "Clip",
            "param": "Metadata/plate_1.gcode",
            "use_ams": True,
            "ams_mapping": flat,
            "ams_mapping2": detail,
        }
    }


def mqtt_client(model):
    return BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-EXT", access_code="synthetic", model=model)


def captured(model, flat, detail):
    client = mqtt_client(model)
    client._handle_project_file_command(studio_command(flat, detail))
    return client._captured_ams_mapping


class TestTheCapture:
    def test_x2d_right_holder(self):
        assert captured("X2D", [-1], [RIGHT]) == [255]

    def test_x2d_left_holder(self):
        assert captured("X2D", [-1], [LEFT]) == [254]

    def test_a_dual_nozzle_printer_known_only_from_its_extruder_report(self):
        client = mqtt_client(None)
        client._is_dual_nozzle = True
        client._handle_project_file_command(studio_command([2, -1], [{"ams_id": 0, "slot_id": 2}, RIGHT]))
        assert client._captured_ams_mapping == [2, 255]

    def test_p1s_external_spool(self):
        assert captured("P1S", [-1], [RIGHT]) == [254]

    def test_a_command_without_the_detail_keeps_the_flat_list(self):
        client = mqtt_client("X2D")
        command = studio_command([-1], None)
        del command["print"]["ams_mapping2"]
        client._handle_project_file_command(command)
        assert client._captured_ams_mapping == [-1]

    def test_the_printers_echo_is_read_the_same_way(self):
        client = mqtt_client("X2D")
        echo = studio_command([-1], [RIGHT])
        echo["print"].update(result="success", reason="success")
        client._handle_project_file_command(echo, source="report")
        assert client._captured_ams_mapping == [255]


class TestOurOwnDispatchIsUntouched:
    """What BamDude sends is byte for byte what it sent; read back, it names what was asked."""

    def _dispatch(self, model, mapping):
        client = mqtt_client(model)
        client._client = MagicMock()
        client.state.connected = True
        client.state.raw_data["ams"] = {"ams": [{"id": "0"}]}
        client.start_print("test.3mf", ams_mapping=mapping)
        return client, json.loads(client._client.publish.call_args[0][1])

    def test_a_two_holder_dispatch_goes_out_as_before_and_reads_back_whole(self):
        client, sent = self._dispatch("X2D", [0, 255])
        assert sent["print"]["ams_mapping"] == [0, -1]
        assert sent["print"]["ams_mapping2"] == [{"ams_id": 0, "slot_id": 0}, RIGHT]
        client._handle_project_file_command(sent)
        assert client._captured_ams_mapping == [0, 255]

    def test_a_single_holder_dispatch_goes_out_as_before_and_reads_back_as_the_one_holder(self):
        client, sent = self._dispatch("P1S", [254])
        assert sent["print"]["ams_mapping"] == [-1]
        assert sent["print"]["ams_mapping2"] == [RIGHT]
        client._handle_project_file_command(sent)
        assert client._captured_ams_mapping == [254]


# ── The figure: 40 g from the right holder, both holders loaded ────────────────

ESTIMATE = [{"slot_id": 1, "used_g": 40.0, "type": "PETG", "color": "#000000"}]
COMPLETED = {
    "status": "completed",
    "last_layer_num": 200,
    "last_progress": 100.0,
    **terminal_snapshot_fields(layer_num=200, total_layers=200, progress=100.0),
}
STOPPED_HALF_WAY = {
    "status": "failed",
    "last_layer_num": 100,
    "last_progress": 50.0,
    **terminal_snapshot_fields(layer_num=100, total_layers=200, progress=50.0),
}
BOTH_HOLDERS = [
    {"id": 254, "tray_type": "PLA", "tray_uuid": "LEFT"},
    {"id": 255, "tray_type": "PETG", "tray_uuid": "RIGHT"},
]


def x2d_state():
    state = MagicMock()
    state.raw_data = {"ams": [], "vt_tray": BOTH_HOLDERS}
    state.tray_now = 254  # the firmware's generic "external of the active nozzle" (measured, archive 775)
    state.last_loaded_tray = -1
    state.tray_change_log = []
    pm = MagicMock()
    pm.get_status.return_value = state
    return pm


def studio_print_to_the_right_holder():
    return captured("X2D", [-1], [RIGHT])


class TestInternalInventory:
    async def test_a_completed_print_charges_the_right_holders_spool(self, db_session, stage):  # noqa: F811
        """Was {(left, 40.0)}: ``-1`` read as "254 first"."""
        _active_sessions[stage.printer.id] = session(
            stage.printer.id,
            spool_assignments={(255, 0): stage.a, (255, 1): stage.b},
            started_at=None,
            ams_mapping=studio_print_to_the_right_holder(),
        )
        await run_internal(db_session, stage.printer, stage.archive, COMPLETED, x2d_state(), usage=ESTIMATE)
        assert await history(db_session) == {(stage.b, 40.0)}

    async def test_a_stopped_print_charges_the_right_holders_spool(self, db_session, stage):  # noqa: F811
        _active_sessions[stage.printer.id] = session(
            stage.printer.id,
            spool_assignments={(255, 0): stage.a, (255, 1): stage.b},
            started_at=None,
            ams_mapping=studio_print_to_the_right_holder(),
        )
        await run_internal(db_session, stage.printer, stage.archive, STOPPED_HALF_WAY, x2d_state(), usage=ESTIMATE)
        assert await history(db_session) == {(stage.b, 20.0)}


REELS = {"LEFT": {"id": 54, "filament": {}}, "RIGHT": {"id": 55, "filament": {}}}


def _spoolman_tracking_row(slot_to_tray):
    return SimpleNamespace(
        archive_id=144,
        filament_usage=ESTIMATE,
        layer_usage=None,
        filament_properties={},
        ams_trays={int(vt["id"]): {"tray_uuid": vt["tray_uuid"], "tray_type": vt["tray_type"]} for vt in BOTH_HOLDERS},
        slot_to_tray=slot_to_tray,
        tray_remain_start={},
    )


async def _spoolman(data, slot_to_tray):
    from backend.app.services.spoolman_tracking import _report_partial_usage, report_usage

    tracking = _spoolman_tracking_row(slot_to_tray)
    client = AsyncMock()
    client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: REELS.get(tag))
    client.use_spool = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(_make_db(tracking))),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback", AsyncMock(return_value=client)
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._resolve_spool_tag", lambda tray, *_a: tray.get("tray_uuid")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch("backend.app.services.printer_manager.printer_manager", x2d_state()),
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", AsyncMock()),
    ):
        completion = CompletionSnapshot.from_event(data)
        if data["status"] == "completed":
            await report_usage(printer_id=1, archive_id=144, completion=completion)
        else:
            await _report_partial_usage(
                charge=await spoolman_charge.admit("test"),
                printer_id=1,
                tracking=tracking,
                last_layer_num=data["last_layer_num"],
                last_progress=data["last_progress"],
                completion=completion,
            )
    return {call.args[0]: call.args[1] for call in client.use_spool.await_args_list}


class TestSpoolman:
    async def test_a_completed_print_charges_the_right_holders_spool(self):
        """Was {54: 40.0} — the left holder's spool."""
        assert await _spoolman(COMPLETED, studio_print_to_the_right_holder()) == {55: 40.0}

    async def test_a_stopped_print_charges_the_right_holders_spool(self):
        assert await _spoolman(STOPPED_HALF_WAY, studio_print_to_the_right_holder()) == {55: 20.0}

    async def test_a_print_to_the_left_holder_still_charges_the_left(self):
        assert await _spoolman(COMPLETED, captured("X2D", [-1], [LEFT])) == {54: 40.0}

    async def test_a_command_without_the_detail_keeps_todays_reading(self):
        """GUARD: ``-1`` with nothing to resolve it is still "the external holder, 254 first"."""
        assert await _spoolman(COMPLETED, [-1]) == {54: 40.0}


class TestMinusOneWithNoExternalHolderLoaded:
    """A used slot the mapping sends to the external holder, on a printer whose holder is empty
    (a slicer print of a plate we could not identify): Spoolman charged whichever AMS tray sat
    at that slot's POSITION. The internal inventory resolves the holder, finds no spool and
    reports the filament as not recorded — so does Spoolman now."""

    async def test_nothing_is_charged_and_the_notice_names_the_holder(self):
        from backend.app.services.spoolman_tracking import report_usage

        tracking = SimpleNamespace(
            archive_id=144,
            filament_usage=[{"slot_id": 1, "used_g": 12.0}, {"slot_id": 2, "used_g": 30.0}],
            layer_usage=None,
            filament_properties={},
            ams_trays={0: {"tray_uuid": "TRAY0", "tray_type": "PLA"}, 1: {"tray_uuid": "TRAY1", "tray_type": "PLA"}},
            slot_to_tray=[-1, 1],
            tray_remain_start={},
        )
        reels = {"TRAY0": {"id": 10, "filament": {}}, "TRAY1": {"id": 11, "filament": {}}}
        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(side_effect=lambda tag: reels.get(tag))
        client.use_spool = AsyncMock()
        notify = AsyncMock()
        pm = MagicMock()
        pm.get_status.return_value = None
        with (
            patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(_make_db(tracking))),
            patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
            patch(
                "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
                AsyncMock(return_value=client),
            ),
            patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="")),
            patch(
                "backend.app.services.spoolman_tracking._resolve_spool_id_via_slot_assignment",
                AsyncMock(return_value=None),
            ),
            patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=[])),
            patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
            patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
            patch("backend.app.services.printer_manager.printer_manager", pm),
            patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
        ):
            await report_usage(printer_id=1, archive_id=144, completion=CompletionSnapshot.from_event(COMPLETED))

        charged = {call.args[0]: call.args[1] for call in client.use_spool.await_args_list}
        assert charged == {11: 30.0}, "slot 2's own tray is charged; tray 0's spool (10) fed nothing"
        assert notify.await_args.args[1] == [(254, 12.0)]

    async def test_a_spool_bound_to_the_unconfigured_holder_is_charged(self):
        """The holder reports no filament type (never configured on the printer), but a spool is
        bound to it in BamDude: that binding is the answer, not a position."""
        from backend.app.services.spoolman_tracking import _report_spool_usage_for_slots

        client = AsyncMock()
        client.find_spool_by_tag = AsyncMock(return_value=None)
        client.use_spool = AsyncMock()

        async def bound(_printer_id, ams_id, tray_id):
            return 77 if (ams_id, tray_id) == (255, 0) else None

        with patch("backend.app.services.spoolman_tracking._resolve_spool_id_via_slot_assignment", bound):
            updated = await _report_spool_usage_for_slots(
                client, [(1, 12.0)], {0: {"tray_uuid": "TRAY0"}}, [-1], "Archive 144", "", printer_id=1
            )
        assert updated == 1
        client.use_spool.assert_awaited_once_with(77, 12.0)


class TestTheActiveTrayHighlightStillFollowsAnAllExternalPrint:
    """#1822: an H2S reports ``tray_now`` as an AMS slot while it feeds from the external spool;
    an all-external mapping promotes it to 254. The capture now reads such a mapping as [254]
    rather than [-1] — the promotion must hold for both spellings."""

    AMS_PUSH = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0"}]}], "tray_now": "0", "ams_exist_bits": "1"}}

    @pytest.mark.parametrize("detail", [[RIGHT], None])
    def test_h2s_all_external(self, detail):
        client = mqtt_client("H2S")
        command = studio_command([-1], detail)
        if detail is None:
            del command["print"]["ams_mapping2"]
        client._handle_project_file_command(command)
        client._process_message({"print": dict(self.AMS_PUSH)})
        assert client.state.tray_now == 254

    def test_a_mixed_mapping_is_not_promoted(self):
        client = mqtt_client("H2S")
        client._handle_project_file_command(studio_command([0, -1], [{"ams_id": 0, "slot_id": 0}, RIGHT]))
        client._process_message({"print": dict(self.AMS_PUSH)})
        assert client.state.tray_now == 0
