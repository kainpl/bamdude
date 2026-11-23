import pytest

from backend.app.models.settings import Settings


def material_options(definition):
    field = next(field for field in definition["fields"] if field["key"] == "filament_type")
    return {option["value"] for option in field["options"]}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filament_fields_follow_the_slicer_without_mutating_the_cache(async_client, db_session):
    base = "/api/v1/cloud/fields"

    async def read():
        single = await async_client.get(f"{base}/filament")
        combined = await async_client.get(base)
        assert single.status_code == combined.status_code == 200
        assert single.json() == combined.json()["filament"]
        return combined.json()

    original = await read()
    assert "TPU-AMS" in material_options(original["filament"])
    assert "SBS" not in material_options(original["filament"])

    setting = await db_session.merge(Settings(key="preferred_slicer", value="orcaslicer"))
    await db_session.commit()
    orca = await read()
    assert "SBS" in material_options(orca["filament"])
    assert "TPU-AMS" not in material_options(orca["filament"])
    assert (orca["process"], orca["printer"]) == (original["process"], original["printer"])

    setting.value = "bambu_studio"
    await db_session.commit()
    assert await read() == original
