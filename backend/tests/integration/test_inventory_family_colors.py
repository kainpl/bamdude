"""Narrow active family-colour feed for the AMS configuration dialog."""

from datetime import datetime, timezone

import pytest

from backend.app.models.spool import Spool


@pytest.mark.asyncio
@pytest.mark.integration
async def test_family_colors_are_exact_active_and_distinct(async_client, db_session):
    for family, rgba, name, brand, archived in [
        ("GF-PETG", "aabbccFF", "Blue", "A-brand", False),
        ("GF-PETG", "AABBCC80", "Azure", "Z-brand", False),
        ("GF-PETG", "BBCCDDFF", "Green", "A-brand", False),
        ("GF-PETG", "CCDDFFFF", "Archived", "A-brand", True),
        ("GF-PLA", "DDEEFFAA", "Wrong family", "A-brand", False),
    ]:
        db_session.add(
            Spool(
                material="PETG",
                filament_family_id=family,
                rgba=rgba,
                color_name=name,
                brand=brand,
                label_weight=1000,
                core_weight=250,
                weight_used=0,
                archived_at=datetime.now(timezone.utc) if archived else None,
            )
        )
    await db_session.commit()

    response = await async_client.get("/api/v1/inventory/spools/family-colors?filament_family_id=GF-PETG")
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"hex_color": "#AABBCC", "color_name": "Blue"},
        {"hex_color": "#BBCCDD", "color_name": "Green"},
    ]
    assert (await async_client.get("/api/v1/inventory/spools/family-colors?filament_family_id=missing")).json() == []
