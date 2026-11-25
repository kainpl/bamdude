"""Tests for the unified slicer-presets dedup logic + URL resolver.

Pure module-level tests; live HTTP / DB paths are covered by the integration
tests in Phase 1.E once the slice routes themselves land.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.api.routes import slicer_presets as sp
from backend.app.api.routes.slicer_presets import (
    _empty_slots,
    _enrich_cloud_metadata,
    _parse_compatible_printers,
    _parse_filament_metadata,
    list_printer_models,
)
from backend.app.schemas.slicer_presets import UnifiedPreset


def _slots(**overrides) -> dict:
    base = _empty_slots()
    base.update(overrides)
    return base


class TestEnrichCloudMetadata:
    """No cross-tier dedup post-#1712 — every tier surfaces its full list so
    the user can pick any source. The tier order only drives auto-pick + group
    rendering, never hiding."""

    def test_same_name_in_all_tiers_appears_in_every_tier(self):
        """A name present in orca_cloud AND cloud AND local AND standard must
        come back in EACH tier — the order is used for auto-pick + rendering,
        not for hiding."""
        name = "A1 0.4 nozzle"
        orca_cloud = _slots(printer=[UnifiedPreset(id="o1", name=name, source="orca_cloud")])
        cloud = _slots(printer=[UnifiedPreset(id="c1", name=name, source="cloud")])
        local = _slots(printer=[UnifiedPreset(id="42", name=name, source="local")])
        standard = _slots(printer=[UnifiedPreset(id=name, name=name, source="standard")])
        out_orca, out_cloud, out_local, out_standard = _enrich_cloud_metadata(orca_cloud, cloud, local, standard)
        assert [p.source for p in out_orca["printer"]] == ["orca_cloud"]
        assert [p.source for p in out_cloud["printer"]] == ["cloud"]
        assert [p.source for p in out_local["printer"]] == ["local"]
        assert [p.source for p in out_standard["printer"]] == ["standard"]

    def test_preserves_order_within_tier(self):
        cloud = _slots(
            printer=[
                UnifiedPreset(id="c1", name="Z-First", source="cloud"),
                UnifiedPreset(id="c2", name="A-Second", source="cloud"),
                UnifiedPreset(id="c3", name="M-Third", source="cloud"),
            ]
        )
        _oc, out_cloud, _ol, _os = _enrich_cloud_metadata(_empty_slots(), cloud, _empty_slots(), _empty_slots())
        assert [p.name for p in out_cloud["printer"]] == ["Z-First", "A-Second", "M-Third"]

    def test_disjoint_names_all_present(self):
        cloud = _slots(filament=[UnifiedPreset(id="c1", name="My PLA", source="cloud")])
        local = _slots(filament=[UnifiedPreset(id="3", name="Imported PETG", source="local")])
        standard = _slots(filament=[UnifiedPreset(id="Bambu PLA Basic", name="Bambu PLA Basic", source="standard")])
        _oc, out_cloud, out_local, out_standard = _enrich_cloud_metadata(_empty_slots(), cloud, local, standard)
        assert len(out_cloud["filament"]) == 1
        assert len(out_local["filament"]) == 1
        assert len(out_standard["filament"]) == 1


class TestFilamentMetadataMerge:
    def test_cloud_inherits_local_filament_metadata(self):
        """A Bambu Cloud entry without its own filament_type + filament_colour
        inherits them from a same-named local row. Cloud doesn't carry metadata
        (rate-limited detail endpoint), so without this merge the SliceModal's
        pre-pick loses match info for every preset the user has cloud-synced AND
        locally imported. Both entries still surface — no dedup."""
        cloud = _slots(filament=[UnifiedPreset(id="c", name="Bambu PLA Basic", source="cloud")])
        local = _slots(
            filament=[
                UnifiedPreset(
                    id="9",
                    name="Bambu PLA Basic",
                    source="local",
                    filament_type="PLA",
                    filament_colour="#00FF00",
                )
            ]
        )
        standard = _slots()
        _oc, out_cloud, out_local, _os = _enrich_cloud_metadata(_empty_slots(), cloud, local, standard)
        assert out_cloud["filament"][0].filament_type == "PLA"
        assert out_cloud["filament"][0].filament_colour == "#00FF00"
        # Local entry is untouched and still present.
        assert out_local["filament"][0].filament_type == "PLA"

    def test_cloud_keeps_own_metadata_when_present(self):
        cloud = _slots(
            filament=[
                UnifiedPreset(
                    id="c",
                    name="My Custom",
                    source="cloud",
                    filament_type="PETG",
                    filament_colour="#FF0000",
                )
            ]
        )
        local = _slots(
            filament=[
                UnifiedPreset(
                    id="9",
                    name="My Custom",
                    source="local",
                    filament_type="PLA",  # would conflict if we naively overwrote
                    filament_colour="#00FF00",
                )
            ]
        )
        _oc, out_cloud, _ol, _os = _enrich_cloud_metadata(_empty_slots(), cloud, local, _empty_slots())
        # Cloud's own non-None metadata MUST win — that's the user's actual
        # cloud preset content, even if it happens to share a name with a
        # local import.
        assert out_cloud["filament"][0].filament_type == "PETG"
        assert out_cloud["filament"][0].filament_colour == "#FF0000"


class TestFilamentMetadataParse:
    def test_array_first_value_extracted(self):
        out = _parse_filament_metadata('{"filament_type":["PLA","-"],"filament_colour":["#FF8800"]}')
        assert out == ("PLA", "#FF8800")

    def test_string_value_returned(self):
        out = _parse_filament_metadata('{"filament_type":"PLA"}')
        assert out == ("PLA", None)

    def test_corrupt_json_returns_none(self):
        out = _parse_filament_metadata("not json {{")
        assert out == (None, None)

    def test_non_dict_returns_none(self):
        out = _parse_filament_metadata("[1,2,3]")
        assert out == (None, None)

    def test_empty_returns_none(self):
        out = _parse_filament_metadata("")
        assert out == (None, None)

    def test_none_returns_none(self):
        out = _parse_filament_metadata(None)
        assert out == (None, None)


class TestParseCompatiblePrinters:
    """``compatible_printers`` exposed for local process / filament presets so
    the SliceModal can filter the dropdowns by the selected printer (#1325)."""

    def test_parses_json_array(self):
        raw = '["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab X1 0.4 nozzle"]'
        assert _parse_compatible_printers(raw) == [
            "Bambu Lab X1 Carbon 0.4 nozzle",
            "Bambu Lab X1 0.4 nozzle",
        ]

    def test_none_and_empty_return_none(self):
        assert _parse_compatible_printers(None) is None
        assert _parse_compatible_printers("") is None
        assert _parse_compatible_printers("[]") is None

    def test_malformed_json_returns_none(self):
        assert _parse_compatible_printers("not json") is None
        # A JSON value that isn't an array is treated as absent, not an error.
        assert _parse_compatible_printers('"a string"') is None

    def test_drops_non_string_and_blank_entries(self):
        assert _parse_compatible_printers('["X1C", 5, "", "  ", "A1"]') == [
            "X1C",
            "A1",
        ]


class TestListPrinterModels:
    """``GET /slicer/printer-models`` exposes ``PRINTER_MODEL_MAP`` so the
    frontend doesn't duplicate the Bambu model registry (#1325 follow-up)."""

    def test_returns_canonical_printer_model_map(self):
        from backend.app.utils.printer_models import PRINTER_MODEL_MAP

        result = list_printer_models()
        # Same shape - mapping from "Bambu Lab <model>" to short code.
        assert result == PRINTER_MODEL_MAP
        # Spot-check a few entries: the SliceModal name-fallback (#1325)
        # specifically depends on these resolving.
        assert result["Bambu Lab X1 Carbon"] == "X1C"
        assert result["Bambu Lab P2S"] == "P2S"
        assert result["Bambu Lab A1 mini"] == "A1 Mini"
        assert result["Bambu Lab H2D Pro"] == "H2D Pro"

    def test_returns_a_copy_not_the_module_dict(self):
        # A response handler must never hand out the live module-level dict —
        # accidental mutation by middleware / serialisers would silently
        # corrupt the registry for every subsequent request.
        from backend.app.utils.printer_models import PRINTER_MODEL_MAP

        result = list_printer_models()
        assert result is not PRINTER_MODEL_MAP


class TestFetchOrcaCloudPresets:
    """``_fetch_orca_cloud_presets`` mirrors the Bambu Cloud fetcher's status
    vocabulary (``ok`` / ``not_authenticated`` / ``expired`` / ``unreachable``)
    and the same permission-shortcut + caching behaviour. Tests pin the
    contract so a future bug in either fetcher doesn't silently desync them."""

    def _orca_creds(self, token: str | None = "tok") -> MagicMock:
        creds = MagicMock()
        creds.token = token
        return creds

    @pytest.mark.asyncio
    async def test_no_token_returns_not_authenticated(self):
        sp._orca_cloud_cache.clear()
        with patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=self._orca_creds(None))):
            user = MagicMock(id=1)
            user.has_permission = MagicMock(return_value=True)
            slots, status = await sp._fetch_orca_cloud_presets(MagicMock(), user)
        assert status == "not_authenticated"
        assert slots == {"printer": [], "process": [], "filament": []}

    @pytest.mark.asyncio
    async def test_user_without_orca_cloud_auth_returns_not_authenticated(self):
        """Defence-in-depth — a user lacking ORCA_CLOUD_AUTH must not see Orca
        presets even if their User row carries a stale token. The permission
        check must short-circuit ahead of the credentials read."""
        sp._orca_cloud_cache.clear()
        user = MagicMock(id=1)
        user.has_permission = MagicMock(return_value=False)
        with patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=self._orca_creds("tok"))) as load:
            slots, status = await sp._fetch_orca_cloud_presets(MagicMock(), user)
        assert status == "not_authenticated"
        assert slots["printer"] == []
        load.assert_not_called()

    @pytest.mark.asyncio
    async def test_auth_error_returns_expired(self):
        sp._orca_cloud_cache.clear()
        svc_mock = MagicMock()
        svc_mock.list_profiles = AsyncMock(side_effect=sp.OrcaCloudAuthError("expired"))
        svc_mock.close = AsyncMock()
        user = MagicMock(id=1)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=self._orca_creds("tok"))),
            patch.object(sp, "_build_orca_service", AsyncMock(return_value=svc_mock)),
        ):
            _slots, status = await sp._fetch_orca_cloud_presets(MagicMock(), user)
        assert status == "expired"
        svc_mock.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orca_error_returns_unreachable(self):
        sp._orca_cloud_cache.clear()
        svc_mock = MagicMock()
        svc_mock.list_profiles = AsyncMock(side_effect=sp.OrcaCloudError("net down"))
        svc_mock.close = AsyncMock()
        user = MagicMock(id=1)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=self._orca_creds("tok"))),
            patch.object(sp, "_build_orca_service", AsyncMock(return_value=svc_mock)),
        ):
            _slots, status = await sp._fetch_orca_cloud_presets(MagicMock(), user)
        assert status == "unreachable"

    @pytest.mark.asyncio
    async def test_happy_path_shapes_grouped_by_type(self):
        """Orca content.type values map onto Bambu Cloud's preset type vocab
        (``printer`` / ``print`` → ``process`` / ``filament``). Verify the
        full mapping by feeding one of each shape."""
        sp._orca_cloud_cache.clear()
        svc_mock = MagicMock()
        svc_mock.list_profiles = AsyncMock(
            return_value=[
                {"id": "m1", "name": "Orca X1C", "content": {"type": "printer"}},
                {"id": "p1", "name": "Orca 0.20mm", "content": {"type": "print"}},
                {
                    "id": "f1",
                    "name": "Orca PLA",
                    "content": {
                        "type": "filament",
                        "filament_type": ["PLA"],
                        "default_filament_colour": ["#000000"],
                    },
                },
            ]
        )
        svc_mock.close = AsyncMock()
        user = MagicMock(id=1)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=self._orca_creds("tok"))),
            patch.object(sp, "_build_orca_service", AsyncMock(return_value=svc_mock)),
        ):
            slots, status = await sp._fetch_orca_cloud_presets(MagicMock(), user)
        assert status == "ok"
        assert [p.name for p in slots["printer"]] == ["Orca X1C"]
        assert [p.name for p in slots["process"]] == ["Orca 0.20mm"]
        filament = slots["filament"]
        assert [p.name for p in filament] == ["Orca PLA"]
        # Inline metadata extracted from the content blob (Orca's sync_pull
        # returns full content, so unlike Bambu Cloud we don't need a second
        # per-preset fetch to enrich filament_type / filament_colour).
        assert filament[0].filament_type == "PLA"
        assert filament[0].filament_colour == "#000000"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_orca_call(self):
        """A second call within TTL must reuse the cached slots and NOT
        hit the Orca service again — same TTL as Bambu Cloud (5 min)."""
        sp._orca_cloud_cache.clear()
        svc_mock = MagicMock()
        svc_mock.list_profiles = AsyncMock(return_value=[])
        svc_mock.close = AsyncMock()
        user = MagicMock(id=1)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=self._orca_creds("tok"))),
            patch.object(sp, "_build_orca_service", AsyncMock(return_value=svc_mock)) as build,
        ):
            await sp._fetch_orca_cloud_presets(MagicMock(), user)
            await sp._fetch_orca_cloud_presets(MagicMock(), user)
        # Build is the cache miss signal — second call reused the cache.
        build.assert_awaited_once()


class TestOrcaCloudIniLists:
    """Orca Cloud can hand a list option over the way the slicer writes it to
    an INI file: ONE string of quoted items joined by ``;`` (upstream c4c5c7042,
    #3381). Read raw, the whole string is one "printer name"."""

    def _orca_creds(self) -> MagicMock:
        creds = MagicMock()
        creds.token = "tok"
        return creds

    @pytest.mark.asyncio
    async def test_quoted_lists_are_decoded_in_the_listing(self):
        sp._orca_cloud_cache.clear()
        svc_mock = MagicMock()
        svc_mock.list_profiles = AsyncMock(
            return_value=[
                {
                    "id": "f1",
                    "name": "SUNLU PLA Meta @Bambu Lab X1 Carbon 0.4 nozzle",
                    "content": {
                        "type": "filament",
                        "filament_type": '"PLA"',
                        "default_filament_colour": '""',
                        "compatible_printers": '"Bambu Lab X1 Carbon 0.4 nozzle";"Bambu Lab H2C 0.4 nozzle"',
                    },
                },
                {"id": "f2", "name": "Only quotes", "content": {"type": "filament", "compatible_printers": '""'}},
                {
                    "id": "f3",
                    "name": "Escaped",
                    "content": {
                        "type": "filament",
                        "default_filament_colour": '"#FF0000"',
                        "compatible_printers": '"Printer \\"A\\"";"Bambu Lab A1 0.4 nozzle"',
                    },
                },
                {
                    "id": "f4",
                    "name": "Text after a closing quote",
                    "content": {"type": "filament", "filament_type": "PLA", "compatible_printers": '"X1C" P1S'},
                },
                {
                    "id": "f5",
                    "name": "A real list",
                    "content": {"type": "filament", "compatible_printers": ["My X1C; tuned"]},
                },
            ]
        )
        svc_mock.close = AsyncMock()
        user = MagicMock(id=1)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=self._orca_creds())),
            patch.object(sp, "_build_orca_service", AsyncMock(return_value=svc_mock)),
        ):
            slots, _status = await sp._fetch_orca_cloud_presets(MagicMock(), user)
        sp._orca_cloud_cache.clear()

        meta, only_quotes, escaped, bad, real_list = slots["filament"]
        assert meta.compatible_printers == ["Bambu Lab X1 Carbon 0.4 nozzle", "Bambu Lab H2C 0.4 nozzle"]
        assert meta.filament_type == "PLA"
        assert meta.filament_colour is None
        assert only_quotes.compatible_printers is None
        assert escaped.compatible_printers == ['Printer "A"', "Bambu Lab A1 0.4 nozzle"]
        assert escaped.filament_colour == "#FF0000"
        # A value the slicer itself cannot read is "no data"; the rest survives.
        assert bad.compatible_printers is None
        assert bad.filament_type == "PLA"
        # A JSON list is never INI-encoded: a ";" in a name stays in the name.
        assert real_list.compatible_printers == ["My X1C; tuned"]

    def test_a_stored_preset_states_its_material_through_the_same_decoder(self):
        setting = '{"filament_type": "\\"PETG\\"", "filament_colour": "\\"#112233\\""}'
        assert _parse_filament_metadata(setting) == ("PETG", "#112233")


class TestPrinterPresetParent:
    """A printer preset saved under a name of its own names the preset it was
    saved from (upstream 3acc52d93, #3250): the slicer checks a process against
    THAT name, and the dialog has to as well.

    ⚠️ Only a preset that is not a system preset has such a parent. A system
    "... 0.6 nozzle" preset inherits the 0.4 one as a matter of file layout,
    and following that would offer 0.4 processes to a 0.6 nozzle."""

    @pytest.mark.asyncio
    async def test_local_user_preset_names_its_parent(self):
        rows = [
            MagicMock(
                id=1,
                preset_type="printer",
                inherits="Bambu Lab H2D 0.4 nozzle",
                setting='{"from": "User", "inherits": "Bambu Lab H2D 0.4 nozzle"}',
            ),
            MagicMock(
                id=2,
                preset_type="printer",
                inherits="Bambu Lab X1 Carbon 0.4 nozzle",
                setting='{"from": "system", "inherits": "Bambu Lab X1 Carbon 0.4 nozzle"}',
            ),
            MagicMock(id=3, preset_type="printer", inherits=None, setting="{}"),
            MagicMock(id=4, preset_type="printer", inherits="  ", setting="not json"),
        ]
        for row, name in zip(
            rows,
            ["Bambu Lab H2D 0.4 nozzle - Skew", "Bambu Lab X1 Carbon 0.6 nozzle", "Root", "Blank"],
            strict=True,
        ):
            row.name = name
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)

        slots = await sp._fetch_local_presets(db)

        assert [p.inherits for p in slots["printer"]] == ["Bambu Lab H2D 0.4 nozzle", None, None, None]

    @pytest.mark.asyncio
    async def test_orca_cloud_printer_preset_names_its_parent(self):
        sp._orca_cloud_cache.clear()
        svc_mock = MagicMock()
        svc_mock.list_profiles = AsyncMock(
            return_value=[
                {
                    "id": "m1",
                    "name": "Bambu Lab H2D 0.4 nozzle - Skew",
                    "content": {"type": "printer", "from": "User", "inherits": " Bambu Lab H2D 0.4 nozzle "},
                },
                {"id": "m2", "name": "No parent", "content": {"type": "printer", "inherits": ""}},
                {"id": "m3", "name": "Not text", "content": {"type": "printer", "inherits": ["x"]}},
                {
                    "id": "m4",
                    "name": "Bambu Lab A1 0.6 nozzle",
                    "content": {"type": "printer", "from": "system", "inherits": "Bambu Lab A1 0.4 nozzle"},
                },
                {
                    "id": "f1",
                    "name": "A filament",
                    "content": {"type": "filament", "inherits": "Bambu PLA Basic @BBL X1C"},
                },
            ]
        )
        svc_mock.close = AsyncMock()
        creds = MagicMock()
        creds.token = "tok"
        user = MagicMock(id=1)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=creds)),
            patch.object(sp, "_build_orca_service", AsyncMock(return_value=svc_mock)),
        ):
            slots, _status = await sp._fetch_orca_cloud_presets(MagicMock(), user)
        sp._orca_cloud_cache.clear()

        assert [p.inherits for p in slots["printer"]] == ["Bambu Lab H2D 0.4 nozzle", None, None, None]
        assert slots["filament"][0].inherits == "Bambu PLA Basic @BBL X1C"

    @pytest.mark.asyncio
    async def test_bambu_cloud_uses_only_what_the_list_already_carries(self):
        """No detail request per preset: the parent is the list entry's own
        ``inherits`` name, or its ``base_id`` when the same list names it."""
        sp._cloud_cache.clear()
        cloud = MagicMock()
        cloud.get_slicer_settings = AsyncMock(
            return_value={
                "printer": {
                    "private": [
                        {"setting_id": "PMUS1", "name": "My H2D", "inherits": "Bambu Lab H2D 0.4 nozzle"},
                        {"setting_id": "PMUS2", "name": "My A1", "base_id": "GM030"},
                        {"setting_id": "PMUS3", "name": "Orphan", "base_id": "GM999"},
                        {"setting_id": "PMUS4", "name": "Rootless"},
                    ],
                    "public": [
                        {"setting_id": "GM030", "name": "Bambu Lab A1 0.4 nozzle"},
                        {"setting_id": "GM031", "name": "Bambu Lab A1 0.6 nozzle", "base_id": "GM030"},
                    ],
                },
                "print": {
                    "private": [
                        {"setting_id": "PPUS1", "name": "0.20mm P1S chornobomb", "base_id": "GP_P1S"},
                        {"setting_id": "PPUS2", "name": "0.20mm Standard @BBL X1C", "base_id": "GP_P1S"},
                    ],
                    "public": [{"setting_id": "GP_P1S", "name": "0.20mm Standard @BBL X1C"}],
                },
                "filament": {
                    "private": [{"setting_id": "PFUS1", "name": "My PLA", "base_id": "GF_A1M"}],
                    "public": [
                        {
                            "setting_id": "GF_A1M",
                            "name": "Bambu PLA Basic @BBL A1M",
                            "compatible_printers": ["Bambu Lab A1 mini 0.4 nozzle"],
                        },
                    ],
                },
            }
        )
        cloud.close = AsyncMock()
        cloud.set_token = MagicMock()
        user = MagicMock(id=7)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "get_stored_token", AsyncMock(return_value=("tok", "e@x", "global"))),
            patch.object(sp, "BambuCloudService", MagicMock(return_value=cloud)),
        ):
            slots, status = await sp._fetch_cloud_presets(MagicMock(commit=AsyncMock()), user)
        sp._cloud_cache.clear()

        assert status == "ok"
        assert {p.name: p.inherits for p in slots["printer"]} == {
            "My H2D": "Bambu Lab H2D 0.4 nozzle",
            "My A1": "Bambu Lab A1 0.4 nozzle",
            "Orphan": None,
            "Rootless": None,
            "Bambu Lab A1 0.4 nozzle": None,
            # A stock preset's base is file layout, not "saved from".
            "Bambu Lab A1 0.6 nozzle": None,
        }
        assert [(p.inherits, p.inherits_id) for p in slots["process"]] == [
            ("0.20mm Standard @BBL X1C", "GP_P1S"),
            ("0.20mm Standard @BBL X1C", "GP_P1S"),
        ]
        assert slots["filament"][0].inherits_id == "GF_A1M"
        assert slots["filament"][1].compatible_printers == ["Bambu Lab A1 mini 0.4 nozzle"]
        cloud.get_setting_detail.assert_not_called()


class TestCloudFilamentMaterialByIdentity:
    """A cloud filament preset states its material through what it IS -- its
    setting id, its family -- not through a same-named preset the bundled tier
    may or may not ship (upstream 9ac3f0f20, done differently).

    The bundle of the slicer in use can lack a whole printer (OrcaSlicer has no
    H3C profile at all): every "@BBL H3C" cloud preset then had no namesake,
    read as "material unknown", and a PETG-CF slot could be handed Generic
    PETG. Never a rule over the NAME, and never a colour."""

    async def _cloud(self, filament: dict) -> list[UnifiedPreset]:
        sp._cloud_cache.clear()
        cloud = MagicMock()
        cloud.get_slicer_settings = AsyncMock(return_value={"filament": filament})
        cloud.close = AsyncMock()
        user = MagicMock(id=7)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "get_stored_token", AsyncMock(return_value=("tok", "e@x", "global"))),
            patch.object(sp, "BambuCloudService", MagicMock(return_value=cloud)),
        ):
            slots, status = await sp._fetch_cloud_presets(MagicMock(commit=AsyncMock()), user)
        sp._cloud_cache.clear()
        assert status == "ok"
        return slots["filament"]

    @pytest.mark.asyncio
    async def test_a_stock_preset_of_a_printer_the_catalogue_lacks_takes_its_familys_material(self):
        # GFSG50_91 is in no catalogue; its base id GFSG50 is a PETG-CF family.
        (preset,) = await self._cloud(
            {"public": [{"setting_id": "GFSG50_91", "name": "Bambu PETG-CF @BBL H3C"}]},
        )
        assert preset.filament_type == "PETG-CF"
        assert preset.filament_colour is None

    @pytest.mark.asyncio
    async def test_a_users_own_preset_takes_the_material_of_the_family_it_names(self):
        (preset,) = await self._cloud(
            {"private": [{"setting_id": "PFUSaaaa", "name": "My Sunlu - Copy", "filament_id": "GFG99"}]},
        )
        assert preset.filament_type == "PETG"

    @pytest.mark.asyncio
    async def test_a_family_of_the_users_own_is_read_from_the_entry_itself(self):
        (preset,) = await self._cloud(
            {
                "private": [
                    {"setting_id": "PFUSbbbb", "name": "Mine", "filament_id": "P122e532", "filament_type": "PETG"}
                ]
            },
        )
        assert preset.filament_type == "PETG"

    @pytest.mark.asyncio
    async def test_nothing_known_stays_unknown(self):
        (preset,) = await self._cloud(
            {"private": [{"setting_id": "PFUScccc", "name": "Bambu PLA Basic @BBL X1C - Copy"}]},
        )
        # The name looks like a stock PLA; a name is not an identity.
        assert preset.filament_type is None

    def test_identity_outranks_a_same_named_preset_of_another_tier(self):
        cloud = _slots(
            filament=[
                UnifiedPreset(id="GFSG50_91", name="Bambu PETG-CF @BBL H3C", source="cloud", filament_type="PETG-CF")
            ]
        )
        standard = _slots(
            filament=[UnifiedPreset(id="x", name="Bambu PETG-CF @BBL H3C", source="standard", filament_type="PETG")]
        )
        _, out_cloud, _, _ = _enrich_cloud_metadata(_empty_slots(), cloud, _empty_slots(), standard)
        assert out_cloud["filament"][0].filament_type == "PETG-CF"

    @pytest.mark.asyncio
    async def test_an_orca_cloud_delta_takes_the_material_of_the_preset_it_was_saved_from(self):
        sp._orca_cloud_cache.clear()
        svc_mock = MagicMock()
        svc_mock.list_profiles = AsyncMock(
            return_value=[
                {
                    "id": "f1",
                    "name": "My HF tweak",
                    "content": {"type": "filament", "inherits": "Bambu PETG HF @BBL X1C"},
                },
                {
                    "id": "f2",
                    "name": "Says its own",
                    "content": {
                        "type": "filament",
                        "inherits": "Bambu PETG HF @BBL X1C",
                        "filament_type": ["PLA"],
                    },
                },
                {
                    "id": "f3",
                    "name": "Copy of a copy",
                    "content": {"type": "filament", "inherits": "My HF tweak"},
                },
                {
                    "id": "f4",
                    "name": "Bambu PLA Basic @BBL X1C - Copy",
                    "content": {"type": "filament", "inherits": "Nobody knows this one"},
                },
            ]
        )
        svc_mock.close = AsyncMock()
        creds = MagicMock()
        creds.token = "tok"
        user = MagicMock(id=1)
        user.has_permission = MagicMock(return_value=True)
        with (
            patch.object(sp, "_load_orca_credentials", AsyncMock(return_value=creds)),
            patch.object(sp, "_build_orca_service", AsyncMock(return_value=svc_mock)),
        ):
            slots, _status = await sp._fetch_orca_cloud_presets(MagicMock(), user)
        sp._orca_cloud_cache.clear()

        assert [p.filament_type for p in slots["filament"]] == ["PETG", "PLA", "PETG", None]
        assert all(p.filament_colour is None for p in slots["filament"])


class TestFilamentPresetInfoSources:
    """``GET /slicer/filament-preset/info`` reads the flow ratio of ONE picked
    preset for the Flow Rate calibration page. Every source a preset can be
    picked from has to be askable -- an Orca Cloud preset was a 400."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["orca_cloud", "cloud", "local", "standard"])
    async def test_every_preset_source_is_answered(self, source):
        resolved = '{"filament_flow_ratio": ["0.98"], "filament_type": ["PETG"]}'
        with patch(
            "backend.app.services.preset_resolver.resolve_preset_ref", AsyncMock(return_value=resolved)
        ) as resolve:
            info = await sp.get_filament_preset_info(source=source, id="abc", db=MagicMock(), user=None)
        assert (info.flow_ratio, info.filament_type) == (0.98, "PETG")
        assert resolve.await_args.args[2].source == source

    @pytest.mark.asyncio
    async def test_an_unknown_source_is_still_refused(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as refusal:
            await sp.get_filament_preset_info(source="bundle", id="abc", db=MagicMock(), user=None)
        assert refusal.value.status_code == 400
