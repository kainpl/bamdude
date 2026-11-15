"""Spoolman 0.27 native tags, on the client (upstream #3168, a8965b0e9 / 842dd9109).

Spoolman 0.27 links tags to spools itself (``spool.tags``). BamDude kept its
tag in ``extra.tag`` alone and read nothing else, so a spool tagged in
Spoolman's own UI, or by another tool, was a stranger to the AMS sync: the
reel in the slot got a SECOND spool. These tests drive the real client
against ``WeighingSpoolman``, whose tag routes answer as a live 0.27.1 did.
"""

import json
import logging

import httpx
import pytest

from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool
from backend.app.services import spoolman as spoolman_service
from backend.app.services.spoolman import AMSTray, ArchivedTagIndex, NativeTagConflict, SpoolmanClient
from backend.tests.unit.services.spoolman_weighing_fake import WeighingSpoolman, client_for

TRAY_UUID = "0123456789ABCDEF0123456789ABCD01"
CHIP_UID = "D3E68F32"
PADDED_CHIP_UID = "D3E68F3200000100"  # what the AMS reports for that chip


def _fake(**kwargs) -> WeighingSpoolman:
    fake = WeighingSpoolman(**kwargs)
    fake.add_vendor(3, "Bambu Lab")
    fake.add_filament(7, vendor_id=3, name="PLA Basic", material="PLA", color_hex="000000", weight=1000)
    return fake


def _tray(tray_uuid: str = TRAY_UUID, tag_uid: str = PADDED_CHIP_UID) -> AMSTray:
    return AMSTray(
        ams_id=0,
        tray_id=0,
        tray_type="PLA",
        tray_sub_brands="PLA Basic",
        tray_color="000000FF",
        remain=80,
        tag_uid=tag_uid,
        tray_uuid=tray_uuid,
        tray_info_idx="GFA00",
        tray_weight=1000,
    )


async def _sync(client: SpoolmanClient, tray: AMSTray, *, cached: bool = True) -> dict | None:
    cached_spools = await client.get_spools() if cached else None
    return await client.sync_ams_tray(tray, "X1C", disable_weight_sync=True, cached_spools=cached_spools)


def _tag_posts(fake: WeighingSpoolman) -> list[tuple[str, dict]]:
    return [(path, body) for method, path, body in fake.calls if method == "POST" and path.endswith("/tag")]


@pytest.mark.asyncio
class TestTheSyncRecognisesANativelyTaggedSpool:
    @pytest.mark.parametrize("cached", [True, False])
    async def test_a_spool_tagged_in_spoolman_is_not_duplicated(self, cached):
        fake = _fake()
        fake.add_spool(42, 7)
        fake.tag_spool(42, TRAY_UUID, "bambu")  # linked in Spoolman's own UI: no extra.tag

        result = await _sync(client_for(fake), _tray(), cached=cached)

        assert result["id"] == 42
        assert list(fake.spools) == [42]

    @pytest.mark.parametrize("cached", [True, False])
    async def test_a_spool_known_by_its_chip_uid_alone_is_found(self, cached):
        # A reader sees the chip's own 4 bytes; the AMS reports them padded.
        fake = _fake()
        fake.add_spool(42, 7)
        fake.tag_spool(42, "d3:e6:8f:32")

        result = await _sync(client_for(fake), _tray(), cached=cached)

        assert result["id"] == 42
        assert list(fake.spools) == [42]
        # ... and now it carries the tray UUID too, so the next lookup needs no chip.
        assert fake.tags_of(42) == sorted([TRAY_UUID, CHIP_UID])

    async def test_a_native_tag_wins_over_another_spools_extra_tag(self):
        fake = _fake()
        fake.add_spool(41, 7, extra={"tag": json.dumps(TRAY_UUID)})
        fake.add_spool(42, 7)
        fake.tag_spool(42, TRAY_UUID)
        client = client_for(fake)

        found = await client.find_spool_by_tag(TRAY_UUID.lower(), cached_spools=await client.get_spools())

        assert found["id"] == 42

    async def test_extra_tag_still_finds_a_spool_linked_before(self):
        fake = _fake()
        fake.add_spool(41, 7, extra={"tag": json.dumps(TRAY_UUID)})

        assert (await client_for(fake).find_spool_by_tag(TRAY_UUID))["id"] == 41

    async def test_an_archived_spools_native_tag_is_a_known_reel(self):
        fake = _fake()
        fake.add_spool(42, 7, archived=True)
        fake.tag_spool(42, TRAY_UUID)
        client = client_for(fake)

        index = ArchivedTagIndex(client)

        assert await index.holds_archived_reel(TRAY_UUID, await client.get_spools()) is True

    async def test_a_reel_whose_chip_an_archived_spool_holds_gets_no_new_spool(self, caplog):
        fake = _fake()
        fake.add_spool(42, 7, archived=True)
        fake.tag_spool(42, CHIP_UID)

        with caplog.at_level(logging.WARNING, logger="backend.app.services.spoolman"):
            result = await _sync(client_for(fake), _tray())

        assert result is None
        assert list(fake.spools) == [42]
        assert "archived spool 42" in caplog.text

    async def test_that_reel_is_not_asked_about_on_every_ams_update(self, caplog):
        fake = _fake()
        fake.add_spool(42, 7, archived=True)
        fake.tag_spool(42, CHIP_UID)
        client = client_for(fake)
        await _sync(client, _tray())
        listings = fake.calls.count(("GET", "/spool", {}))
        caplog.clear()

        with caplog.at_level(logging.WARNING, logger="backend.app.services.spoolman"):
            again = await _sync(client, _tray())

        assert again is None
        assert list(fake.spools) == [42]
        # Only the sync's own listing: Spoolman is not asked who holds the tag again.
        assert fake.calls.count(("GET", "/spool", {})) == listings + 1
        assert caplog.text == ""


@pytest.mark.asyncio
class TestTheSyncWritesNativeTags:
    async def test_a_new_spool_gets_both_identifiers_and_keeps_extra_tag(self):
        fake = _fake()

        created = await _sync(client_for(fake), _tray())

        assert json.loads(fake.spools[created["id"]]["extra"]["tag"]) == TRAY_UUID
        assert fake.tags_of(created["id"]) == sorted([TRAY_UUID, CHIP_UID])
        assert {body.get("format") for _, body in _tag_posts(fake)} == {"bambu"}

    async def test_a_spool_matched_by_extra_tag_gets_them_too(self):
        fake = _fake()
        fake.add_spool(42, 7, extra={"tag": json.dumps(TRAY_UUID)})

        await _sync(client_for(fake), _tray())

        assert fake.tags_of(42) == sorted([TRAY_UUID, CHIP_UID])

    async def test_tags_the_spool_already_carries_are_not_sent_again(self):
        fake = _fake()
        fake.add_spool(42, 7, extra={"tag": json.dumps(TRAY_UUID)})
        client = client_for(fake)
        await _sync(client, _tray())
        sent = len(_tag_posts(fake))

        await _sync(client, _tray())

        assert len(_tag_posts(fake)) == sent

    async def test_an_all_zero_identifier_is_never_sent(self):
        # Spoolman accepts an all-zero uid, and one spool would then own "no tag read".
        fake = _fake()
        tray = _tray(tray_uuid="", tag_uid="A1B2C3D4E5F60708")
        tray.tray_uuid = "0" * 32

        created = await _sync(client_for(fake), tray)

        assert fake.tags_of(created["id"]) == ["A1B2C3D4"]

    async def test_a_uid_held_elsewhere_is_logged_with_its_holder_and_makes_no_second_spool(self, caplog):
        fake = _fake()
        fake.tag_filament(7, TRAY_UUID)
        client = client_for(fake)

        with caplog.at_level(logging.WARNING, logger="backend.app.services.spoolman"):
            created = await _sync(client, _tray())
        assert f"{TRAY_UUID} belongs to filament 7" in caplog.text
        refused = len(_tag_posts(fake))

        again = await _sync(client, _tray())

        assert again["id"] == created["id"]
        assert len(fake.spools) == 1
        # Asked once: the sync runs on every AMS update.
        assert len(_tag_posts(fake)) == refused


@pytest.mark.asyncio
class TestAnOlderSpoolman:
    async def test_the_sync_behaves_as_it_always_did(self):
        fake = _fake(native_tags=False)
        client = client_for(fake)

        created = await _sync(client, _tray())
        again = await _sync(client, _tray())

        assert again["id"] == created["id"]
        assert json.loads(fake.spools[created["id"]]["extra"]["tag"]) == TRAY_UUID
        assert _tag_posts(fake) == []
        assert await client.has_tag_api() is False

    async def test_a_tag_query_that_lists_every_spool_is_not_believed(self):
        # An unknown query parameter is ignored: ?tag= answers the whole list.
        fake = _fake(native_tags=False)
        fake.add_spool(41, 7)
        fake.add_spool(42, 7)

        assert await client_for(fake).find_spool_by_native_tag(TRAY_UUID) is None

    async def test_a_lookup_without_a_cache_falls_back_to_extra_tag(self):
        fake = _fake(native_tags=False)
        fake.add_spool(41, 7)
        fake.add_spool(42, 7, extra={"tag": json.dumps(TRAY_UUID)})

        assert (await client_for(fake).find_spool_by_tag(TRAY_UUID))["id"] == 42


@pytest.mark.asyncio
class TestTheCapabilityProbe:
    def _probes(self, fake: WeighingSpoolman) -> int:
        return fake.log.count("GET /tag/reader")

    async def test_a_yes_is_asked_once(self):
        fake = _fake()
        client = client_for(fake)

        assert await client.has_tag_api() is True
        assert await client.has_tag_api() is True
        assert self._probes(fake) == 1

    async def test_a_no_is_asked_again_once_it_is_stale(self, monkeypatch):
        fake = _fake(native_tags=False)
        client = client_for(fake)
        clock = [1000.0]
        monkeypatch.setattr(spoolman_service.time, "monotonic", lambda: clock[0])

        assert await client.has_tag_api() is False
        assert await client.has_tag_api() is False
        assert self._probes(fake) == 1

        fake.native_tags = True  # Spoolman was upgraded underneath
        clock[0] += spoolman_service.TAG_API_RECHECK_SECONDS + 1
        assert await client.has_tag_api() is True

    async def test_an_unreachable_spoolman_is_not_remembered_as_a_no(self):
        fake = _fake()
        down = [True]

        async def handler(request: httpx.Request) -> httpx.Response:
            if down[0]:
                raise httpx.ConnectError("down")
            return await fake.handler(request)

        client = SpoolmanClient("http://spoolman.test:7912")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        assert await client.has_tag_api() is False
        down[0] = False
        assert await client.has_tag_api() is True

    async def test_a_client_for_another_url_asks_for_itself(self):
        old, new = _fake(native_tags=False), _fake()

        assert await client_for(old).has_tag_api() is False
        assert await client_for(new).has_tag_api() is True


@pytest.mark.asyncio
class TestAManualLink:
    async def test_both_identifiers_are_linked(self):
        fake = _fake()
        fake.add_spool(42, 7)

        added = await client_for(fake).link_native_tags(42, [TRAY_UUID.lower(), CHIP_UID], "bambu")

        assert added == [TRAY_UUID, CHIP_UID]
        assert fake.tags_of(42) == sorted([TRAY_UUID, CHIP_UID])

    async def test_a_uid_another_spool_holds_is_refused_naming_it(self):
        fake = _fake()
        fake.add_spool(41, 7)
        fake.add_spool(42, 7)
        fake.tag_spool(41, TRAY_UUID)

        with pytest.raises(NativeTagConflict) as refusal:
            await client_for(fake).link_native_tags(42, [TRAY_UUID], "bambu")

        assert (refusal.value.spool_id, refusal.value.archived) == (41, False)
        assert fake.tags_of(41) == [TRAY_UUID]

    async def test_an_archived_holder_is_refused_too_and_keeps_its_tag(self):
        fake = _fake()
        fake.add_spool(41, 7, archived=True)
        fake.add_spool(42, 7)
        fake.tag_spool(41, TRAY_UUID)

        with pytest.raises(NativeTagConflict) as refusal:
            await client_for(fake).link_native_tags(42, [TRAY_UUID], "bambu")

        assert (refusal.value.spool_id, refusal.value.archived) == (41, True)
        assert fake.tags_of(41) == [TRAY_UUID]

    async def test_a_filament_holder_is_named_as_one(self):
        fake = _fake()
        fake.add_spool(42, 7)
        fake.tag_filament(7, CHIP_UID)

        with pytest.raises(NativeTagConflict) as refusal:
            await client_for(fake).link_native_tags(42, [CHIP_UID])

        assert (refusal.value.spool_id, refusal.value.filament_id) == (None, 7)

    async def test_a_refused_second_identifier_takes_the_first_back(self):
        fake = _fake()
        fake.add_spool(41, 7)
        fake.add_spool(42, 7)
        fake.tag_spool(41, CHIP_UID)

        with pytest.raises(NativeTagConflict):
            await client_for(fake).link_native_tags(42, [TRAY_UUID, CHIP_UID], "bambu")

        assert fake.tags_of(42) == []

    async def test_a_tag_the_spool_had_before_is_not_taken_back(self):
        fake = _fake()
        fake.add_spool(41, 7)
        fake.add_spool(42, 7)
        fake.tag_spool(42, TRAY_UUID)
        fake.tag_spool(41, CHIP_UID)

        with pytest.raises(NativeTagConflict):
            await client_for(fake).link_native_tags(42, [TRAY_UUID, CHIP_UID], "bambu")

        assert fake.tags_of(42) == [TRAY_UUID]

    async def test_an_older_spoolman_links_nothing_and_refuses_nothing(self):
        fake = _fake(native_tags=False)
        fake.add_spool(42, 7)

        assert await client_for(fake).link_native_tags(42, [TRAY_UUID], "bambu") == []


@pytest.mark.asyncio
class TestUnlink:
    async def test_every_native_tag_goes(self):
        fake = _fake()
        fake.add_spool(42, 7)
        fake.tag_spool(42, TRAY_UUID)
        fake.tag_spool(42, CHIP_UID)
        client = client_for(fake)

        await client.unlink_all_native_tags(await client.get_spool(42))

        assert fake.tags_of(42) == []

    async def test_a_spool_without_tags_costs_no_request(self):
        fake = _fake(native_tags=False)
        fake.add_spool(42, 7)
        client = client_for(fake)
        spool = await client.get_spool(42)
        fake.log.clear()

        await client.unlink_all_native_tags(spool)

        assert fake.log == []


class TestTheMapper:
    def _spool(self, **fields) -> dict:
        return {
            "id": 7,
            "filament": {"id": 99, "name": "PLA Black", "material": "PLA", "color_hex": "000000", "weight": 1000},
            "extra": {},
            "registered": "2026-01-01T00:00:00Z",
            **fields,
        }

    def test_native_tags_show_as_the_spools_identifiers(self):
        mapped = _map_spoolman_spool(self._spool(tags=[{"uid": TRAY_UUID, "format": "bambu"}, {"uid": CHIP_UID}]))

        assert (mapped["tray_uuid"], mapped["tag_uid"]) == (TRAY_UUID, CHIP_UID)

    def test_extra_tag_keeps_its_place_and_a_native_tag_fills_the_other(self):
        mapped = _map_spoolman_spool(self._spool(extra={"tag": json.dumps(TRAY_UUID)}, tags=[{"uid": CHIP_UID}]))

        assert (mapped["tray_uuid"], mapped["tag_uid"]) == (TRAY_UUID, CHIP_UID)

    def test_a_spool_from_an_older_spoolman_reads_as_before(self):
        mapped = _map_spoolman_spool(self._spool(extra={"tag": json.dumps(PADDED_CHIP_UID)}))

        assert (mapped["tray_uuid"], mapped["tag_uid"]) == (None, PADDED_CHIP_UID)
