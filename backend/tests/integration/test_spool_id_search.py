"""``#123`` in a spool search is that spool, exactly (upstream 632930f02, f652d3e0a).

The number written on a spool's label is how an operator looks it up. The
search matched ``#123`` as text — against nothing, under the default naming
template — while a bare ``123`` also found 1230, 5123 and lot 123. In the
slot-assignment picker an explicit number finds the spool whatever the slot's
material filter says, and a miss says why.
"""

from datetime import datetime, timezone

import pytest

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment

LIST = "/api/v1/inventory/spools?page=1&per_page=50"


def _spool(**fields) -> Spool:
    return Spool(label_weight=1000, core_weight=250, **{"material": "PLA", "brand": "Acme", **fields})


async def _seed(db_session, count: int = 13) -> list[Spool]:
    """Thirteen spools in id order; the second carries the twelfth's id as its lot."""
    spools = [_spool(color_name=f"Row {index}") for index in range(count)]
    db_session.add_all(spools)
    await db_session.flush()
    spools.sort(key=lambda spool: spool.id)
    spools[1].lot = spools[11].id
    await db_session.commit()
    return spools


def _ids(response) -> set[int]:
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()["items"]}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_hash_number_finds_exactly_that_spool_in_the_list(async_client, db_session):
    spools = await _seed(db_session)
    target, twelve = spools[0], spools[11]

    assert _ids(await async_client.get(f"{LIST}&q=%23{target.id}")) == {target.id}
    assert _ids(await async_client.get(f"{LIST}&q=%23{twelve.id}")) == {twelve.id}  # not the lot-12 spool
    # A bare number is still a substring search — the id, other ids and the lot.
    bare = _ids(await async_client.get(f"{LIST}&q={twelve.id}"))
    assert {twelve.id, spools[1].id} <= bare


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_hash_that_is_not_a_number_is_still_text(async_client, db_session):
    tagged = _spool(note="shelf #A7 by the door")
    plain = _spool(note="nothing here")
    db_session.add_all([tagged, plain])
    await db_session.commit()

    assert _ids(await async_client.get(f"{LIST}&q=%23A7")) == {tagged.id}
    # An id beyond any row is nobody, never an error.
    assert _ids(await async_client.get(f"{LIST}&q=%2399999999999999999999")) == set()
    # A number token beside words narrows like any other token.
    assert _ids(await async_client.get(f"{LIST}&q=door+%23{tagged.id}")) == {tagged.id}
    assert _ids(await async_client.get(f"{LIST}&q=door+%23{plain.id}")) == set()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_picker_finds_a_numbered_spool_whatever_the_slot_holds(async_client, db_session, printer_factory):
    printer = await printer_factory()
    petg = _spool(material="PETG", brand="Faraway")
    db_session.add(petg)
    await db_session.commit()
    base = f"/api/v1/inventory/spools/picker?printer_id={printer.id}&ams_id=0&tray_id=0&tray_material=PLA"

    # By name the PLA slot's filter hides it …
    assert _ids(await async_client.get(f"{base}&q=Faraway")) == set()
    # … by number it is found, and nothing explains a miss that did not happen.
    found = await async_client.get(f"{base}&q=%23{petg.id}")
    assert _ids(found) == {petg.id}
    assert found.json()["miss"] is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_picker_says_why_a_numbered_spool_is_not_offered(async_client, db_session, printer_factory):
    printer = await printer_factory()
    other = await printer_factory(name="Other printer")
    elsewhere = _spool(brand="Elsewhere")
    archived = _spool(brand="Archived", archived_at=datetime.now(timezone.utc))
    here = _spool(brand="Here")
    db_session.add_all([elsewhere, archived, here])
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=elsewhere.id, printer_id=other.id, ams_id=1, tray_id=2))
    db_session.add(SpoolAssignment(spool_id=here.id, printer_id=printer.id, ams_id=0, tray_id=0))
    await db_session.commit()
    base = f"/api/v1/inventory/spools/picker?printer_id={printer.id}&ams_id=0&tray_id=0&tray_material=PLA"

    async def miss(query: str) -> dict | None:
        response = await async_client.get(f"{base}&q={query}")
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        return response.json()["miss"]

    assert await miss(f"%23{elsewhere.id}") == {
        "reason": "other_slot",
        "spool_id": elsewhere.id,
        "printer_id": other.id,
        "printer_name": "Other printer",
        "ams_id": 1,
        "tray_id": 2,
    }
    assert (await miss(f"%23{archived.id}"))["reason"] == "archived"
    assert (await miss("%23987654"))["reason"] == "not_found"
    # The spool being replaced is the one already in this slot.
    replaced = await async_client.get(f"{base}&q=%23{here.id}&replacing_spool_id={here.id}")
    assert replaced.json()["miss"]["reason"] == "this_slot"
    # A text search that finds nothing has no single cause to name.
    assert await miss("Unobtainium") is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_hash_the_template_writes_before_another_field_stays_text(async_client, db_session):
    """``#{lot}`` on screen is text the operator reads; ``#{id}`` is the id itself."""
    from backend.app.models.settings import Settings

    spools = await _seed(db_session)  # spools[1].lot == spools[11].id
    setting = Settings(key="spool_display_template", value="{brand} #{lot}")
    db_session.add(setting)
    await db_session.commit()
    twelve = spools[11]

    assert _ids(await async_client.get(f"{LIST}&q=%23{twelve.id}")) == {twelve.id, spools[1].id}

    setting.value = "#{id} {brand}"
    await db_session.commit()
    assert _ids(await async_client.get(f"{LIST}&q=%23{spools[0].id}")) == {spools[0].id}
