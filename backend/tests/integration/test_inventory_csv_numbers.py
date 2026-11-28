"""Spools keep their numbers and label identities when the inventory moves by CSV (C32, upstream #3520).

A printed label's QR code names its spool by the number and by a key made from the number and the
row's label identity, so the CSV carries both, and an import keeps them for a number this install
never handed out. A number given out here before -- a living spool's or a deleted one's -- or one
the file names twice gets a new number, and with it a new label identity. The same checks run on
PostgreSQL in ``test_inventory_csv_numbers_postgres.py``.
"""

import csv
from io import StringIO

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.services.spool_label_identity import label_key, matches_label_key

U1 = "6f1c2a10-1b2c-4d3e-8f40-5a6b7c8d9e01"
U2 = "6f1c2a10-1b2c-4d3e-8f40-5a6b7c8d9e02"
U3 = "6f1c2a10-1b2c-4d3e-8f40-5a6b7c8d9e03"
U4 = "6f1c2a10-1b2c-4d3e-8f40-5a6b7c8d9e04"

IMPORT = "/api/v1/inventory/spools/import"


def _upload(text: str):
    return {"file": ("inventory.csv", text.encode("utf-8"), "text/csv")}


async def _spools(db_session: AsyncSession) -> dict[int, Spool]:
    db_session.expire_all()
    return {spool.id: spool for spool in (await db_session.execute(select(Spool))).scalars().all()}


async def _new_spool(db_session: AsyncSession, **values) -> int:
    spool = Spool(material=values.pop("material", "PLA"), **values)
    db_session.add(spool)
    await db_session.commit()
    return spool.id


async def assert_a_moved_inventory_keeps_its_numbers(async_client: AsyncClient, db_session: AsyncSession):
    csv_text = (
        "id,material,brand,color_name,label_identity\n"
        f"7,PLA,Polymaker,Red,{U1}\n"
        f"9,PETG,Bambu Lab,Blue,{U2}\n"
        ",ABS,eSun,Black,\n"
    )

    preview = await async_client.post(f"{IMPORT}?dry_run=true", files=_upload(csv_text))
    assert preview.status_code == 200, preview.text
    assert [row["keeps_id"] for row in preview.json()["rows"]] == [7, 9, None]
    assert preview.json()["warnings"] == []
    # The label identity is the label key's secret half: the file has it, the answer does not.
    assert U1 not in preview.text

    imported = await async_client.post(IMPORT, files=_upload(csv_text))
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == 3

    spools = await _spools(db_session)
    assert (spools[7].material, spools[7].label_identity) == ("PLA", U1)
    assert (spools[9].material, spools[9].label_identity) == ("PETG", U2)
    # The new spool of the file takes the next number after the ones the file kept.
    assert spools[10].material == "ABS"
    # A label printed on the old install still proves its spool here.
    assert matches_label_key(7, spools[7].label_identity, label_key(7, U1))
    # And the counter continues after the import's highest.
    assert await _new_spool(db_session, material="TPU") == 11


async def assert_a_number_given_out_here_is_renumbered(async_client: AsyncClient, db_session: AsyncSession):
    living = await _new_spool(db_session, material="PLA")
    deleted = await _new_spool(db_session, material="PLA")
    gone = (await _spools(db_session))[deleted]
    await db_session.delete(gone)
    await db_session.commit()

    csv_text = (
        "id,material,color_name,label_identity\n"
        f"{living},PETG,In use,{U1}\n"
        f"{deleted},PETG,Deleted,{U2}\n"
        f"500,PETG,First,{U3}\n"
        f"500,PETG,Twice,{U4}\n"
    )

    preview = await async_client.post(f"{IMPORT}?dry_run=true", files=_upload(csv_text))
    assert preview.status_code == 200, preview.text
    assert [row["keeps_id"] for row in preview.json()["rows"]] == [None, None, 500, None]
    assert any("3 spool(s) get a new id" in warning for warning in preview.json()["warnings"])

    imported = await async_client.post(IMPORT, files=_upload(csv_text))
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == 4

    by_colour = {spool.color_name: spool for spool in (await _spools(db_session)).values() if spool.material == "PETG"}
    assert by_colour["First"].id == 500 and by_colour["First"].label_identity == U3
    for colour, asked in (("In use", U1), ("Deleted", U2), ("Twice", U4)):
        renumbered = by_colour[colour]
        # Never the number of the deleted spool, never a number twice; the file's identity went with its number.
        assert renumbered.id not in (living, deleted, 500)
        assert renumbered.label_identity != asked


@pytest.mark.asyncio
@pytest.mark.integration
class TestASpoolKeepsItsNumber:
    async def test_a_moved_inventory_keeps_its_numbers(self, async_client: AsyncClient, db_session: AsyncSession):
        await assert_a_moved_inventory_keeps_its_numbers(async_client, db_session)

    async def test_a_number_given_out_here_is_renumbered(self, async_client: AsyncClient, db_session: AsyncSession):
        await assert_a_number_given_out_here_is_renumbered(async_client, db_session)

    async def test_the_export_writes_the_number_and_the_label_identity(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        number = await _new_spool(db_session, material="PLA", brand="Polymaker")
        identity = (await _spools(db_session))[number].label_identity

        exported = await async_client.get("/api/v1/inventory/spools/export")
        assert exported.status_code == 200, exported.text
        row = next(csv.DictReader(StringIO(exported.text)))

        assert (row["id"], row["label_identity"]) == (str(number), identity)

    async def test_a_label_identity_used_here_is_not_kept(self, async_client: AsyncClient, db_session: AsyncSession):
        await _new_spool(db_session, material="PLA", label_identity=U1)

        csv_text = f"id,material,color_name,label_identity\n800,PETG,Moved,{U1}\n"
        preview = await async_client.post(f"{IMPORT}?dry_run=true", files=_upload(csv_text))
        assert preview.json()["rows"][0]["keeps_id"] == 800
        assert any("get a new label identity" in warning for warning in preview.json()["warnings"])

        assert (await async_client.post(IMPORT, files=_upload(csv_text))).json()["created"] == 1
        moved = (await _spools(db_session))[800]
        assert moved.label_identity not in (None, U1)

    @pytest.mark.parametrize(
        ("cells", "reason"),
        [
            ("abc,PLA,", "id must be a positive whole number"),
            ("0,PLA,", "id must be a positive whole number"),
            # Past the id column's range: kept, it would leave the counter nowhere to go.
            ("2147483648,PLA,", "id must be at most 2147483647"),
            ("12,PLA,not-a-uuid", "label_identity must be a UUID"),
        ],
    )
    async def test_a_bad_number_or_identity_is_a_row_error(self, async_client: AsyncClient, cells: str, reason: str):
        preview = await async_client.post(
            f"{IMPORT}?dry_run=true", files=_upload(f"id,material,label_identity\n{cells}\n")
        )

        row = preview.json()["rows"][0]
        assert row["status"] == "error"
        assert reason in row["reason"]

    async def test_a_number_far_above_this_installs_is_given_a_new_one(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """The counter never goes back: a number a million above this install's own would raise it for good."""
        csv_text = "id,material,color_name\n1000000,PLA,At the bound\n1000005,PLA,Past it\n"

        preview = await async_client.post(f"{IMPORT}?dry_run=true", files=_upload(csv_text))
        assert [row["keeps_id"] for row in preview.json()["rows"]] == [1000000, None]
        assert any("1 spool(s) get a new id" in warning for warning in preview.json()["warnings"])

        assert (await async_client.post(IMPORT, files=_upload(csv_text))).json()["created"] == 2
        past = next(spool for spool in (await _spools(db_session)).values() if spool.color_name == "Past it")
        # The next number after the kept one, not the file's.
        assert past.id == 1000001

    async def test_a_file_without_the_numbers_imports_as_before(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        first = await _new_spool(db_session, material="PLA")

        imported = await async_client.post(IMPORT, files=_upload("material,color_name\nPETG,Plain\n"))

        assert imported.json()["created"] == 1
        plain = next(spool for spool in (await _spools(db_session)).values() if spool.color_name == "Plain")
        assert plain.id == first + 1 and plain.label_identity
