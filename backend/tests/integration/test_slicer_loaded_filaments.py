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
from sqlalchemy import select, update

from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.local_preset import LocalPreset
from backend.app.models.user import User
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


class TestFarmPrinters:
    """D4 (spec slicing-setup r5): every printer that is not archived — online,
    paused, offline or in maintenance — names its model for "Only models of my
    farm"; nozzles come from a live snapshot, else [] (never an assumed 0.4)."""

    async def test_farm_printers_lists_offline_paused_and_maintenance(self, async_client, printer_factory, farm):
        online = await printer_factory(name="online")
        maintenance = await printer_factory(name="maintenance", is_active=False)
        offline = await printer_factory(name="offline")
        for printer in (online, maintenance):
            farm(printer, _state("X1C", trays=[]))

        body = await _answer(async_client)

        assert {p["printer_id"] for p in body["farm_printers"]} == {online.id, maintenance.id, offline.id}
        assert [p["printer_id"] for p in body["printers"]] == [online.id]

    async def test_farm_printers_skips_archived(self, async_client, printer_factory, farm):
        kept = await printer_factory(name="kept")
        archived = await printer_factory(name="retired", archived=True)

        body = await _answer(async_client)

        ids = {p["printer_id"] for p in body["farm_printers"]}
        assert kept.id in ids and archived.id not in ids

    async def test_farm_printers_respects_key_scope_none_empty_and_cut(self, async_client, printer_factory, farm):
        mine, theirs = await printer_factory(name="mine"), await printer_factory(name="theirs")
        for printer in (mine, theirs):
            farm(printer, _state("X1C", trays=[_tray(0, "PLA", PLA_BASIC)]))
        scoped = await async_client.post(
            "/api/v1/api-keys/", json={"name": "one", "printer_ids": [mine.id], "can_manage_library": True}
        )
        none = await async_client.post(
            "/api/v1/api-keys/", json={"name": "none", "printer_ids": [], "can_manage_library": True}
        )
        assert scoped.status_code == 200 and none.status_code == 200

        body = await _answer(async_client, headers={"X-API-Key": scoped.json()["key"], "Authorization": ""})
        assert [p["printer_id"] for p in body["farm_printers"]] == [mine.id]
        assert theirs.id not in {p["printer_id"] for p in body["printers"]}

        empty = await _answer(async_client, headers={"X-API-Key": none.json()["key"], "Authorization": ""})
        assert empty["farm_printers"] == [] and empty["printers"] == []

    async def test_farm_printer_without_a_snapshot_has_no_nozzles(self, async_client, printer_factory, farm):
        offline = await printer_factory(name="offline", model="X1C")

        entry = next(p for p in (await _answer(async_client))["farm_printers"] if p["printer_id"] == offline.id)

        assert entry["model"] == "X1C"
        assert entry["nozzle_diameters"] == []

    async def test_unarchived_offline_printer_is_listed(self, async_client, printer_factory, farm, monkeypatch):
        # Unarchive connects for real and the address is unreachable: do not wait out the connect confirmation.
        monkeypatch.setattr("backend.app.services.printer_manager.CONNECT_CONFIRM_SECONDS", 0.2)
        printer = await printer_factory(name="back", archived=True)
        assert printer.id not in {p["printer_id"] for p in (await _answer(async_client))["farm_printers"]}

        restored = await async_client.post(f"/api/v1/printers/{printer.id}/unarchive")
        assert restored.status_code == 200, restored.text

        assert printer.id in {p["printer_id"] for p in (await _answer(async_client))["farm_printers"]}


class TestLoaded:
    async def test_a_family_names_its_presets_by_each_tiers_id(self, async_client, db_session, printer_factory, farm):
        # The Bambu Cloud ids need the caller's right AND their own link (§8.4).
        await _link(db_session, (await _user_named(db_session, "test_admin")).id)
        await db_session.commit()
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

    async def test_shared_bases_give_exact_ids_and_no_alias(self, async_client, db_session, printer_factory, farm):
        """Spec slicing-setup §8.3: GFSR99 / GFSR99_10 are Generic TPU, the other
        GFSR99_NN Generic EVA. Each family names exact ids only; the shared base
        is an alias of neither, an unambiguous one (GFSA00) is PLA Basic's."""
        await _link(db_session, (await _user_named(db_session, "test_admin")).id)
        await db_session.commit()
        printer = await printer_factory()
        trays = [_tray(0, "TPU", "GFU99"), _tray(1, "EVA", "GFR99"), _tray(2, "PLA", PLA_BASIC)]
        farm(printer, _state("X1C", trays=trays))

        families = (await _answer(async_client))["families"]

        tpu, eva = families["GFU99"], families["GFR99"]
        assert {"GFSR99", "GFSR99_10"} <= set(tpu["cloud"]) and "GFSR99_01" not in tpu["cloud"]
        assert "GFSR99_01" in eva["cloud"] and not {"GFSR99", "GFSR99_10"} & set(eva["cloud"])
        assert "GFSR99" not in tpu["cloud_aliases"] and "GFSR99" not in eva["cloud_aliases"]
        assert "GFSA00" in families[PLA_BASIC]["cloud_aliases"]

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


# --- Who sees which family refs (spec slicing-setup r5 §8.4, D10) ----------
#
# Recognition and the by_material basis come first and do not depend on what
# the caller may see. Then each cloud bucket is authorised per provider from
# local state only — the right, the caller's own stored credentials, one
# consistent credential snapshot — and re-checked after the mirror rows were
# read. Private ids (a mirror's cloud id, a push anchor) are the caller's own
# only; a custom family's name comes from a ref the caller may see.

FAM = "P7654321"  # a custom family
FAM2 = "P1111111"  # a custom family with nothing but A's private refs


async def _user_named(db, username: str) -> User:
    return await db.scalar(select(User).where(User.username == username))


async def _link(db, user_id: int, *, bambu: bool = True, orca: bool = True) -> None:
    values: dict = {}
    if bambu:
        values["cloud_token"] = "bambu-token"
    if orca:
        values["orca_cloud_token"] = "orca-token"
    await db.execute(update(User).where(User.id == user_id).values(**values))


async def _disconnect(db, user_id: int, provider: str, *, keep_token: bool = False) -> None:
    """What a disconnect or relink does to the account row: the generation moves."""
    column = getattr(User, f"{provider}_cloud_generation")
    values: dict = {column.key: column + 1}
    if not keep_token:
        values["cloud_token" if provider == "bambu" else "orca_cloud_token"] = None
    await db.execute(
        update(User).where(User.id == user_id).values(**values).execution_options(synchronize_session=False)
    )


@pytest.fixture
async def refs_world(async_client, db_session, printer_factory, farm):
    """A (the admin) and B, both with both rights and both links; one printer
    with the custom families and Bambu PLA Basic loaded."""
    from backend.tests.integration.test_external_folder_rights import _make_user

    b_headers, b_id = await _make_user(
        async_client,
        username="refs_b",
        permissions=["library:upload", "printers:read", "cloud:auth", "orca_cloud:auth"],
    )
    a = await _user_named(db_session, "test_admin")
    await _link(db_session, a.id)
    await _link(db_session, b_id)
    db_session.add_all(
        [
            UserFilamentFamily(
                filament_id=FAM, ecosystem="bambu", alias="Global alias", filament_type="PETG", origin="cloud_bambu"
            ),
            UserFilamentFamily(
                filament_id=FAM2, ecosystem="bambu", alias="Hidden alias", filament_type="PETG", origin="cloud_bambu"
            ),
        ]
    )
    shared = LocalPreset(name="Shared PETG local", preset_type="filament", setting="{}")
    lab = LocalPreset(name="Lab PETG", preset_type="filament", setting="{}")
    db_session.add_all([shared, lab])
    await db_session.flush()
    mirror = {"ecosystem": "bambu", "family_filament_id": FAM}
    db_session.add_all(
        [
            UserFilamentPreset(
                owner_user_id=a.id, source="cloud_bambu", cloud_id="PFUSmineA", name="A's PETG", **mirror
            ),
            UserFilamentPreset(
                owner_user_id=a.id,
                source="cloud_orca",
                cloud_id="orcaMineA",
                name="A's Orca PETG",
                **{**mirror, "ecosystem": "orca"},
            ),
            UserFilamentPreset(
                owner_user_id=a.id,
                source="local",
                local_preset_id=shared.id,
                name="Shared PETG local",
                pushed_cloud_id="PFUSpushA",
                bambu_pushed_owner_identity=a.account_identity,
                orca_pushed_profile_id="orcaPushA",
                orca_pushed_owner_identity=a.account_identity,
                **mirror,
            ),
            UserFilamentPreset(
                owner_user_id=None, source="cloud_bambu", cloud_id="PFUSnobody", name="Nobody's PETG", **mirror
            ),
            UserFilamentPreset(owner_user_id=None, source="local", local_preset_id=lab.id, name="Lab PETG", **mirror),
            UserFilamentPreset(
                owner_user_id=a.id,
                source="cloud_bambu",
                cloud_id="PFUSonlyA",
                name="A's hidden",
                ecosystem="bambu",
                family_filament_id=FAM2,
            ),
        ]
    )
    await db_session.commit()
    printer = await printer_factory()
    trays = [_tray(0, "PETG", FAM), _tray(1, "PETG", FAM2), _tray(2, "PLA", PLA_BASIC)]
    farm(printer, _state("X1C", trays=trays))
    return SimpleNamespace(a=a, b_id=b_id, b=b_headers, shared=shared, lab=lab, printer=printer)


def _empty_bambu(body: dict) -> None:
    pla = body["families"][PLA_BASIC]
    assert pla["cloud"] == [] and pla["cloud_aliases"] == [], pla
    assert "Bambu PLA Basic @BBL X1C" in pla["standard"]
    assert "PFUSmineA" not in body["families"][FAM]["cloud"] and "PFUSpushA" not in body["families"][FAM]["cloud"]


def _full_bambu(body: dict) -> None:
    pla = body["families"][PLA_BASIC]
    assert "GFSA00" in pla["cloud"] and "GFSA00" in pla["cloud_aliases"], pla


def _local_name(body: dict, world) -> None:
    fam = body["families"][FAM]
    assert set(fam["local"]) == {str(world.shared.id), str(world.lab.id)}
    assert fam["name"] == "Shared PETG local"  # the lowest visible local preset id


def _after_mirror_read(monkeypatch, action):
    """Run ``action(db)`` right after the mirror rows were read."""
    from backend.app.services import slice_loaded_filaments as lf

    original = lf._read_mirrors

    async def wrapped(db, family_ids):
        rows = await original(db, family_ids)
        await action(db)
        return rows

    monkeypatch.setattr(lf, "_read_mirrors", wrapped)


class TestFamilyRefVisibility:
    async def test_b_never_sees_a_push_anchor_of_a(self, async_client, refs_world):
        body = await _answer(async_client, headers=refs_world.b)
        fam = body["families"][FAM]
        assert fam["cloud"] == [] and fam["orca_cloud"] == []
        _local_name(body, refs_world)
        _full_bambu(body)  # B's own access shows the system ids

    async def test_owner_sees_their_own_cloud_ref(self, async_client, refs_world):
        fam = (await _answer(async_client))["families"][FAM]
        assert fam["cloud"] == ["PFUSmineA", "PFUSpushA"]
        assert fam["orca_cloud"] == ["orcaMineA", "orcaPushA"]
        assert fam["name"] == "A's PETG"

    async def test_ownerless_mirror_gives_its_local_ref_only(self, async_client, refs_world):
        fam = (await _answer(async_client))["families"][FAM]
        assert "PFUSnobody" not in fam["cloud"]
        assert str(refs_world.lab.id) in fam["local"]

    async def test_no_provider_right_empties_the_whole_cloud_bucket(self, async_client, db_session, refs_world):
        from backend.tests.integration.test_external_folder_rights import _make_user

        c, c_id = await _make_user(async_client, username="refs_c", permissions=["library:upload", "printers:read"])
        await _link(db_session, c_id)
        await db_session.commit()
        body = await _answer(async_client, headers=c)
        _empty_bambu(body)
        assert body["families"][FAM]["orca_cloud"] == []
        _local_name(body, refs_world)

    async def test_right_without_stored_credentials_empties_the_bucket(self, async_client, db_session, refs_world):
        await db_session.execute(
            update(User).where(User.id == refs_world.b_id).values(cloud_token=None, orca_cloud_token=None)
        )
        await db_session.commit()
        body = await _answer(async_client, headers=refs_world.b)
        _empty_bambu(body)
        assert body["families"][FAM]["orca_cloud"] == []

    async def test_disconnect_during_the_first_read_empties_the_bucket(self, async_client, monkeypatch, refs_world):
        from backend.app.services import cloud_credentials

        original = cloud_credentials.snapshot
        done: list = []

        async def snapshot(db, user, provider, **kw):
            snap = await original(db, user, provider, **kw)
            if provider == "bambu" and not done:
                done.append(True)
                await _disconnect(db, user.id, "bambu")
            return snap

        monkeypatch.setattr(cloud_credentials, "snapshot", snapshot)
        body = await _answer(async_client)
        _empty_bambu(body)
        assert body["families"][FAM]["orca_cloud"] == ["orcaMineA", "orcaPushA"]
        assert body["families"][FAM]["name"] == "A's Orca PETG"

    async def test_disconnect_before_the_snapshot_with_a_stale_principal_object_empties_the_bucket(
        self, async_client, monkeypatch, refs_world
    ):
        from backend.app.services import cloud_credentials

        original = cloud_credentials.snapshot

        async def snapshot(db, user, provider, **kw):
            if provider == "bambu":
                # Core update: the request's principal object still carries its token.
                await _disconnect(db, user.id, "bambu")
                assert user.cloud_token == "bambu-token"
            return await original(db, user, provider, **kw)

        monkeypatch.setattr(cloud_credentials, "snapshot", snapshot)
        _empty_bambu(await _answer(async_client))

    @pytest.mark.parametrize("provider", ["bambu", "orca"])
    @pytest.mark.parametrize("conflicts", [1, 2])
    async def test_change_between_snapshot_and_reload_retries_once_then_none(
        self, async_client, monkeypatch, refs_world, provider, conflicts
    ):
        from backend.app.core import auth_lifetime

        original = auth_lifetime.validate_captured
        right = "cloud:auth" if provider == "bambu" else "orca_cloud:auth"
        seen: list = []

        async def validate(db, capture, **kw):
            if tuple(kw.get("required_permissions") or ()) == (right,) and len(seen) < conflicts:
                seen.append(True)
                await _disconnect(db, capture.user_id, provider, keep_token=True)
            return await original(db, capture, **kw)

        monkeypatch.setattr(auth_lifetime, "validate_captured", validate)
        fam = (await _answer(async_client))["families"][FAM]
        bucket = fam["cloud"] if provider == "bambu" else fam["orca_cloud"]
        if conflicts == 1:
            assert bucket, "one conflict retries and succeeds on the consistent state"
        else:
            assert bucket == [], "two in a row give no provider"

    async def test_key_scope_lost_between_gate_and_final_check_is_lost(self, async_client, monkeypatch, refs_world):
        key = await _cloud_key(async_client)

        async def drop_scope(db):
            await db.execute(update(APIKey).where(APIKey.name == "refs-cloud").values(can_access_cloud=False))

        _after_mirror_read(monkeypatch, drop_scope)
        body = await _answer(async_client, headers=key)
        _empty_bambu(body)
        assert body["families"][FAM]["orca_cloud"] == []
        _local_name(body, refs_world)

    async def test_jwt_authority_lost_between_gate_and_final_check_is_lost(self, async_client, monkeypatch, refs_world):
        async def end_sessions(db):
            await db.execute(
                update(User).where(User.id == refs_world.b_id).values(session_generation=User.session_generation + 1)
            )

        _after_mirror_read(monkeypatch, end_sessions)
        body = await _answer(async_client, headers=refs_world.b)
        _empty_bambu(body)
        _local_name(body, refs_world)

    async def test_disconnect_with_token_removed_after_the_gate_is_lost(self, async_client, monkeypatch, refs_world):
        async def disconnect(db):
            await _disconnect(db, refs_world.a.id, "bambu")

        _after_mirror_read(monkeypatch, disconnect)
        body = await _answer(async_client)
        _empty_bambu(body)
        assert body["families"][FAM]["name"] == "A's Orca PETG"

    async def test_relink_with_access_intact_is_generation_changed_and_keeps_system_refs(
        self, async_client, monkeypatch, refs_world
    ):
        async def relink(db):
            await _disconnect(db, refs_world.a.id, "bambu", keep_token=True)

        _after_mirror_read(monkeypatch, relink)
        body = await _answer(async_client)
        _full_bambu(body)
        assert body["families"][FAM]["cloud"] == []
        assert body["families"][FAM]["orca_cloud"] == ["orcaMineA", "orcaPushA"]

    async def test_disconnect_inside_the_second_gate_is_lost(self, async_client, monkeypatch, refs_world):
        from backend.app.core import auth_lifetime

        state = {"read": False, "struck": False}
        original = auth_lifetime.validate_captured

        async def validate(db, capture, **kw):
            if state["read"] and not state["struck"] and tuple(kw.get("required_permissions") or ()) == ("cloud:auth",):
                state["struck"] = True
                await _disconnect(db, capture.user_id, "bambu")
            return await original(db, capture, **kw)

        async def relink(db):
            await _disconnect(db, refs_world.a.id, "bambu", keep_token=True)
            state["read"] = True

        monkeypatch.setattr(auth_lifetime, "validate_captured", validate)
        _after_mirror_read(monkeypatch, relink)
        body = await _answer(async_client)
        assert state["struck"]
        _empty_bambu(body)

    async def test_inactive_account_after_the_first_gate_is_lost_and_the_rest_is_served(
        self, async_client, monkeypatch, refs_world
    ):
        async def deactivate(db):
            await db.execute(update(User).where(User.id == refs_world.b_id).values(is_active=False))

        _after_mirror_read(monkeypatch, deactivate)
        body = await _answer(async_client, headers=refs_world.b)
        assert _printer(body, refs_world.printer.id)["filaments"]
        _empty_bambu(body)
        assert body["families"][FAM]["orca_cloud"] == []
        _local_name(body, refs_world)

    async def test_authority_lost_empties_the_bucket_and_the_name(
        self, async_client, monkeypatch, db_session, refs_world
    ):
        from fastapi import HTTPException

        from backend.app.core import auth_lifetime

        async def refused(db, capture, **kw):
            if kw.get("required_permissions"):
                raise HTTPException(401, "refused")
            return await original(db, capture, **kw)

        original = auth_lifetime.validate_captured
        monkeypatch.setattr(auth_lifetime, "validate_captured", refused)
        jwt = await _answer(async_client)
        _empty_bambu(jwt)
        assert jwt["families"][FAM]["orca_cloud"] == []
        _local_name(jwt, refs_world)
        monkeypatch.setattr(auth_lifetime, "validate_captured", original)

        key = await _cloud_key(async_client)
        await db_session.execute(update(APIKey).where(APIKey.name == "refs-cloud").values(can_access_cloud=False))
        await db_session.commit()
        lost = await _answer(async_client, headers=key)
        _empty_bambu(lost)
        _local_name(lost, refs_world)

    async def test_key_loses_cloud_scope_between_reads_is_lost_not_generation_changed(
        self, async_client, monkeypatch, refs_world
    ):
        key = await _cloud_key(async_client)

        async def relink_and_drop(db):
            await _disconnect(db, refs_world.a.id, "bambu", keep_token=True)
            await db.execute(update(APIKey).where(APIKey.name == "refs-cloud").values(can_access_cloud=False))

        _after_mirror_read(monkeypatch, relink_and_drop)
        body = await _answer(async_client, headers=key)
        _empty_bambu(body)  # lost: the system ids go too, not only the private ones

    async def test_no_principal_answers_without_cloud_and_without_401(self, async_client, monkeypatch, refs_world):
        from backend.app.services import cloud_credentials

        async def never(*args, **kw):
            raise AssertionError("no principal: no snapshot")

        monkeypatch.setattr(cloud_credentials, "snapshot", never)
        created = await async_client.post(
            "/api/v1/api-keys/", json={"name": "refs-plain", "can_manage_library": True, "can_read_status": True}
        )
        body = await _answer(async_client, headers={"X-API-Key": created.json()["key"], "Authorization": ""})
        _empty_bambu(body)
        _local_name(body, refs_world)

    async def test_lost_empties_cloud_aliases_too(self, async_client, monkeypatch, refs_world):
        async def disconnect(db):
            await _disconnect(db, refs_world.a.id, "bambu")

        _after_mirror_read(monkeypatch, disconnect)
        assert (await _answer(async_client))["families"][PLA_BASIC]["cloud_aliases"] == []

    async def test_relink_after_the_mirror_read_drops_private_ids_and_name(self, async_client, monkeypatch, refs_world):
        async def relink_both(db):
            await _disconnect(db, refs_world.a.id, "bambu", keep_token=True)
            await _disconnect(db, refs_world.a.id, "orca", keep_token=True)

        _after_mirror_read(monkeypatch, relink_both)
        body = await _answer(async_client)
        fam = body["families"][FAM]
        assert fam["cloud"] == [] and fam["orca_cloud"] == []
        _local_name(body, refs_world)
        _full_bambu(body)  # system ids remain

    async def test_right_revoked_after_the_mirror_read_empties_the_bucket(self, async_client, monkeypatch, refs_world):
        async def revoke(db):
            group = await db.scalar(select(Group).where(Group.name == "xf_refs_b"))
            group.permissions = ["library:upload", "printers:read", "orca_cloud:auth"]
            await db.flush()

        _after_mirror_read(monkeypatch, revoke)
        body = await _answer(async_client, headers=refs_world.b)
        _empty_bambu(body)

    async def test_key_without_cloud_access_gets_no_cloud_refs(self, async_client, refs_world):
        created = await async_client.post(
            "/api/v1/api-keys/", json={"name": "refs-nocloud", "can_manage_library": True, "can_read_status": True}
        )
        body = await _answer(async_client, headers={"X-API-Key": created.json()["key"], "Authorization": ""})
        _empty_bambu(body)
        assert body["families"][FAM]["orca_cloud"] == []

    async def test_key_owner_without_provider_right_gets_no_cloud_refs(self, async_client, db_session, refs_world):
        from backend.tests.integration.test_external_folder_rights import _make_user

        c, c_id = await _make_user(
            async_client, username="refs_keyowner", permissions=["library:upload", "printers:read", "api_keys:create"]
        )
        await _link(db_session, c_id)
        await db_session.commit()
        created = await async_client.post(
            "/api/v1/api-keys/",
            headers=c,
            json={"name": "refs-c-key", "can_manage_library": True, "can_read_status": True, "can_access_cloud": True},
        )
        assert created.status_code == 200, created.text
        body = await _answer(async_client, headers={"X-API-Key": created.json()["key"], "Authorization": ""})
        _empty_bambu(body)
        assert body["families"][FAM]["orca_cloud"] == []

    async def test_key_with_owner_rights_and_link_sees_owner_refs_within_its_printer_scope(
        self, async_client, printer_factory, farm, refs_world
    ):
        other = await printer_factory(name="other")
        farm(other, _state("X1C", trays=[_tray(0, "PLA", PLA_BASIC)]))
        key = await _cloud_key(async_client, printer_ids=[refs_world.printer.id])
        body = await _answer(async_client, headers=key)
        assert [p["printer_id"] for p in body["printers"]] == [refs_world.printer.id]
        assert body["families"][FAM]["cloud"] == ["PFUSmineA", "PFUSpushA"]
        assert body["families"][FAM]["name"] == "A's PETG"
        _full_bambu(body)

    async def test_custom_name_only_from_a_visible_ref(self, async_client, refs_world):
        a = await _answer(async_client)
        b = await _answer(async_client, headers=refs_world.b)
        assert a["families"][FAM]["name"] == "A's PETG"
        assert b["families"][FAM]["name"] == "Shared PETG local"
        assert a["families"][FAM2]["name"] == "A's hidden"
        assert b["families"][FAM2]["name"] is None  # no visible ref, no name — never the stored alias

    async def test_known_family_without_visible_refs_matches_nothing(self, async_client, refs_world):
        body = await _answer(async_client, headers=refs_world.b)
        assert {"kind": "ams", "material": "PETG", "family_id": FAM2, "by_material": False} in _printer(
            body, refs_world.printer.id
        )["filaments"]
        fam2 = body["families"][FAM2]
        assert fam2["cloud"] == fam2["orca_cloud"] == fam2["local"] == fam2["standard"] == fam2["cloud_aliases"] == []

    async def test_generic_borrowed_unknown_keep_by_material(self, async_client, printer_factory, farm, refs_world):
        other = await printer_factory(name="mixed")
        # Generic PLA; a PLA Basic profile on a PETG slot (borrowed); an unknown family id.
        trays = [_tray(0, "PLA", GENERIC_PLA), _tray(1, "PETG", PLA_BASIC), _tray(2, "PETG", "ZZUNKNOWN")]
        farm(other, _state("X1C", trays=trays))
        for headers in ({}, refs_world.b):
            filaments = _printer(await _answer(async_client, headers=headers or None), other.id)["filaments"]
            assert {"kind": "ams", "material": "PLA", "family_id": GENERIC_PLA, "by_material": True} in filaments
            assert {"kind": "ams", "material": "PETG", "family_id": PLA_BASIC, "by_material": True} in filaments
            assert {"kind": "ams", "material": "PETG", "family_id": None, "by_material": True} in filaments


async def _cloud_key(async_client, **extra) -> dict:
    created = await async_client.post(
        "/api/v1/api-keys/",
        json={
            "name": "refs-cloud",
            "can_manage_library": True,
            "can_read_status": True,
            "can_access_cloud": True,
            **extra,
        },
    )
    assert created.status_code == 200, created.text
    return {"X-API-Key": created.json()["key"], "Authorization": ""}
