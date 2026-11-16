"""``GET /slicer/loaded-filaments`` — what the Slice dialog's two farm filters ask.

Online = active, not archived, connected, inside the caller's printer scope.
Loaded = the spool a slot holds and its family, read from the feed snapshot:
a slot advertised as a Generic profile for AMS backup compatibility answers
with the spool it really holds; a spool without a family is matched by the
material type its slot states; an external holder counts, an empty slot does
not; and Spoolman mode answers the same without asking Spoolman.

The printers' state is built by the real snapshot builder from hand-built
status pushes; only the manager's two lookups are replaced.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services import ams_advertised_overlay as overlay
from backend.app.services.printer_feed_snapshot import FeedTelemetry, snapshot_from_state
from backend.app.services.printer_manager import printer_manager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

URL = "/api/v1/slicer/loaded-filaments"
PLA_BASIC = "GFA00"  # Bambu PLA Basic
GENERIC_PLA = "GFL99"
GENERIC_ASA = "GFB98"


def _tray(tid: int, tray_type: str, idx: str = "", color: str = "FFFFFFFF") -> dict:
    return {"id": str(tid), "tray_type": tray_type, "tray_info_idx": idx, "tray_color": color}


def _state(model: str, *, trays: list[dict] | None = None, external: dict | None = None, nozzle: str = "0.4"):
    telemetry = FeedTelemetry()
    report: dict = {"nozzle_diameter": nozzle}
    if trays is not None:
        report["ams"] = {"ams": [{"id": "0", "tray": trays}], "ams_exist_bits": "1"}
    else:
        report["ams"] = {"ams": [], "ams_exist_bits": "0"}
    report["vt_tray"] = external if external is not None else {"id": "254", "tray_type": ""}
    telemetry.observe({"print": report}, model)
    return SimpleNamespace(feed_telemetry=telemetry, connected=True, connection_generation=1)


@pytest.fixture
def farm(monkeypatch):
    """Install printers' live state behind the manager: ``farm[printer] = state``."""
    states: dict[int, tuple[str, object]] = {}

    def put(printer, state) -> None:
        states[printer.id] = (printer.model, state)

    def snapshot(printer_id: int):
        model, state = states[printer_id]
        return snapshot_from_state(printer_id, model, state, overlay=overlay.entries_for(printer_id))

    monkeypatch.setattr(printer_manager, "is_connected", lambda pid: pid in states)
    monkeypatch.setattr(printer_manager, "get_feed_snapshot", snapshot)
    overlay.forget_all()
    yield put
    overlay.forget_all()


def _advertise(printer_id: int, tray_id: int, *, actual: tuple[str, str, str], advertised: tuple[str, str]) -> None:
    """Record that slot ``tray_id`` was told ``advertised`` while it holds ``actual``."""
    overlay.replace_printer(
        printer_id,
        {
            (0, tray_id): overlay.OverlayEntry(
                actual_material=actual[0],
                actual_variant=actual[1],
                actual_color=actual[2],
                actual_cols=(),
                advertised_variant=advertised[0],
                advertised_color=advertised[1],
                source="internal",
            )
        },
    )


async def _answer(client, **kwargs) -> dict:
    response = await client.get(URL, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def _printer(body: dict, printer_id: int) -> dict:
    return next(p for p in body["printers"] if p["printer_id"] == printer_id)


class TestOnline:
    async def test_only_active_unarchived_connected_printers_are_online(self, async_client, printer_factory, farm):
        online = await printer_factory(name="online")
        inactive = await printer_factory(name="maintenance", is_active=False)
        archived = await printer_factory(name="retired", archived=True)
        offline = await printer_factory(name="offline")
        for printer in (online, inactive, archived):
            farm(printer, _state("X1C", trays=[_tray(0, "PLA", PLA_BASIC)]))

        body = await _answer(async_client)

        assert [p["printer_id"] for p in body["printers"]] == [online.id]
        assert offline.id not in {p["printer_id"] for p in body["printers"]}

    async def test_a_printer_states_its_model_what_it_accepts_and_its_nozzles(
        self, async_client, printer_factory, farm
    ):
        printer = await printer_factory(model="Bambu Lab X1E")
        farm(printer, _state("Bambu Lab X1E", trays=[], nozzle="0.6"))

        entry = _printer(await _answer(async_client), printer.id)

        assert entry["model"] == "X1E"
        assert entry["nozzle_diameters"] == [0.6]
        # the mirrored printer config: what an X1E accepts files of
        assert "X1C" in entry["accepts"]

    async def test_an_api_key_sees_only_its_printers(self, async_client, printer_factory, farm):
        mine, theirs = await printer_factory(name="mine"), await printer_factory(name="theirs")
        for printer in (mine, theirs):
            farm(printer, _state("X1C", trays=[_tray(0, "PLA", PLA_BASIC)]))
        created = await async_client.post(
            "/api/v1/api-keys/", json={"name": "slicer", "printer_ids": [mine.id], "can_manage_library": True}
        )
        assert created.status_code == 200, created.text

        body = await _answer(async_client, headers={"X-API-Key": created.json()["key"], "Authorization": ""})

        assert [p["printer_id"] for p in body["printers"]] == [mine.id]


class TestLoaded:
    async def test_a_family_names_its_presets_by_each_tiers_id(self, async_client, printer_factory, farm):
        printer = await printer_factory()
        farm(printer, _state("X1C", trays=[_tray(0, "PLA", PLA_BASIC)]))

        body = await _answer(async_client)

        assert _printer(body, printer.id)["filaments"] == [
            {"kind": "ams", "material": "PLA", "family_id": PLA_BASIC, "by_material": False}
        ]
        refs = body["families"][PLA_BASIC]
        assert refs["name"] == "Bambu PLA Basic"
        assert "Bambu PLA Basic @BBL X1C" in refs["standard"]
        assert "GFSA00" in refs["cloud"]

    async def test_a_slot_advertised_as_generic_answers_with_the_spool_it_holds(
        self, async_client, printer_factory, farm
    ):
        printer = await printer_factory()
        # the printer was TOLD Generic PLA in a canonical colour; the spool is Bambu PLA Basic
        farm(printer, _state("X1C", trays=[_tray(0, "PLA", GENERIC_PLA, "000000FF")]))
        _advertise(printer.id, 0, actual=("PLA", PLA_BASIC, "FF0000FF"), advertised=(GENERIC_PLA, "000000FF"))

        body = await _answer(async_client)

        assert _printer(body, printer.id)["filaments"] == [
            {"kind": "ams", "material": "PLA", "family_id": PLA_BASIC, "by_material": False}
        ]
        assert GENERIC_PLA not in body["families"]

    async def test_a_spool_without_a_family_is_matched_by_the_type_its_slot_states(
        self, async_client, printer_factory, farm
    ):
        printer = await printer_factory()
        # ASA-GF has no family: the slot borrows Generic ASA's profile and keeps the spool's type
        # ... and a slot whose type is not its profile's states that type, whatever profile it borrowed
        farm(
            printer,
            _state("X1C", trays=[_tray(0, "ASA-GF", GENERIC_ASA), _tray(1, "PETG", ""), _tray(2, "PLA-CF", PLA_BASIC)]),
        )

        filaments = _printer(await _answer(async_client), printer.id)["filaments"]

        assert filaments == [
            {"kind": "ams", "material": "ASA-GF", "family_id": GENERIC_ASA, "by_material": True},
            {"kind": "ams", "material": "PETG", "family_id": None, "by_material": True},
            {"kind": "ams", "material": "PLA-CF", "family_id": PLA_BASIC, "by_material": True},
        ]

    async def test_an_empty_slot_does_not_count_and_an_external_holder_does(self, async_client, printer_factory, farm):
        printer = await printer_factory(model="A1")
        farm(
            printer,
            _state(
                "A1",
                trays=[_tray(0, "", ""), _tray(1, "PLA", PLA_BASIC)],
                external={"id": "254", "tray_type": "TPU", "tray_info_idx": "GFU99", "tray_color": "112233FF"},
            ),
        )

        filaments = _printer(await _answer(async_client), printer.id)["filaments"]

        assert [(f["kind"], f["material"]) for f in filaments] == [("ams", "PLA"), ("external", "TPU")]

    async def test_a_printer_without_an_ams_answers_with_its_external_holder_or_nothing(
        self, async_client, printer_factory, farm
    ):
        bare, fed = await printer_factory(model="A1"), await printer_factory(model="A1")
        farm(bare, _state("A1"))
        farm(fed, _state("A1", external={"id": "254", "tray_type": "PLA", "tray_info_idx": PLA_BASIC}))

        body = await _answer(async_client)

        assert _printer(body, bare.id)["filaments"] == []
        assert _printer(body, fed.id)["filaments"] == [
            {"kind": "external", "material": "PLA", "family_id": PLA_BASIC, "by_material": False}
        ]

    async def test_a_users_own_family_names_the_users_presets(self, async_client, db_session, printer_factory, farm):
        printer = await printer_factory()
        db_session.add(
            UserFilamentFamily(
                filament_id="P1234567",
                ecosystem="bambu",
                alias="My PETG",
                filament_type="PETG",
                origin="cloud_bambu",
            )
        )
        db_session.add(
            UserFilamentPreset(
                owner_user_id=None,
                ecosystem="bambu",
                source="cloud_bambu",
                cloud_id="PFUSabcdef",
                name="My PETG @Bambu Lab X1 Carbon 0.4 nozzle",
                family_filament_id="P1234567",
            )
        )
        await db_session.commit()
        farm(printer, _state("X1C", trays=[_tray(0, "PETG", "P1234567")]))

        body = await _answer(async_client)

        assert _printer(body, printer.id)["filaments"] == [
            {"kind": "ams", "material": "PETG", "family_id": "P1234567", "by_material": False}
        ]
        assert body["families"]["P1234567"]["cloud"] == ["PFUSabcdef"]
        assert body["families"]["P1234567"]["name"] == "My PETG"

    async def test_spoolman_mode_answers_from_the_slot_without_asking_spoolman(
        self, async_client, printer_factory, farm, monkeypatch
    ):
        from backend.app.services import spoolman

        async def _never(*_args, **_kwargs):
            raise AssertionError("the Slice dialog's filter must not call Spoolman")

        monkeypatch.setattr(spoolman, "get_spoolman_client", _never)
        saved = await async_client.put("/api/v1/settings/", json={"spoolman_enabled": True})
        assert saved.status_code == 200, saved.text
        printer = await printer_factory()
        # a Spoolman spool's slot: advertised Generic PLA, really its linked Bambu PLA Basic
        farm(printer, _state("X1C", trays=[_tray(0, "PLA", GENERIC_PLA, "000000FF")]))
        overlay.replace_printer(
            printer.id,
            {
                (0, 0): overlay.OverlayEntry(
                    actual_material="PLA",
                    actual_variant=PLA_BASIC,
                    actual_color="00FF00FF",
                    actual_cols=(),
                    advertised_variant=GENERIC_PLA,
                    advertised_color="000000FF",
                    source="spoolman",
                )
            },
        )

        filaments = _printer(await _answer(async_client), printer.id)["filaments"]

        assert filaments == [{"kind": "ams", "material": "PLA", "family_id": PLA_BASIC, "by_material": False}]


class TestRights:
    """The answer is printer facts — who is online and what is loaded — so it
    needs the right to read printers beside the right to slice."""

    async def test_a_user_who_may_slice_but_not_read_printers_is_refused(self, async_client, printer_factory, farm):
        from backend.tests.integration.test_external_folder_rights import _make_user

        farm(await printer_factory(name="p"), _state("X1C", trays=[_tray(0, "PLA", PLA_BASIC)]))
        slicer_only, _id = await _make_user(async_client, username="lf_slicer", permissions=["library:upload"])
        both, _id = await _make_user(async_client, username="lf_both", permissions=["library:upload", "printers:read"])

        refused = await async_client.get(URL, headers=slicer_only)
        allowed = await async_client.get(URL, headers=both)

        assert refused.status_code == 403, refused.text
        assert "printers" not in refused.json()
        assert allowed.status_code == 200, allowed.text
        assert len(allowed.json()["printers"]) == 1

    async def test_an_api_key_without_the_status_scope_is_refused(self, async_client, printer_factory, farm):
        farm(await printer_factory(name="p"), _state("X1C", trays=[_tray(0, "PLA", PLA_BASIC)]))
        created = await async_client.post(
            "/api/v1/api-keys/", json={"name": "library", "can_manage_library": True, "can_read_status": False}
        )
        assert created.status_code == 200, created.text

        response = await async_client.get(URL, headers={"X-API-Key": created.json()["key"], "Authorization": ""})

        assert response.status_code == 403, response.text
