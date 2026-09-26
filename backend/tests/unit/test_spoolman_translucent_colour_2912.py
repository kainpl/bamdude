"""A clear spool stays clear on the way to Spoolman (upstream 73912d4f, #2912).

BamDude cut every colour to six characters before writing it to Spoolman, so a
clear spool (``00000000``) was stored as opaque black, and the read side greyed
out any eight-character value it met. The stored shape is now six characters for
an opaque spool — existing data stays byte-identical — and eight only when the
alpha byte says the filament is translucent; two colours match exactly when
storing them would produce the same value.
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool
from backend.app.services.spoolman import AMSTray, SpoolmanClient
from backend.app.utils.color_utils import color_match_key, spoolman_color_hex

MINIMAL_SPOOL = {
    "id": 1,
    "filament": {
        "material": "PLA",
        "name": "PLA Basic",
        "color_hex": "FF0000",
        "weight": 1000.0,
        "vendor": {"name": "Bambu Lab"},
    },
    "used_weight": 250.0,
    "archived": False,
    "registered": "2024-01-01T00:00:00Z",
}


class TestSpoolmanColorHex:
    """#2912 — what a colour is stored as in Spoolman's color_hex."""

    def test_opaque_value_stays_six_characters(self):
        """The common case must be byte-identical to what is already stored, or
        every opaque spool gets rewritten on its next touch."""
        assert spoolman_color_hex("FF0000FF") == "FF0000"

    def test_translucent_value_keeps_its_alpha(self):
        assert spoolman_color_hex("FF000080") == "FF000080"

    def test_fully_transparent_keeps_its_alpha(self):
        """The reported case: a clear spool reads as 00000000 and must not be
        stored as opaque black."""
        assert spoolman_color_hex("00000000") == "00000000"

    def test_six_character_input_passes_through(self):
        assert spoolman_color_hex("00FF00") == "00FF00"

    def test_normalises_case_and_hash_prefix(self):
        assert spoolman_color_hex("#ff000080") == "FF000080"

    def test_none_and_empty_return_none(self):
        assert spoolman_color_hex(None) is None
        assert spoolman_color_hex("") is None

    def test_short_value_passes_through_rather_than_being_padded(self):
        """A malformed value is reported as it is, not reshaped into something
        that looks valid."""
        assert spoolman_color_hex("FFF") == "FFF"


class TestColorMatchKey:
    """#2912 — two colours match exactly when storing them would give the same value."""

    def test_opaque_value_matches_its_six_character_twin(self):
        """The upgrade hazard: a user's existing filaments are all stored six
        characters. If an opaque 8-char value stopped matching them, the next AMS
        sync would mint a duplicate filament for every spool on the instance."""
        assert color_match_key("FF0000FF") == color_match_key("FF0000")

    def test_translucent_value_does_not_match_its_opaque_twin(self):
        """Both directions. A clear roll must not attach to the black filament,
        and — the case that only exists once 8-char values are storable — a black
        roll must not attach to a clear one and inherit its swatch and name."""
        assert color_match_key("00000000") != color_match_key("000000")
        assert color_match_key("00000000") != color_match_key("000000FF")

    def test_differs_when_the_rgb_differs(self):
        assert color_match_key("FF0000FF") != color_match_key("00FF00FF")

    def test_is_the_stored_shape(self):
        """Stated as an invariant because three separate comparisons rely on it."""
        for value in ("FF0000", "FF0000FF", "FF000080", "00000000"):
            assert color_match_key(value) == spoolman_color_hex(value)

    def test_normalises_case_and_hash_prefix(self):
        assert color_match_key("#ff0000") == "FF0000"

    def test_missing_value_is_empty_string(self):
        assert color_match_key(None) == ""
        assert color_match_key("") == ""


class TestTheReadSide:
    """``_map_spoolman_spool`` reads an eight-character colour back with its alpha."""

    def test_eight_char_color_hex_is_read_back_with_its_alpha(self):
        """#2912: 8-char color_hex is a value the write side stores on purpose for a
        translucent spool, so the read must return it rather than grey it out.

        This test previously asserted the opposite, on the premise that only 6-char
        hex was valid from Spoolman. Spoolman stores whatever it is given, and
        BamDude's own rgba fields advertise RRGGBBAA — the read was what turned a
        clear spool into neutral grey.
        """
        spool = {**MINIMAL_SPOOL, "filament": {**MINIMAL_SPOOL["filament"], "color_hex": "FF000080"}}
        result = _map_spoolman_spool(spool)
        assert result["rgba"] == "FF000080"

    def test_fully_transparent_color_hex_survives_the_read(self):
        """The reported case: a clear spool stored as 00000000 must not come back
        as opaque black."""
        spool = {**MINIMAL_SPOOL, "filament": {**MINIMAL_SPOOL["filament"], "color_hex": "00000000"}}
        result = _map_spoolman_spool(spool)
        assert result["rgba"] == "00000000"

    def test_six_char_color_hex_still_gains_the_opaque_alpha(self):
        """Existing data is 6-char and must keep round-tripping unchanged — the
        opaque byte is appended, not doubled onto an alpha that is already there."""
        spool = {**MINIMAL_SPOOL, "filament": {**MINIMAL_SPOOL["filament"], "color_hex": "FF0000"}}
        result = _map_spoolman_spool(spool)
        assert result["rgba"] == "FF0000FF"

    def test_seven_char_color_hex_falls_back(self):
        """Only 6 or 8 are valid lengths; a 7-char value is malformed and still
        greys out rather than being padded into something plausible."""
        spool = {**MINIMAL_SPOOL, "filament": {**MINIMAL_SPOOL["filament"], "color_hex": "FF0000F"}}
        result = _map_spoolman_spool(spool)
        assert result["rgba"] == "808080FF"


class TestColorHexAlphaHandling:
    """#2912 — a clear spool must not be stored as opaque black, and widening the
    stored value must not mint duplicates against inventories that hold six
    characters everywhere.
    """

    @pytest.fixture
    def client(self):
        return SpoolmanClient("http://localhost:7912")

    def _tray(self, tray_color: str) -> AMSTray:
        return AMSTray(
            ams_id=0,
            tray_id=0,
            tray_type="PLA",
            tray_sub_brands="PLA Basic",
            tray_color=tray_color,
            remain=100,
            tag_uid="",
            tray_uuid="A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4",
            tray_info_idx="GFA00",
            tray_weight=1000,
        )

    async def _posted_payload(self, client, color_hex: str) -> dict:
        """Run create_filament and return the JSON body it actually sent."""
        with patch.object(client, "_get_client") as mock_get_client:
            mock_http_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            mock_response.json = Mock(return_value={"id": 99})
            mock_http_client.post = AsyncMock(return_value=mock_response)
            mock_get_client.return_value = mock_http_client

            await client.create_filament(name="PLA Basic", material="PLA", color_hex=color_hex)

        return mock_http_client.post.call_args.kwargs["json"]

    @pytest.mark.asyncio
    async def test_create_filament_stores_alpha_for_a_translucent_spool(self, client):
        """create_filament is the chokepoint every create funnels through — it
        truncated to six characters unconditionally, which is what turned a clear
        spool into opaque black."""
        payload = await self._posted_payload(client, "00000000")
        assert payload["color_hex"] == "00000000"

    @pytest.mark.asyncio
    async def test_create_filament_keeps_an_opaque_spool_at_six(self, client):
        """Passing everything through would rewrite the color_hex of every opaque
        spool on its next touch. Existing data has to stay byte-identical."""
        payload = await self._posted_payload(client, "FF0000FF")
        assert payload["color_hex"] == "FF0000"

    @pytest.mark.asyncio
    async def test_clear_tray_creates_a_translucent_filament(self, client):
        """End-to-end through the AMS auto-create path with nothing to match."""
        with (
            patch.object(client, "ensure_bambu_vendor", AsyncMock(return_value=2)),
            patch.object(client, "get_filaments", AsyncMock(return_value=[])),
            patch.object(client, "get_external_filaments", AsyncMock(return_value=[])),
            patch.object(client, "create_filament", AsyncMock(return_value={"id": 99})) as mock_create,
        ):
            await client._find_or_create_filament(self._tray("00000000"))

        assert mock_create.call_args.kwargs["color_hex"] == "00000000"

    @pytest.mark.asyncio
    async def test_opaque_tray_still_matches_an_existing_six_char_filament(self, client):
        """The upgrade hazard neither the report nor the original patch mentioned.

        Every filament already in a user's Spoolman is stored six characters. If
        the match compared full strings, an 8-char tray colour would stop matching
        them and the next AMS sync would mint a duplicate filament for every spool
        on the instance. An opaque tray keys to six characters and still matches.
        """
        existing = {"id": 6, "name": "Black", "material": "PLA", "color_hex": "000000", "vendor_id": 2}
        with (
            patch.object(client, "ensure_bambu_vendor", AsyncMock(return_value=2)),
            patch.object(client, "get_filaments", AsyncMock(return_value=[existing])),
            patch.object(client, "get_external_filaments", AsyncMock()) as mock_external,
            patch.object(client, "create_filament", AsyncMock()) as mock_create,
        ):
            result = await client._find_or_create_filament(self._tray("000000FF"))

        assert result is existing
        mock_external.assert_not_called()
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_clear_tray_does_not_attach_to_the_black_filament(self, client):
        """A translucent tray keys to eight characters, so it does not match the
        opaque filament of the same RGB and gets its own record instead."""
        black = {"id": 6, "name": "Black", "material": "PLA", "color_hex": "000000", "vendor_id": 2}
        with (
            patch.object(client, "ensure_bambu_vendor", AsyncMock(return_value=2)),
            patch.object(client, "get_filaments", AsyncMock(return_value=[black])),
            patch.object(client, "get_external_filaments", AsyncMock(return_value=[])),
            patch.object(client, "create_filament", AsyncMock(return_value={"id": 99})) as mock_create,
        ):
            await client._find_or_create_filament(self._tray("00000000"))

        assert mock_create.call_args.kwargs["color_hex"] == "00000000"

    @pytest.mark.asyncio
    async def test_black_tray_does_not_attach_to_a_clear_filament(self, client):
        """The inverse direction, which only became possible once 8-char values
        were storable at all: without the alpha in the key, an opaque black roll
        would match the clear filament, then render as the transparency
        checkerboard and be named Clear. Whichever roll synced first would decide
        and the other would be mislabelled.
        """
        clear = {"id": 6, "name": "Clear", "material": "PLA", "color_hex": "00000000", "vendor_id": 2}
        with (
            patch.object(client, "ensure_bambu_vendor", AsyncMock(return_value=2)),
            patch.object(client, "get_filaments", AsyncMock(return_value=[clear])),
            patch.object(client, "get_external_filaments", AsyncMock(return_value=[])),
            patch.object(client, "create_filament", AsyncMock(return_value={"id": 99})) as mock_create,
        ):
            await client._find_or_create_filament(self._tray("000000FF"))

        assert mock_create.call_args.kwargs["color_hex"] == "000000"

    @pytest.mark.asyncio
    async def test_clear_tray_does_not_take_a_same_rgb_external_entry(self, client):
        """The reported path on a fresh Spoolman with the external library
        reachable. Candidates are built with the same key, so SpoolmanDB's opaque
        "PLA Basic Black" is no longer a candidate for a clear tray and the
        filament is created from the tray data with its alpha intact.
        """
        external = [
            {
                "id": "bambulab_pla_black_1000_175_n",
                "manufacturer": "Bambu Lab",
                "name": "PLA Basic Black",
                "material": "PLA",
                "color_hex": "000000",
            },
        ]
        with (
            patch.object(client, "ensure_bambu_vendor", AsyncMock(return_value=2)),
            patch.object(client, "get_filaments", AsyncMock(return_value=[])),
            patch.object(client, "get_external_filaments", AsyncMock(return_value=external)),
            patch.object(client, "create_filament", AsyncMock(return_value={"id": 99})) as mock_create,
        ):
            await client._find_or_create_filament(self._tray("00000000"))

        assert mock_create.call_args.kwargs["color_hex"] == "00000000"

    @pytest.mark.asyncio
    async def test_find_or_create_filament_creates_a_clear_filament_with_its_alpha(self, client):
        """The user-driven path, with nothing to match. There is no split to pin
        here: the key and the created value are the same string, and for a clear
        spool that string is eight characters."""
        with (
            patch.object(client, "find_or_create_vendor", AsyncMock(return_value=3)),
            patch.object(client, "get_filaments", AsyncMock(return_value=[])),
            patch.object(client, "create_filament", AsyncMock(return_value={"id": 99})) as mock_create,
        ):
            await client.find_or_create_filament(
                material="PLA",
                subtype="Basic",
                brand="Bambu Lab",
                color_hex="00000000",
                label_weight=1000,
            )

        assert mock_create.call_args.kwargs["color_hex"] == "00000000"

    @pytest.mark.asyncio
    async def test_find_or_create_filament_matches_an_existing_six_char_filament(self, client):
        """The upgrade guard on the *other* match loop.

        `test_opaque_tray_still_matches_an_existing_six_char_filament` pins it for
        the AMS path. The public `find_or_create_filament` has its own loop, and
        the non-BL RFID auto-create (spoolman.py, `_sync_tray_to_spoolman`) now
        hands it `tray.tray_color` whole where it used to hand over
        `tray.tray_color[:6]`. If the opaque fold ever came off this key, every
        non-Bambu RFID spool on an instance would mint a duplicate filament on the
        next sync and nothing would go red.
        """
        existing = {
            "id": 7,
            "name": "PLA Basic",
            "material": "PLA",
            "color_hex": "FF0000",
            "vendor": {"id": 3, "name": "Bambu Lab"},
        }
        with (
            patch.object(client, "find_or_create_vendor", AsyncMock(return_value=3)),
            patch.object(client, "get_filaments", AsyncMock(return_value=[existing])),
            patch.object(client, "create_filament", AsyncMock()) as mock_create,
        ):
            result = await client.find_or_create_filament(
                material="PLA",
                subtype="Basic",
                brand="Bambu Lab",
                color_hex="FF0000FF",
                label_weight=1000,
            )

        assert result == 7
        mock_create.assert_not_called()
