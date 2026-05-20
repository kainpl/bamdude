"""Unit tests for D.1: Spoolman color_name persistence via spool.extra.

Upstream Bambuddy #1357 / commit 4a98914d. Spoolman 0.23.1 has no
``color_name`` field on Filament, so we persist the user's color_name
under ``spool.extra.bambu_color_name``. These tests pin:

- ``_filament_subtype_part`` strips the material prefix consistently.
- ``find_or_create_filament`` matches by subtype-portion (so
  AMS-sync's "Glow" merges with edit-flow's "PLA Glow").
- ``_map_spoolman_spool`` reads color_name with the right priority:
  ``spool.extra.bambu_color_name`` → ``filament.color_name`` → synth
  from subtype.
- ``color_name_is_synthesized`` is True only when we fell back to subtype.
- ``is_filament_shared`` excludes the supplied spool id.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool
from backend.app.services.spoolman import SpoolmanClient, _filament_subtype_part


class TestFilamentSubtypePart:
    def test_strips_material_prefix(self):
        assert _filament_subtype_part("PLA Glow", "PLA") == "glow"
        assert _filament_subtype_part("PETG Black", "PETG") == "black"

    def test_no_prefix_returns_lowercased(self):
        # AMS-sync auto-create stores just "Glow" without the material prefix
        assert _filament_subtype_part("Glow", "PLA") == "glow"

    def test_case_insensitive_prefix_match(self):
        assert _filament_subtype_part("pla glow", "PLA") == "glow"
        assert _filament_subtype_part("PLA glow", "pla") == "glow"

    def test_empty_inputs_safe(self):
        assert _filament_subtype_part("", "PLA") == ""
        assert _filament_subtype_part("PLA Glow", "") == "pla glow"
        assert _filament_subtype_part("", "") == ""

    def test_material_not_at_start_kept(self):
        # "Pro PLA Glow" doesn't START with "PLA " so the helper returns
        # the whole name lowercased — we don't try to scan inside the
        # string. Documents this corner so a future change doesn't
        # accidentally start matching.
        assert _filament_subtype_part("Pro PLA Glow", "PLA") == "pro pla glow"


class TestMapSpoolmanSpoolColorName:
    """``_map_spoolman_spool`` priority: extra → filament → synth."""

    def _base_spool(self, *, extra: dict | None = None, color_name: str | None = None) -> dict:
        return {
            "id": 1,
            "filament": {
                "id": 100,
                "name": "PLA Glow",
                "material": "PLA",
                "color_hex": "FFAA00",
                "color_name": color_name,
                "weight": 1000,
                "vendor": {"name": "Bambu Lab"},
            },
            "extra": extra or {},
            "registered": "2026-01-01T00:00:00Z",
        }

    def test_extra_wins_over_filament_color_name(self):
        spool = self._base_spool(
            extra={"bambu_color_name": json.dumps("Sunset Orange")},
            color_name="Some Other Value",
        )
        mapped = _map_spoolman_spool(spool)
        assert mapped["color_name"] == "Sunset Orange"
        assert mapped["color_name_is_synthesized"] is False

    def test_filament_color_name_used_when_no_extra(self):
        spool = self._base_spool(color_name="Lab-Stored Color")
        mapped = _map_spoolman_spool(spool)
        assert mapped["color_name"] == "Lab-Stored Color"
        assert mapped["color_name_is_synthesized"] is False

    def test_synth_fallback_to_subtype(self):
        """Neither extra nor filament.color_name set → fall back to subtype
        ("Glow") and flag as synthesised."""
        spool = self._base_spool()  # both empty
        mapped = _map_spoolman_spool(spool)
        assert mapped["color_name"] == "Glow"
        assert mapped["color_name_is_synthesized"] is True

    def test_extra_overrides_filament_even_when_filament_set(self):
        spool = self._base_spool(
            extra={"bambu_color_name": json.dumps("Real Edit")},
            color_name="Old Value",
        )
        mapped = _map_spoolman_spool(spool)
        assert mapped["color_name"] == "Real Edit"

    def test_empty_extra_string_treated_as_unset(self):
        """A cleared extra value (empty JSON string) falls through to the
        next source — the route writes ``json.dumps("")`` to clear."""
        spool = self._base_spool(
            extra={"bambu_color_name": json.dumps("")},
            color_name="Filament Fallback",
        )
        mapped = _map_spoolman_spool(spool)
        assert mapped["color_name"] == "Filament Fallback"


class TestIsFilamentShared:
    @pytest.mark.asyncio
    async def test_returns_true_when_other_spool_links_to_filament(self):
        client = SpoolmanClient(base_url="http://spoolman.example")
        client.get_all_spools = AsyncMock(
            return_value=[
                {"id": 1, "filament": {"id": 100}},
                {"id": 2, "filament": {"id": 100}},  # sibling
            ]
        )
        assert await client.is_filament_shared(100, exclude_spool_id=1) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_only_excluded_spool_links(self):
        client = SpoolmanClient(base_url="http://spoolman.example")
        client.get_all_spools = AsyncMock(return_value=[{"id": 1, "filament": {"id": 100}}])
        assert await client.is_filament_shared(100, exclude_spool_id=1) is False

    @pytest.mark.asyncio
    async def test_includes_archived_spools(self):
        """A sibling spool that was archived must still count as shared so
        the singleton path doesn't accidentally overwrite its metadata."""
        client = SpoolmanClient(base_url="http://spoolman.example")

        async def fake_get_all_spools(allow_archived: bool = False):
            assert allow_archived is True, "is_filament_shared must include archived siblings"
            return [
                {"id": 1, "filament": {"id": 100}},
                {"id": 2, "filament": {"id": 100}, "archived": True},
            ]

        client.get_all_spools = fake_get_all_spools
        assert await client.is_filament_shared(100, exclude_spool_id=1) is True


class TestFindOrCreateFilamentSubtypeMatch:
    """``find_or_create_filament`` matches by SUBTYPE portion of the name
    so AMS-sync auto-creates ("Glow") merge with user edits ("PLA Glow")."""

    @pytest.mark.asyncio
    async def test_matches_ams_sync_style_name(self):
        """AMS-sync stored the filament as just "Glow"; the user edits
        compose "PLA Glow". Both must resolve to the same filament_id —
        otherwise every edit mints a duplicate."""
        client = SpoolmanClient(base_url="http://spoolman.example")
        client.find_or_create_vendor = AsyncMock(return_value=9)
        # Filament was created by AMS-sync with just "Glow"
        client.get_filaments = AsyncMock(
            return_value=[
                {
                    "id": 42,
                    "material": "PLA",
                    "name": "Glow",
                    "color_hex": "FFAA00",
                    "vendor": {"id": 9, "name": "Bambu Lab"},
                }
            ]
        )
        client.create_filament = AsyncMock()

        result = await client.find_or_create_filament(
            material="PLA",
            subtype="Glow",
            brand="Bambu Lab",
            color_hex="FFAA00",
            label_weight=1000,
        )
        assert result == 42
        (
            client.create_filament.assert_not_called(),
            ("Subtype-match should reuse the existing 'Glow' filament, not mint a 'PLA Glow' duplicate"),
        )

    @pytest.mark.asyncio
    async def test_distinct_color_creates_new(self):
        client = SpoolmanClient(base_url="http://spoolman.example")
        client.find_or_create_vendor = AsyncMock(return_value=9)
        client.get_filaments = AsyncMock(
            return_value=[
                {
                    "id": 42,
                    "material": "PLA",
                    "name": "Glow",
                    "color_hex": "FFAA00",
                    "vendor": {"id": 9, "name": "Bambu Lab"},
                }
            ]
        )
        client.create_filament = AsyncMock(return_value={"id": 43})

        result = await client.find_or_create_filament(
            material="PLA",
            subtype="Glow",
            brand="Bambu Lab",
            color_hex="123456",  # different color
            label_weight=1000,
        )
        assert result == 43
        client.create_filament.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_pass_color_name_to_create_filament(self):
        """Spoolman silently drops the key. We no longer pass it — the
        route writes color_name into spool.extra.bambu_color_name instead."""
        client = SpoolmanClient(base_url="http://spoolman.example")
        client.find_or_create_vendor = AsyncMock(return_value=9)
        client.get_filaments = AsyncMock(return_value=[])  # no match → create
        client.create_filament = AsyncMock(return_value={"id": 99})

        await client.find_or_create_filament(
            material="PLA",
            subtype="Glow",
            brand="Bambu Lab",
            color_hex="FFAA00",
            label_weight=1000,
            color_name="Sunset Orange",  # caller still passes it but we ignore
        )
        # Assert create_filament wasn't called with color_name kwarg
        kwargs = client.create_filament.call_args.kwargs
        assert "color_name" not in kwargs, "color_name must NOT be passed to create_filament (Spoolman drops it)"
