"""Integration tests for the material-disambiguated colour lookup endpoint.

Covers ``GET /api/v1/inventory/colors/by-material`` (upstream Bambuddy #1718),
which resolves the same hex to different catalogue names depending on the
material context — e.g. ``#000000`` is "Charcoal" for PLA Matte but "Black"
for PLA Basic. The flat ``/colors/map`` collapses these to a single priority
winner; this endpoint preserves the material so the queue scheduler's
Filament Override / Mapping labels show the actually-sliced sub-brand colour.
"""

import pytest
from httpx import AsyncClient

# ---- /colors/by-material — disambiguated lookup (#1718) -------------------


async def _seed_black_collision(client: AsyncClient) -> None:
    """Seed the #000000 ambiguity the endpoint was built to resolve.

    PLA Matte → Charcoal, PLA Basic → Black, both at #000000 — same shape as
    Bambu's production catalog.
    """
    for entry in (
        {
            "manufacturer": "Bambu Lab",
            "color_name": "Charcoal",
            "hex_color": "#000000",
            "material": "PLA Matte",
        },
        {
            "manufacturer": "Bambu Lab",
            "color_name": "Black",
            "hex_color": "#000000",
            "material": "PLA Basic",
        },
    ):
        response = await client.post("/api/v1/inventory/colors", json=entry)
        assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_by_material_returns_material_specific_name(async_client: AsyncClient):
    """Same hex + different material → returns the correctly-paired name."""
    await _seed_black_collision(async_client)

    matte = await async_client.get(
        "/api/v1/inventory/colors/by-material", params={"hex": "#000000", "material": "PLA Matte"}
    )
    assert matte.status_code == 200, matte.text
    assert matte.json() == {"color_name": "Charcoal"}

    basic = await async_client.get(
        "/api/v1/inventory/colors/by-material", params={"hex": "#000000", "material": "PLA Basic"}
    )
    assert basic.status_code == 200, basic.text
    assert basic.json() == {"color_name": "Black"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_by_material_falls_back_to_first_when_material_unknown(async_client: AsyncClient):
    """Unknown / unsupplied material → priority-order fallback, same as
    ``/colors/map`` so existing flat-map callers don't regress."""
    await _seed_black_collision(async_client)

    # Unknown material → first Bambu Lab entry wins (matches /map's priority).
    unknown = await async_client.get(
        "/api/v1/inventory/colors/by-material", params={"hex": "#000000", "material": "PLA-Nope"}
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["color_name"] in {"Charcoal", "Black"}

    # No material at all → same fallback.
    nomat = await async_client.get("/api/v1/inventory/colors/by-material", params={"hex": "#000000"})
    assert nomat.status_code == 200, nomat.text
    assert nomat.json()["color_name"] in {"Charcoal", "Black"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_by_material_returns_null_when_hex_missing(async_client: AsyncClient):
    """Hex not present in the catalog → color_name=None (do NOT 404)."""
    response = await async_client.get(
        "/api/v1/inventory/colors/by-material",
        params={"hex": "#abcdef", "material": "PLA Matte"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"color_name": None}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_by_material_case_insensitive_on_both_inputs(async_client: AsyncClient):
    """Lookup must tolerate mixed-case hex (legacy imports stored ``#B39B84``-
    style upper-case) and material (frontend derives material from sub-brand
    names whose casing isn't pinned). The endpoint strips ``#`` and lower-cases
    ``hex_color`` and lower-cases ``material`` before equality, so both
    directions of the case mismatch must round-trip."""
    # Seed an upper-case stored hex to exercise the lower-cased comparison.
    seed = await async_client.post(
        "/api/v1/inventory/colors",
        json={
            "manufacturer": "Bambu Lab",
            "color_name": "Iridium Gold Metallic",
            "hex_color": "#B39B84",  # stored upper-case
            "material": "PLA Metal",
        },
    )
    assert seed.status_code == 200, seed.text

    # Query the upper-case hex with lower-case input — must still match.
    lower_query = await async_client.get(
        "/api/v1/inventory/colors/by-material",
        params={"hex": "#b39b84", "material": "PLA Metal"},
    )
    assert lower_query.status_code == 200
    assert lower_query.json() == {"color_name": "Iridium Gold Metallic"}

    # Material is matched case-insensitively too.
    await _seed_black_collision(async_client)
    mixed_mat = await async_client.get(
        "/api/v1/inventory/colors/by-material",
        params={"hex": "#000000", "material": "pla matte"},
    )
    assert mixed_mat.json() == {"color_name": "Charcoal"}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_by_material_rejects_short_hex(async_client: AsyncClient):
    """Invalid hex (< 6 chars after stripping '#') → color_name=None, no crash."""
    response = await async_client.get(
        "/api/v1/inventory/colors/by-material",
        params={"hex": "#abc", "material": "PLA Matte"},
    )
    assert response.status_code == 200
    assert response.json() == {"color_name": None}
