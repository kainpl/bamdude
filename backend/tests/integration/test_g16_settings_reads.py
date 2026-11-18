"""Settings reads that must not fail whole, and the reads other sections lean on.

- one stored row that cannot be read as its field no longer fails every settings read and save;
- the strict-stagger switch is a field of the settings schema, so the page can save it;
- macros, cameras, local presets and the Spoolman mode are read by the print dialog, the camera
  wall, the spool forms and the inventory page: their readers need those sections' rights, not
  ``settings:read``.
"""

import pytest

from backend.app.core.permissions import Permission
from backend.app.models.group import Group
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.tests.auth_helpers import mint_test_access

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    ("key", "stored"),
    [
        ("ftp_timeout", "None"),
        ("default_filament_cost", "abc"),
        ("pause_warning_minutes", "999"),
        ("preheat_max_wait_seconds", "None"),
        ("mcp_allowed_hosts", "{not json"),
    ],
)
async def test_an_unreadable_row_reads_as_its_default_and_can_be_saved_again(async_client, db_session, key, stored):
    from backend.app.schemas.settings import AppSettings

    db_session.add(Settings(key=key, value=stored))
    await db_session.commit()
    expected = AppSettings().model_dump()[key]

    read = await async_client.get("/api/v1/settings/")
    assert read.status_code == 200, read.text
    assert read.json()[key] == expected
    assert (await async_client.get("/api/v1/settings/ui-preferences")).status_code == 200

    saved = await async_client.put("/api/v1/settings/", json={"currency": "EUR"})
    assert saved.status_code == 200, saved.text
    assert saved.json()["currency"] == "EUR"


async def test_the_strict_stagger_switch_is_saved_and_read_back(async_client, db_session):
    from backend.app.api.routes.settings import get_setting

    saved = await async_client.put("/api/v1/settings/", json={"stagger_strict_for_direct_dispatch": True})

    assert saved.status_code == 200, saved.text
    assert saved.json()["stagger_strict_for_direct_dispatch"] is True
    assert (await get_setting(db_session, "stagger_strict_for_direct_dispatch")).lower() == "true"
    assert (await async_client.get("/api/v1/settings/")).json()["stagger_strict_for_direct_dispatch"] is True


async def _headers(db_session, name: str, *permissions: Permission) -> dict:
    group = Group(name=f"g16 {name}", permissions=[p.value for p in permissions])
    user = User(username=f"g16-{name}", role="user", is_active=True, password_hash="unused", groups=[group])
    db_session.add(user)
    await db_session.commit()
    return {"Authorization": f"Bearer {await mint_test_access(db_session, username=user.username)}"}


@pytest.mark.parametrize(
    ("path", "right"),
    [
        ("/api/v1/macros/", Permission.PRINTERS_READ),
        ("/api/v1/cameras/", Permission.CAMERA_VIEW),
        ("/api/v1/local-presets/", Permission.INVENTORY_READ),
        ("/api/v1/settings/spoolman", Permission.INVENTORY_READ),
    ],
)
async def test_a_section_reader_without_the_settings_right_reads_what_its_pages_need(
    async_client, db_session, path, right
):
    reader = await _headers(db_session, f"reader-{right.name.lower()}", right)
    stranger = await _headers(db_session, f"stranger-{right.name.lower()}", Permission.STATS_READ)

    assert (await async_client.get(path, headers=reader)).status_code == 200
    assert (await async_client.get(path, headers=stranger)).status_code == 403
    assert (await async_client.get("/api/v1/settings/", headers=reader)).status_code == 403


async def test_the_spoolman_address_stays_with_the_settings_right(async_client, db_session):
    db_session.add_all(
        [Settings(key="spoolman_enabled", value="true"), Settings(key="spoolman_url", value="http://spoolman.lan:7912")]
    )
    await db_session.commit()
    inventory = await _headers(db_session, "inventory", Permission.INVENTORY_READ)

    mode = (await async_client.get("/api/v1/settings/spoolman", headers=inventory)).json()
    full = (await async_client.get("/api/v1/settings/spoolman")).json()

    assert (mode["spoolman_enabled"], mode["spoolman_url"]) == ("true", "")
    assert full["spoolman_url"] == "http://spoolman.lan:7912"
