"""Printed local labels follow a row through edits and reject a reused ID."""

from uuid import UUID

import pytest
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError

from backend.app.migrations import m260_spool_label_identity
from backend.app.models.location import Location
from backend.app.models.spool import Spool
from backend.app.services.spool_label_identity import label_key, local_spool_label_link

pytestmark = pytest.mark.asyncio


async def test_migration_backfills_each_row_once_and_rejects_duplicate_ids(test_engine):
    async with test_engine.begin() as conn:
        await conn.execute(insert(Spool), [{"id": i, "material": "PLA"} for i in range(1, 502)])
        await conn.exec_driver_sql("DROP INDEX ix_spool_label_identity")
        await conn.exec_driver_sql("ALTER TABLE spool DROP COLUMN label_identity")
        await m260_spool_label_identity.upgrade(conn)
        query = text("SELECT label_identity FROM spool ORDER BY id")
        identities = (await conn.execute(query)).scalars().all()
        assert len(set(identities)) == 501
        assert all(UUID(value).version == 4 for value in identities)
        await m260_spool_label_identity.upgrade(conn)
        assert (await conn.execute(query)).scalars().all() == identities
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    text("UPDATE spool SET label_identity = :identity WHERE id = 2"), {"identity": identities[0]}
                )


async def test_spool_insert_generates_one_distinct_identity_for_each_row(db_session):
    spools = [Spool(material="PLA", lot=10) for _ in range(3)]
    db_session.add_all(spools)
    await db_session.commit()

    assert len({spool.label_identity for spool in spools}) == len(spools)
    assert all(UUID(spool.label_identity).version == 4 for spool in spools)
    with pytest.raises(IntegrityError), db_session.no_autoflush:
        async with db_session.begin_nested():
            db_session.add(Spool(material="PLA", label_identity=spools[0].label_identity))
            await db_session.flush()


async def test_spool_label_survives_lot_change_and_rejects_replacement(db_session, async_client, printer_factory):
    original = Spool(material="PLA", lot=10)
    location = Location(name="Shelf A", name_key="shelf a")
    db_session.add_all([original, location])
    await db_session.commit()
    spool_id = original.id
    key = label_key(spool_id, original.label_identity)
    assert local_spool_label_link("https://farm.example", spool_id, original.label_identity).endswith(
        f"/inventory?spool={spool_id}&src=local&v=2&key={key}"
    )

    url = f"/api/v1/inventory/spools/{spool_id}"
    params = {"label_key": key}
    updated = await async_client.patch(url, params=params, json={"lot": 20, "location_id": location.id})
    assert updated.status_code == 200, updated.text
    assert updated.json()["lot"] == 20
    assert updated.json()["location_id"] == location.id
    assert (await async_client.get(url, params=params)).status_code == 200

    deleted = await async_client.delete(url)
    assert deleted.status_code == 200, deleted.text
    replacement = Spool(id=spool_id, material="PLA", lot=10)
    db_session.add(replacement)
    await db_session.commit()
    assert label_key(spool_id, replacement.label_identity) != key

    preview = await async_client.get(url, params=params)
    change = await async_client.patch(url, params=params, json={"lot": 30, "location_id": location.id})
    printer = await printer_factory()
    assignment = await async_client.post(
        "/api/v1/inventory/assignments",
        json={"spool_id": spool_id, "printer_id": printer.id, "ams_id": 0, "tray_id": 0, "label_key": key},
    )
    for response in (preview, change, assignment):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "spool_label_changed"
    current = (await async_client.get(url)).json()
    assert current["lot"] == 10
    assert current["location_id"] is None
    assert (await async_client.get("/api/v1/inventory/assignments")).json() == []

    assigned = await async_client.post(
        "/api/v1/inventory/assignments",
        json={
            "spool_id": spool_id,
            "printer_id": printer.id,
            "ams_id": 0,
            "tray_id": 0,
            "label_key": label_key(spool_id, replacement.label_identity),
        },
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["spool_id"] == spool_id
