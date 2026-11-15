"""Spoolman 0.27 native tags through the routes (upstream #3168, a8965b0e9 / 842dd9109).

The real ``SpoolmanClient`` against ``WeighingSpoolman``, whose tag routes
answer as a live 0.27.1 did — so "the archived spool kept its tag" and
"nothing was linked" are read off the Spoolman side.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.app.services.spoolman_tracking import get_fallback_spool_tag_for_slot
from backend.tests.unit.services.spoolman_weighing_fake import BASE_URL, WeighingSpoolman, client_for

SPOOLMAN = "/api/v1/spoolman"
INVENTORY = "/api/v1/spoolman/inventory"

TRAY_UUID = "0123456789ABCDEF0123456789ABCD01"
OTHER_UUID = "FEDCBA9876543210FEDCBA9876543210"
CHIP_UID = "D3E68F32"
PADDED_CHIP_UID = "D3E68F3200000100"


@pytest.fixture
async def spoolman_settings(db_session):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value=BASE_URL))
    await db_session.commit()


def _serve(fake: WeighingSpoolman):
    from backend.app.api.routes import spoolman_inventory

    fake.add_vendor(3, "Bambu Lab")
    fake.add_filament(7, vendor_id=3, name="PLA Basic", material="PLA", color_hex="FF0000", weight=1000)
    client = client_for(fake)
    spoolman_inventory._health_check_cache.clear()
    return (
        patch("backend.app.api.routes.spoolman_inventory.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman_inventory.init_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman.init_spoolman_client", AsyncMock(return_value=client)),
    )


@pytest.fixture
def spoolman(spoolman_settings):
    fake = WeighingSpoolman()
    patches = _serve(fake)
    with patches[0], patches[1], patches[2], patches[3]:
        yield fake


@pytest.fixture
def old_spoolman(spoolman_settings):
    """A Spoolman from before native tags."""
    fake = WeighingSpoolman(native_tags=False)
    patches = _serve(fake)
    with patches[0], patches[1], patches[2], patches[3]:
        yield fake


def _extra_tag(fake: WeighingSpoolman, spool_id: int) -> str:
    raw = (fake.spools[spool_id].get("extra") or {}).get("tag")
    return json.loads(raw) if raw else ""


def _tag_writes(fake: WeighingSpoolman) -> list[str]:
    """Requests that would change a native tag (the extra-field route ``/field/spool/tag`` is not one)."""
    return [e for e in fake.log if "/tag" in e and not e.startswith("GET") and "/field/" not in e]


@pytest.mark.asyncio
@pytest.mark.integration
class TestStatus:
    async def test_a_spoolman_with_native_tags_says_so(self, async_client: AsyncClient, spoolman):
        body = (await async_client.get(f"{SPOOLMAN}/status")).json()

        assert (body["connected"], body["native_tags"]) == (True, True)

    async def test_an_older_spoolman_does_not(self, async_client: AsyncClient, old_spoolman):
        body = (await async_client.get(f"{SPOOLMAN}/status")).json()

        assert (body["connected"], body["native_tags"]) == (True, False)


@pytest.mark.asyncio
@pytest.mark.integration
class TestLinkASpoolToASlotsTag:
    async def test_a_tray_uuid_is_written_to_both_places(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7)

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": TRAY_UUID.lower()})

        assert response.status_code == 200
        assert _extra_tag(spoolman, 42) == TRAY_UUID
        assert spoolman.tags_of(42) == [TRAY_UUID]
        assert spoolman.tags[TRAY_UUID]["format"] == "bambu"

    async def test_a_sixteen_character_value_stays_in_extra_tag_alone(self, async_client: AsyncClient, spoolman):
        # A slot's fallback id or the AMS's padded chip uid: not what a reader sees.
        spoolman.add_spool(42, 7)

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": PADDED_CHIP_UID})

        assert response.status_code == 200
        assert _extra_tag(spoolman, 42) == PADDED_CHIP_UID
        assert spoolman.tags_of(42) == []

    async def test_a_uuid_another_spool_holds_is_refused_naming_it(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7)
        spoolman.add_spool(42, 7)
        spoolman.tag_spool(41, TRAY_UUID)

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": TRAY_UUID})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert (detail["error"], detail["spool_id"]) == ("tag_already_linked", 41)
        assert _extra_tag(spoolman, 42) == ""
        assert spoolman.tags_of(41) == [TRAY_UUID]

    async def test_an_archived_holder_is_named_and_keeps_its_tag(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, archived=True)
        spoolman.add_spool(42, 7)
        spoolman.tag_spool(41, TRAY_UUID)

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": TRAY_UUID})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert (detail["error"], detail["spool_id"], detail["archived"]) == ("tag_already_linked", 41, True)
        assert "41" in detail["message"]
        assert _extra_tag(spoolman, 42) == ""
        assert spoolman.tags_of(41) == [TRAY_UUID]

    async def test_a_filament_holder_is_its_own_refusal(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7)
        spoolman.tag_filament(7, TRAY_UUID)

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": TRAY_UUID})

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "tag_linked_elsewhere"
        assert _extra_tag(spoolman, 42) == ""

    async def test_a_failed_extra_tag_write_takes_the_native_tag_back(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7)
        spoolman.field_status = 500  # "tag" cannot be registered, so Spoolman refuses the PATCH

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": TRAY_UUID})

        assert response.status_code == 502
        assert spoolman.tags_of(42) == []

    async def test_an_older_spoolman_links_as_before(self, async_client: AsyncClient, old_spoolman):
        old_spoolman.add_spool(42, 7)

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": TRAY_UUID})

        assert response.status_code == 200
        assert _extra_tag(old_spoolman, 42) == TRAY_UUID
        assert _tag_writes(old_spoolman) == []


@pytest.mark.asyncio
@pytest.mark.integration
class TestUnlinkAndClear:
    async def test_unlink_removes_extra_tag_and_every_native_tag(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, extra={"tag": json.dumps(TRAY_UUID)})
        spoolman.registered.add("tag")
        spoolman.tag_spool(42, TRAY_UUID, "bambu")
        spoolman.tag_spool(42, CHIP_UID, "bambu")

        response = await async_client.post(f"{SPOOLMAN}/spools/42/unlink")

        assert response.status_code == 200
        assert _extra_tag(spoolman, 42) == ""
        assert spoolman.tags_of(42) == []

    async def test_unlink_on_an_older_spoolman_asks_for_no_tag_route(self, async_client: AsyncClient, old_spoolman):
        old_spoolman.add_spool(42, 7, extra={"tag": json.dumps(TRAY_UUID)})
        old_spoolman.registered.add("tag")

        response = await async_client.post(f"{SPOOLMAN}/spools/42/unlink")

        assert response.status_code == 200
        assert _extra_tag(old_spoolman, 42) == ""
        assert _tag_writes(old_spoolman) == []

    async def test_clear_rfid_tag_removes_the_native_tags_too(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, extra={"tag": json.dumps(TRAY_UUID)})
        spoolman.registered.add("tag")
        spoolman.tag_spool(42, TRAY_UUID, "bambu")
        spoolman.tag_spool(42, CHIP_UID, "bambu")

        response = await async_client.patch(f"{INVENTORY}/spools/42", json={"tag_uid": None, "tray_uuid": None})

        assert response.status_code == 200
        body = response.json()
        assert (body["tag_uid"], body["tray_uuid"]) == (None, None)
        assert spoolman.tags_of(42) == []

    async def test_an_edit_that_does_not_clear_the_tag_leaves_them(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7)
        spoolman.tag_spool(42, TRAY_UUID, "bambu")

        response = await async_client.patch(f"{INVENTORY}/spools/42", json={"note": "top shelf"})

        assert response.status_code == 200
        assert response.json()["tray_uuid"] == TRAY_UUID
        assert spoolman.tags_of(42) == [TRAY_UUID]


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheTagRoute:
    async def test_both_identifiers_become_native_tags(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7)

        response = await async_client.patch(
            f"{INVENTORY}/spools/42/tag", json={"tray_uuid": TRAY_UUID, "tag_uid": CHIP_UID}
        )

        assert response.status_code == 200
        assert _extra_tag(spoolman, 42) == TRAY_UUID
        assert spoolman.tags_of(42) == sorted([TRAY_UUID, CHIP_UID])
        body = response.json()
        assert (body["tray_uuid"], body["tag_uid"]) == (TRAY_UUID, CHIP_UID)

    async def test_a_refused_second_identifier_leaves_the_spool_as_it_was(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, archived=True)
        spoolman.add_spool(42, 7)
        spoolman.tag_spool(41, CHIP_UID)

        response = await async_client.patch(
            f"{INVENTORY}/spools/42/tag", json={"tray_uuid": TRAY_UUID, "tag_uid": CHIP_UID}
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert (detail["error"], detail["field"], detail["spool_id"], detail["archived"]) == (
            "tag_already_linked",
            "tag_uid",
            41,
            True,
        )
        assert spoolman.tags_of(42) == []
        assert _extra_tag(spoolman, 42) == ""

    async def test_an_older_spoolman_writes_extra_tag_only(self, async_client: AsyncClient, old_spoolman):
        old_spoolman.add_spool(42, 7)

        response = await async_client.patch(f"{INVENTORY}/spools/42/tag", json={"tray_uuid": TRAY_UUID})

        assert response.status_code == 200
        assert _extra_tag(old_spoolman, 42) == TRAY_UUID
        assert _tag_writes(old_spoolman) == []

    async def test_the_amss_padded_chip_uid_is_stored_as_the_chips_own(self, async_client: AsyncClient, spoolman):
        """As the sync and the copy store it — the form a reader sees (review slots M-3)."""
        spoolman.add_spool(42, 7)

        response = await async_client.patch(
            f"{INVENTORY}/spools/42/tag", json={"tray_uuid": TRAY_UUID, "tag_uid": PADDED_CHIP_UID}
        )

        assert response.status_code == 200
        assert spoolman.tags_of(42) == sorted([TRAY_UUID, CHIP_UID])

    async def test_any_failure_after_the_native_link_takes_it_back(self, async_client: AsyncClient, spoolman):
        """Not only a translated Spoolman refusal (review slots M-7)."""
        from backend.app.services.spoolman import SpoolmanClient

        spoolman.add_spool(42, 7)

        with (
            patch.object(SpoolmanClient, "update_spool_full", AsyncMock(side_effect=RuntimeError("boom"))),
            pytest.raises(RuntimeError),
        ):
            await async_client.patch(f"{INVENTORY}/spools/42/tag", json={"tray_uuid": TRAY_UUID, "tag_uid": CHIP_UID})

        assert spoolman.tags_of(42) == []


@pytest.mark.asyncio
@pytest.mark.integration
class TestALinkWhoseSlotRowCannotBeSaved:
    async def test_the_native_tag_is_taken_back(self, async_client: AsyncClient, spoolman, printer_factory):
        """The route answers 500 and asks for a retry; the reel must not stay
        half linked in Spoolman meanwhile (review slots M-7)."""
        printer = await printer_factory()
        spoolman.add_spool(42, 7)

        with patch("backend.app.api.routes.spoolman.text", side_effect=RuntimeError("database is locked")):
            response = await async_client.post(
                f"{SPOOLMAN}/spools/42/link",
                json={"spool_tag": TRAY_UUID, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

        assert response.status_code == 500
        assert spoolman.tags_of(42) == []

    async def test_and_the_previous_reel_keeps_resolving_to_the_spool(
        self, async_client: AsyncClient, spoolman, printer_factory
    ):
        printer = await printer_factory()
        spoolman.add_spool(42, 7, extra={"tag": json.dumps(OTHER_UUID)})
        spoolman.registered.add("tag")
        spoolman.tag_spool(42, OTHER_UUID, "bambu")

        with patch("backend.app.api.routes.spoolman.text", side_effect=RuntimeError("database is locked")):
            response = await async_client.post(
                f"{SPOOLMAN}/spools/42/link",
                json={"spool_tag": TRAY_UUID, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

        assert response.status_code == 500
        assert spoolman.tags_of(42) == [OTHER_UUID]


@pytest.mark.asyncio
@pytest.mark.integration
class TestAManualRelinkForgetsThePreviousReel:
    """Spool 42 was linked to reel A by mistake and is corrected to reel B.

    With ``extra.tag`` alone the re-link replaced the value and reel A was a
    stranger again. Native tags accumulate, and the reader asks them first: reel
    A in an AMS still resolved to spool 42 and its prints were charged to it
    (review slots I-3).
    """

    def _linked_to_reel_a(self, spoolman: WeighingSpoolman) -> None:
        spoolman.add_spool(42, 7, extra={"tag": json.dumps(TRAY_UUID)})
        spoolman.registered.add("tag")
        spoolman.tag_spool(42, TRAY_UUID, "bambu")
        spoolman.tag_spool(42, CHIP_UID, "bambu")

    async def test_the_tag_route_drops_reel_a(self, async_client: AsyncClient, spoolman):
        self._linked_to_reel_a(spoolman)

        response = await async_client.patch(
            f"{INVENTORY}/spools/42/tag", json={"tray_uuid": OTHER_UUID, "tag_uid": "A1B2C3D4"}
        )

        assert response.status_code == 200
        assert spoolman.tags_of(42) == sorted([OTHER_UUID, "A1B2C3D4"])
        assert _extra_tag(spoolman, 42) == OTHER_UUID

    async def test_the_link_route_drops_reel_a(self, async_client: AsyncClient, spoolman):
        self._linked_to_reel_a(spoolman)

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": OTHER_UUID})

        assert response.status_code == 200
        assert spoolman.tags_of(42) == [OTHER_UUID]

    async def test_reel_a_can_then_be_linked_to_its_own_spool(self, async_client: AsyncClient, spoolman):
        self._linked_to_reel_a(spoolman)
        spoolman.add_spool(43, 7)
        await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": OTHER_UUID})

        response = await async_client.patch(
            f"{INVENTORY}/spools/43/tag", json={"tray_uuid": TRAY_UUID, "tag_uid": CHIP_UID}
        )

        assert response.status_code == 200
        assert spoolman.tags_of(43) == sorted([TRAY_UUID, CHIP_UID])

    async def test_a_previous_tag_that_was_no_bambu_reel_goes_too(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, extra={"tag": json.dumps("A1B2C3D4E5F6")})
        spoolman.registered.add("tag")
        spoolman.tag_spool(42, "A1B2C3D4E5F6")

        response = await async_client.patch(f"{INVENTORY}/spools/42/tag", json={"tag_uid": "0A0B0C0D0E0F"})

        assert response.status_code == 200
        assert spoolman.tags_of(42) == ["0A0B0C0D0E0F"]

    async def test_a_tag_another_tool_gave_the_spool_is_left(self, async_client: AsyncClient, spoolman):
        """Not the previous link and not a Bambu reel: nothing says BamDude put it there."""
        self._linked_to_reel_a(spoolman)
        spoolman.tag_spool(42, "0A0B0C0D0E0F", "ntag")

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": OTHER_UUID})

        assert response.status_code == 200
        assert spoolman.tags_of(42) == sorted([OTHER_UUID, "0A0B0C0D0E0F"])

    async def test_linking_the_same_reel_again_keeps_its_tags(self, async_client: AsyncClient, spoolman):
        self._linked_to_reel_a(spoolman)

        response = await async_client.patch(
            f"{INVENTORY}/spools/42/tag", json={"tray_uuid": TRAY_UUID, "tag_uid": CHIP_UID}
        )

        assert response.status_code == 200
        assert spoolman.tags_of(42) == sorted([TRAY_UUID, CHIP_UID])

    async def test_a_slots_generated_id_takes_no_reel_tag_away(self, async_client: AsyncClient, spoolman):
        """A reel put on a holder without a reader is linked by the slot's id: it is still that reel."""
        self._linked_to_reel_a(spoolman)

        response = await async_client.post(f"{SPOOLMAN}/spools/42/link", json={"spool_tag": "00AA11BB22CC33DD"})

        assert response.status_code == 200
        assert spoolman.tags_of(42) == sorted([TRAY_UUID, CHIP_UID])


@pytest.mark.asyncio
@pytest.mark.integration
class TestAddFromASlotWhoseReelAnArchivedSpoolHolds:
    async def test_the_refusal_names_the_holder(self, async_client: AsyncClient, spoolman, printer_factory):
        """Held through the chip uid alone: the click answered 500 "Spoolman did
        not create a spool from the slot" (review slots M-1)."""
        from types import SimpleNamespace

        printer = await printer_factory()
        spoolman.add_spool(42, 7, archived=True)
        spoolman.tag_spool(42, CHIP_UID)
        tray = {
            "id": 1,
            "tray_type": "PLA",
            "tray_sub_brands": "PLA Basic",
            "tray_color": "FF0000FF",
            "tray_uuid": TRAY_UUID,
            "tag_uid": PADDED_CHIP_UID,
            "tray_info_idx": "GFA00",
            "remain": 80,
        }
        state = SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [tray]}]})

        with patch("backend.app.api.routes.spoolman.printer_manager") as pm:
            pm.get_status.return_value = state
            response = await async_client.post(
                f"{SPOOLMAN}/spools/from-slot", json={"printer_id": printer.id, "ams_id": 0, "tray_id": 1}
            )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert (detail["error"], detail["spool_id"], detail["archived"]) == ("tag_already_linked", 42, True)
        assert list(spoolman.spools) == [42]


@pytest.mark.asyncio
@pytest.mark.integration
class TestLinkedMap:
    async def test_a_natively_tagged_spool_is_linked(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, extra={"tag": json.dumps(OTHER_UUID)})
        spoolman.add_spool(42, 7)
        spoolman.tag_spool(42, TRAY_UUID)

        linked = (await async_client.get(f"{SPOOLMAN}/spools/linked")).json()["linked"]

        assert {tag: row["id"] for tag, row in linked.items()} == {OTHER_UUID: 41, TRAY_UUID: 42}


def _tag(value: str) -> dict:
    return {"tag": json.dumps(value)}


@pytest.mark.asyncio
@pytest.mark.integration
class TestCopyExtraTagsToNativeTags:
    URL = f"{INVENTORY}/tags/migrate"

    async def test_the_preview_changes_nothing_and_the_run_does_what_it_said(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, extra=_tag(TRAY_UUID))
        spoolman.add_spool(42, 7, extra=_tag(PADDED_CHIP_UID))
        spoolman.add_spool(43, 7, extra=_tag("A1B2C3D4E5F6"))
        spoolman.add_spool(44, 7)

        preview = (await async_client.post(self.URL)).json()

        assert preview["dry_run"] is True
        assert preview["moved"] == [
            {"spool_id": 41, "tag": TRAY_UUID, "format": "bambu"},
            # The AMS's padding comes off: the chip's own 4 bytes are what a reader sees.
            {"spool_id": 42, "tag": CHIP_UID, "format": "bambu"},
            {"spool_id": 43, "tag": "A1B2C3D4E5F6", "format": None},
        ]
        assert spoolman.tags == {}

        done = (await async_client.post(self.URL, params={"dry_run": "false"})).json()

        assert {**done, "dry_run": True} == preview
        assert spoolman.tags_of(41) == [TRAY_UUID]
        assert spoolman.tags_of(42) == [CHIP_UID]
        assert spoolman.tags_of(43) == ["A1B2C3D4E5F6"]
        # extra.tag stays as it was.
        assert _extra_tag(spoolman, 42) == PADDED_CHIP_UID

    async def test_a_second_run_finds_everything_in_place(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, extra=_tag(TRAY_UUID))
        spoolman.add_spool(42, 7, extra=_tag(PADDED_CHIP_UID))
        await async_client.post(self.URL, params={"dry_run": "false"})
        writes = len(_tag_writes(spoolman))

        again = (await async_client.post(self.URL, params={"dry_run": "false"})).json()

        assert (again["moved"], again["already"], again["conflicts"]) == ([], 2, [])
        assert len(_tag_writes(spoolman)) == writes

    async def test_two_active_spools_sharing_a_tag_are_both_reported_and_neither_linked(
        self, async_client: AsyncClient, spoolman
    ):
        spoolman.add_spool(41, 7, extra=_tag(TRAY_UUID))
        spoolman.add_spool(42, 7, extra=_tag(TRAY_UUID))

        done = (await async_client.post(self.URL, params={"dry_run": "false"})).json()

        assert done["moved"] == []
        assert [(c["spool_id"], c["reason"], c["holder"]) for c in done["conflicts"]] == [
            (41, "shared_extra_tag", 42),
            (42, "shared_extra_tag", 41),
        ]
        assert spoolman.tags == {}

    async def test_a_tag_spoolman_holds_on_another_spool_is_reported(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7)
        spoolman.add_spool(42, 7, extra=_tag(TRAY_UUID))
        spoolman.tag_spool(41, TRAY_UUID)

        done = (await async_client.post(self.URL, params={"dry_run": "false"})).json()

        assert done["moved"] == []
        assert [(c["spool_id"], c["reason"], c["holder"], c["holder_archived"]) for c in done["conflicts"]] == [
            (42, "held_by_spool", 41, False)
        ]
        assert spoolman.tags_of(41) == [TRAY_UUID]

    async def test_an_archived_holder_keeps_its_tag(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, archived=True)
        spoolman.add_spool(42, 7, extra=_tag(TRAY_UUID))
        spoolman.tag_spool(41, TRAY_UUID)

        done = (await async_client.post(self.URL, params={"dry_run": "false"})).json()

        assert [(c["spool_id"], c["holder"], c["holder_archived"]) for c in done["conflicts"]] == [(42, 41, True)]
        assert spoolman.tags_of(41) == [TRAY_UUID]
        assert spoolman.tags_of(42) == []

    async def test_a_tag_a_filament_holds_is_reported(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, extra=_tag(TRAY_UUID))
        spoolman.tag_filament(7, TRAY_UUID)

        preview = (await async_client.post(self.URL)).json()

        assert [(c["spool_id"], c["reason"], c["holder_filament"]) for c in preview["conflicts"]] == [
            (42, "held_by_filament", 7)
        ]

    async def test_archived_spools_are_not_copied(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, archived=True, extra=_tag(TRAY_UUID))
        spoolman.add_spool(42, 7, extra=_tag(TRAY_UUID))  # the reel's active record

        done = (await async_client.post(self.URL, params={"dry_run": "false"})).json()

        assert (done["archived"], [m["spool_id"] for m in done["moved"]]) == (1, [42])
        assert spoolman.tags_of(41) == []

    async def test_a_slots_fallback_id_stays_behind(self, async_client: AsyncClient, spoolman, printer_factory):
        printer = await printer_factory()
        slot_id = get_fallback_spool_tag_for_slot(printer.serial_number, 0, 2)
        spoolman.add_spool(42, 7, extra=_tag(slot_id))

        done = (await async_client.post(self.URL, params={"dry_run": "false"})).json()

        assert (done["slot_ids"], done["moved"]) == (1, [])
        assert spoolman.tags == {}

    async def test_values_that_are_no_tag_are_left_alone(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, extra=_tag("not-a-tag"))
        spoolman.add_spool(42, 7, extra=_tag("0" * 32))
        spoolman.add_spool(43, 7, extra=_tag(""))

        done = (await async_client.post(self.URL, params={"dry_run": "false"})).json()

        assert (done["moved"], done["conflicts"]) == ([], [])
        assert spoolman.tags == {}

    async def test_an_older_spoolman_is_refused_with_a_code(self, async_client: AsyncClient, old_spoolman):
        response = await async_client.post(self.URL)

        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "spoolman_without_native_tags"
