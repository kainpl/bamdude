"""Integration tests for the /api/inventory/colors/map endpoint.

Verifies the priority rules used by the color name resolution:
- Bambu Lab manufacturer wins over other manufacturers for the same hex
- is_default tiebreaker after manufacturer priority
- Hex normalization to lowercase 6-char (no leading '#')
- Empty/invalid entries are skipped
"""

import pytest
from httpx import AsyncClient


class TestColorNameMapEndpoint:
    """Tests for GET /api/inventory/colors/map."""

    @pytest.fixture
    async def color_entries(self, db_session):
        """Seed the color_catalog with conflict-test entries."""
        from backend.app.models.color_catalog import ColorCatalogEntry

        entries = [
            # Same hex, different manufacturers - Bambu Lab wins
            ColorCatalogEntry(
                manufacturer="Bambu Lab",
                color_name="Cherry Pink",
                hex_color="#FF0066",
                material="PLA",
                is_default=False,
            ),
            ColorCatalogEntry(
                manufacturer="Polymaker",
                color_name="Pink",
                hex_color="#FF0066",
                material="PLA",
                is_default=False,
            ),
            # Default-only entry
            ColorCatalogEntry(
                manufacturer="Generic",
                color_name="Generic Blue",
                hex_color="#0000FF",
                material="PLA",
                is_default=True,
            ),
            ColorCatalogEntry(
                manufacturer="Other",
                color_name="Random Blue",
                hex_color="#0000FF",
                material="PLA",
                is_default=False,
            ),
            # Hex without '#' to test normalization
            ColorCatalogEntry(
                manufacturer="Bambu Lab",
                color_name="Pure Black",
                hex_color="000000",
                material="PLA",
                is_default=True,
            ),
            # Invalid hex (too short) - should be skipped
            ColorCatalogEntry(
                manufacturer="Bambu Lab",
                color_name="Bad",
                hex_color="#FFF",
                material="PLA",
                is_default=False,
            ),
            # Empty name - should be skipped
            ColorCatalogEntry(
                manufacturer="Bambu Lab",
                color_name="",
                hex_color="#AABBCC",
                material="PLA",
                is_default=False,
            ),
        ]
        for e in entries:
            db_session.add(e)
        await db_session.commit()
        return entries

    @pytest.mark.asyncio
    async def test_returns_compact_map(self, async_client: AsyncClient, color_entries):
        response = await async_client.get("/api/v1/inventory/colors/map")
        assert response.status_code == 200
        data = response.json()
        assert "colors" in data
        assert isinstance(data["colors"], dict)

    @pytest.mark.asyncio
    async def test_bambu_lab_wins_over_other_manufacturers(self, async_client: AsyncClient, color_entries):
        response = await async_client.get("/api/v1/inventory/colors/map")
        colors = response.json()["colors"]
        # ff0066 has Bambu (Cherry Pink) + Polymaker (Pink) - Bambu wins
        assert colors.get("ff0066") == "Cherry Pink"

    @pytest.mark.asyncio
    async def test_is_default_breaks_tie_among_non_bambu(self, async_client: AsyncClient, color_entries):
        response = await async_client.get("/api/v1/inventory/colors/map")
        colors = response.json()["colors"]
        # 0000ff has Generic (default) + Other (non-default) - Generic wins
        assert colors.get("0000ff") == "Generic Blue"

    @pytest.mark.asyncio
    async def test_normalizes_keys_to_lowercase_no_hash(self, async_client: AsyncClient, color_entries):
        response = await async_client.get("/api/v1/inventory/colors/map")
        colors = response.json()["colors"]
        # All keys are lowercase, no '#'
        for key in colors:
            assert key == key.lower()
            assert not key.startswith("#")
            assert len(key) == 6

    @pytest.mark.asyncio
    async def test_handles_hex_without_hash(self, async_client: AsyncClient, color_entries):
        response = await async_client.get("/api/v1/inventory/colors/map")
        colors = response.json()["colors"]
        # "000000" stored without '#' should still normalize and appear
        assert colors.get("000000") == "Pure Black"

    @pytest.mark.asyncio
    async def test_skips_invalid_hex(self, async_client: AsyncClient, color_entries):
        response = await async_client.get("/api/v1/inventory/colors/map")
        colors = response.json()["colors"]
        # 3-char hex was skipped (length != 6)
        assert "fff" not in colors

    @pytest.mark.asyncio
    async def test_skips_empty_names(self, async_client: AsyncClient, color_entries):
        response = await async_client.get("/api/v1/inventory/colors/map")
        colors = response.json()["colors"]
        # aabbcc had empty color_name → not in map
        assert "aabbcc" not in colors

    @pytest.mark.asyncio
    async def test_empty_catalog_returns_empty_map(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/inventory/colors/map")
        assert response.status_code == 200
        assert response.json() == {"colors": {}}
