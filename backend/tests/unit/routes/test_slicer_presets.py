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
