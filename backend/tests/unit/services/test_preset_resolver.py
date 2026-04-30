"""Tests for ``preset_resolver`` — Phase 1 of the 0.5.x slicer cycle.

Covers all three sources (local / cloud / standard) plus the CLOUD_AUTH
permission gate and the silent-fallback prevention via ``type`` injection.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from backend.app.models.local_preset import LocalPreset
from backend.app.schemas.slicer import PresetRef
from backend.app.services.preset_resolver import (
    _resolve_standard,
    resolve_preset_ref,
)


class TestStandardSource:
    def test_emits_inherits_stub_with_correct_type(self):
        """Standard tier emits a 4-key stub. ``type`` is critical — a missing
        ``type`` field makes the CLI silently reject the preset and fall back
        to embedded settings (fixed in upstream, pinned here as a regression
        guard)."""
        out = _resolve_standard(PresetRef(source="standard", id="0.20mm Standard"), "process")
        parsed = json.loads(out)
        assert parsed["name"] == "0.20mm Standard"
        assert parsed["inherits"] == "0.20mm Standard"
        assert parsed["from"] == "system"
        assert parsed["type"] == "process"

    def test_printer_slot_maps_to_machine_type(self):
        out = _resolve_standard(PresetRef(source="standard", id="A1 0.4 nozzle"), "printer")
        parsed = json.loads(out)
        assert parsed["type"] == "machine"

    def test_filament_slot_maps_to_filament_type(self):
        out = _resolve_standard(PresetRef(source="standard", id="Bambu PLA Basic @BBL A1"), "filament")
        parsed = json.loads(out)
        assert parsed["type"] == "filament"

    def test_unknown_slot_rejected(self):
        with pytest.raises(HTTPException) as exc_info:
            _resolve_standard(PresetRef(source="standard", id="x"), "unknown_slot")
        assert exc_info.value.status_code == 400


class TestLocalSource:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_setting_json_string(self, db_session):
        preset = LocalPreset(
            name="My Process",
            preset_type="process",
            source="manual",
            setting='{"name":"My Process","layer_height":0.2}',
        )
        db_session.add(preset)
        await db_session.commit()
        await db_session.refresh(preset)

        out = await resolve_preset_ref(
            db_session,
            None,
            PresetRef(source="local", id=str(preset.id)),
            "process",
        )
        parsed = json.loads(out)
        assert parsed["name"] == "My Process"
        assert parsed["layer_height"] == 0.2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_invalid_id_string_rejected(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            await resolve_preset_ref(db_session, None, PresetRef(source="local", id="not-a-number"), "process")
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_unknown_id_returns_400(self, db_session):
        with pytest.raises(HTTPException) as exc_info:
            await resolve_preset_ref(db_session, None, PresetRef(source="local", id="999999"), "process")
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_wrong_preset_type_rejected(self, db_session):
        """A printer-typed preset can't be used in a process slot — the
        resolver rejects it so the sidecar never sees a type-mismatched
        triplet (which would silently fall back to embedded settings)."""
        preset = LocalPreset(
            name="A1 Printer",
            preset_type="printer",
            source="manual",
            setting='{"name":"A1 Printer"}',
        )
        db_session.add(preset)
        await db_session.commit()
        await db_session.refresh(preset)

        with pytest.raises(HTTPException) as exc_info:
            await resolve_preset_ref(
                db_session,
                None,
                PresetRef(source="local", id=str(preset.id)),
                "process",  # wrong slot
            )
        assert exc_info.value.status_code == 400


class TestCloudSource:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_settings_json_under_setting_envelope(self, db_session):
        """``get_setting_detail`` returns ``{setting: {...preset...}}``.
        The resolver unwraps the envelope before returning the JSON string."""
        with (
            patch(
                "backend.app.services.preset_resolver.get_stored_token",
                AsyncMock(return_value=("tok", "u@e.com", "global")),
            ),
            patch("backend.app.services.preset_resolver.BambuCloudService") as mock_svc_cls,
        ):
            mock_svc = SimpleNamespace(
                set_token=lambda _t: None,
                get_setting_detail=AsyncMock(return_value={"setting": {"name": "Cloud PLA", "filament_type": ["PLA"]}}),
                close=AsyncMock(),
            )
            mock_svc_cls.return_value = mock_svc

            out = await resolve_preset_ref(
                db_session,
                None,
                PresetRef(source="cloud", id="abc-123"),
                "filament",
            )
            parsed = json.loads(out)
            assert parsed["name"] == "Cloud PLA"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_stored_token_returns_400(self, db_session):
        with (
            patch(
                "backend.app.services.preset_resolver.get_stored_token",
                AsyncMock(return_value=(None, None, "global")),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await resolve_preset_ref(
                db_session,
                None,
                PresetRef(source="cloud", id="abc"),
                "filament",
            )
        assert exc_info.value.status_code == 400
        assert "Bambu Cloud" in exc_info.value.detail

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cloud_auth_error_becomes_401(self, db_session):
        from backend.app.services.bambu_cloud import BambuCloudAuthError

        with (
            patch(
                "backend.app.services.preset_resolver.get_stored_token",
                AsyncMock(return_value=("tok", "u@e.com", "global")),
            ),
            patch("backend.app.services.preset_resolver.BambuCloudService") as mock_svc_cls,
        ):
            mock_svc = SimpleNamespace(
                set_token=lambda _t: None,
                get_setting_detail=AsyncMock(side_effect=BambuCloudAuthError("expired")),
                close=AsyncMock(),
            )
            mock_svc_cls.return_value = mock_svc

            with pytest.raises(HTTPException) as exc_info:
                await resolve_preset_ref(
                    db_session,
                    None,
                    PresetRef(source="cloud", id="x"),
                    "filament",
                )
            assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cloud_network_error_becomes_502(self, db_session):
        from backend.app.services.bambu_cloud import BambuCloudError

        with (
            patch(
                "backend.app.services.preset_resolver.get_stored_token",
                AsyncMock(return_value=("tok", "u@e.com", "global")),
            ),
            patch("backend.app.services.preset_resolver.BambuCloudService") as mock_svc_cls,
        ):
            mock_svc = SimpleNamespace(
                set_token=lambda _t: None,
                get_setting_detail=AsyncMock(side_effect=BambuCloudError("dns")),
                close=AsyncMock(),
            )
            mock_svc_cls.return_value = mock_svc

            with pytest.raises(HTTPException) as exc_info:
                await resolve_preset_ref(
                    db_session,
                    None,
                    PresetRef(source="cloud", id="x"),
                    "filament",
                )
            assert exc_info.value.status_code == 502


class TestUnknownSource:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_garbage_source_returns_400(self, db_session):
        # Pydantic Literal already blocks bad source values at validation;
        # this guards the resolver itself in case a stale call site bypasses
        # the schema (e.g. internal callers).
        ref = PresetRef.model_construct(source="bogus", id="x")  # bypass validator
        with pytest.raises(HTTPException) as exc_info:
            await resolve_preset_ref(db_session, None, ref, "process")
        assert exc_info.value.status_code == 400
