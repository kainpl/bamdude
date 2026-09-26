"""The inventory routes keep a clear spool clear (upstream 73912d4f, #2912).

The create route cut ``rgba`` to six characters, so a hand-entered "fully
transparent" colour landed on opaque black; the edit compared bare RGB
prefixes, so an alpha-only edit never reached the filament, while comparing raw
strings would PATCH it on every no-op edit of an opaque spool.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SAMPLE_SPOOLMAN_SPOOL = {
    "id": 42,
    "filament": {
        "id": 7,
        "name": "PLA Basic",
        "material": "PLA",
        "color_hex": "FF0000",
        "weight": 1000,
        "vendor": {"id": 3, "name": "Bambu Lab"},
    },
    "remaining_weight": 750.0,
    "used_weight": 250.0,
    "location": "Printer1 - AMS A1",
    "comment": "test note",
    "first_used": "2024-01-01T00:00:00+00:00",
    "last_used": "2024-02-01T00:00:00+00:00",
    "registered": "2024-01-01T00:00:00+00:00",
    "archived": False,
    "price": None,
    "extra": {"tag": '"AABBCCDDEEFF0011AABBCCDDEEFF0011"'},
}


@pytest.fixture
async def spoolman_settings(db_session):
    """Create Spoolman settings in the database (enabled with URL)."""
    from backend.app.models.settings import Settings

    enabled_setting = Settings(key="spoolman_enabled", value="true")
    url_setting = Settings(key="spoolman_url", value="http://localhost:7912")
    db_session.add(enabled_setting)
    db_session.add(url_setting)
    await db_session.commit()
    return {"enabled": enabled_setting, "url": url_setting}


@pytest.fixture
def mock_spoolman_client():
    """Mock the Spoolman client with a sample spool."""
    mock_client = MagicMock()
    mock_client.base_url = "http://localhost:7912"
    mock_client.health_check = AsyncMock(return_value=True)
    mock_client.get_all_spools = AsyncMock(return_value=[SAMPLE_SPOOLMAN_SPOOL])
    mock_client.get_spool = AsyncMock(return_value=SAMPLE_SPOOLMAN_SPOOL)
    mock_client.create_spool = AsyncMock(return_value=SAMPLE_SPOOLMAN_SPOOL)
    mock_client.delete_spool = AsyncMock(return_value=True)
    mock_client.set_spool_archived = AsyncMock(
        side_effect=lambda spool_id, archived: {**SAMPLE_SPOOLMAN_SPOOL, "archived": archived}
    )
    mock_client.reset_spool_usage = AsyncMock(return_value={**SAMPLE_SPOOLMAN_SPOOL, "used_weight": 0})
    mock_client.update_spool_full = AsyncMock(return_value=SAMPLE_SPOOLMAN_SPOOL)
    mock_client.merge_spool_extra = AsyncMock(return_value=SAMPLE_SPOOLMAN_SPOOL)
    mock_client.find_or_create_filament = AsyncMock(return_value=7)
    mock_client.find_or_create_vendor = AsyncMock(return_value=3)
    mock_client.patch_filament = AsyncMock(return_value={"id": 7})
    # Default to singleton (only this spool uses the filament) so edits
    # exercise the new in-place-PATCH path; tests that need the shared
    # branch override this on the fly.
    mock_client.is_filament_shared = AsyncMock(return_value=False)
    mock_client.ensure_extra_field = AsyncMock(return_value=True)
    # list_spools calls maybe_sync_spoolman_locations which invokes
    # get_distinct_locations on the route-resolved client. Empty list keeps the
    # mock honest without staging phantom catalog rows.
    mock_client.get_distinct_locations = AsyncMock(return_value=[])

    with (
        patch(
            "backend.app.api.routes.spoolman_inventory.get_spoolman_client",
            AsyncMock(return_value=mock_client),
        ),
        patch(
            "backend.app.api.routes.spoolman_inventory.init_spoolman_client",
            AsyncMock(return_value=mock_client),
        ),
    ):
        yield mock_client


class TestTranslucentColour:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_spool_keeps_a_clear_colour_translucent(
        self,
        async_client: AsyncClient,
        spoolman_settings,
        mock_spoolman_client,
    ):
        """#2912: the create route truncated rgba to six characters, so entering
        "fully transparent" by hand landed on the same opaque black as the AMS
        case in the report."""
        payload = {
            "material": "PLA",
            "rgba": "00000000",
            "label_weight": 1000,
            "weight_used": 0,
        }
        response = await async_client.post("/api/v1/spoolman/inventory/spools", json=payload)

        assert response.status_code == 200
        assert mock_spoolman_client.find_or_create_filament.call_args.kwargs["color_hex"] == "00000000"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_spool_keeps_an_opaque_colour_at_six(
        self,
        async_client: AsyncClient,
        spoolman_settings,
        mock_spoolman_client,
    ):
        """The opaque case has to stay six characters or every create starts
        writing a shape the rest of the instance does not hold."""
        payload = {
            "material": "PLA",
            "rgba": "FF0000FF",
            "label_weight": 1000,
            "weight_used": 0,
        }
        response = await async_client.post("/api/v1/spoolman/inventory/spools", json=payload)

        assert response.status_code == 200
        assert mock_spoolman_client.find_or_create_filament.call_args.kwargs["color_hex"] == "FF0000"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_alpha_only_edit_reaches_the_filament(
        self,
        async_client: AsyncClient,
        spoolman_settings,
        mock_spoolman_client,
    ):
        """#2912: making a spool translucent is a real change to the filament's
        colour. Comparing bare RGB prefixes would call it a no-op and the edit
        would never land."""
        # Sample filament is FF0000; make it half-transparent.
        payload = {"rgba": "FF000080"}
        response = await async_client.patch("/api/v1/spoolman/inventory/spools/42", json=payload)

        assert response.status_code == 200
        mock_spoolman_client.patch_filament.assert_called_once()
        assert mock_spoolman_client.patch_filament.call_args.args[1]["color_hex"] == "FF000080"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_with_the_round_tripped_opaque_rgba_is_a_no_op(
        self,
        async_client: AsyncClient,
        spoolman_settings,
        mock_spoolman_client,
    ):
        """#2912: the read side hands the frontend FF0000FF for a filament stored
        as FF0000, and the edit form sends it straight back. Comparing raw strings
        would make metadata_unchanged permanently False and PATCH the filament on
        every no-op edit.
        """
        payload = {"rgba": "FF0000FF", "note": "unrelated change"}
        response = await async_client.patch("/api/v1/spoolman/inventory/spools/42", json=payload)

        assert response.status_code == 200
        mock_spoolman_client.patch_filament.assert_not_called()
        mock_spoolman_client.find_or_create_filament.assert_not_called()
