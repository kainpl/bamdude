"""Paper geometries you can create, not only choose from.

The sheet table has existed since m146 with two seeded rows and no way to add a
third — "choosing between two is fine, drawing a third is not", which is what
this closes.

⚠️ The rules are the ones the design side already settled, applied to paper:
a seeded geometry is read-only and duplicated rather than edited, and a grid
that runs off its page is refused on save rather than discovered on stock.
"""

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

BASE = "/api/v1/label-templates/sheets"

A_SHEET = {
    "name": "My stock",
    "page_size": "A4",
    "cell_width_mm": 63.5,
    "cell_height_mm": 38.1,
    "cols": 3,
    "rows": 7,
    "margin_top_mm": 15.0,
    "margin_left_mm": 7.0,
    "gap_x_mm": 2.5,
    "gap_y_mm": 0.0,
}


class TestCreating:
    async def test_a_sheet_is_created_and_listed(self, async_client: AsyncClient):
        created = await async_client.post(BASE, json=A_SHEET)
        assert created.status_code == 201, created.text

        listed = (await async_client.get(BASE)).json()
        assert any(row["id"] == created.json()["id"] for row in listed)

    async def test_a_new_sheet_is_not_built_in(self, async_client: AsyncClient):
        created = (await async_client.post(BASE, json=A_SHEET)).json()

        assert created["is_builtin"] is False
        assert created["builtin_key"] is None

    async def test_a_grid_that_runs_off_the_paper_is_refused(self, async_client: AsyncClient):
        """⚠️ Refused, not warned about: the discovery costs a sheet of stock."""
        refused = await async_client.post(BASE, json={**A_SHEET, "cols": 4})

        assert refused.status_code == 422
        assert "wide" in refused.json()["detail"]

    async def test_a5_is_judged_against_a5(self, async_client: AsyncClient):
        """The grid that fits A4 does not fit A5, and the resolver knows the
        difference now — it used to answer Letter for anything but A4."""
        assert (await async_client.post(BASE, json={**A_SHEET, "page_size": "A5"})).status_code == 422

    async def test_an_unknown_paper_is_refused(self, async_client: AsyncClient):
        assert (await async_client.post(BASE, json={**A_SHEET, "page_size": "A3"})).status_code == 422


class TestTheSeededOnesAreFrozen:
    @staticmethod
    async def _a_builtin(async_client: AsyncClient, db_session) -> dict:
        """A seeded row, inserted here rather than taken from the seed.

        ⚠️ The integration database does not run the label seed, so reading one
        out of the list would make these tests pass or fail on whether some
        other fixture happened to create one.
        """
        from backend.app.models.label_template import LabelSheet

        db_session.add(LabelSheet(builtin_key="avery_test", **A_SHEET))
        await db_session.commit()

        rows = (await async_client.get(BASE)).json()
        builtin = next(row for row in rows if row["builtin_key"] == "avery_test")
        assert builtin["is_builtin"] is True
        return builtin

    async def test_editing_one_is_refused(self, async_client: AsyncClient, db_session):
        builtin = await self._a_builtin(async_client, db_session)

        refused = await async_client.put(f"{BASE}/{builtin['id']}", json=A_SHEET)

        assert refused.status_code == 409
        assert "duplicate" in refused.json()["detail"]

    async def test_deleting_one_is_refused(self, async_client: AsyncClient, db_session):
        builtin = await self._a_builtin(async_client, db_session)

        assert (await async_client.delete(f"{BASE}/{builtin['id']}")).status_code == 409

    async def test_duplicating_one_gives_an_editable_copy(self, async_client: AsyncClient, db_session):
        builtin = await self._a_builtin(async_client, db_session)

        copy = await async_client.post(f"{BASE}/{builtin['id']}/duplicate")

        assert copy.status_code == 201
        assert copy.json()["is_builtin"] is False
        assert copy.json()["cols"] == builtin["cols"], "the geometry travels with the copy"
        assert (await async_client.put(f"{BASE}/{copy.json()['id']}", json=A_SHEET)).status_code == 200


class TestEditingYourOwn:
    async def test_a_change_sticks(self, async_client: AsyncClient):
        created = (await async_client.post(BASE, json=A_SHEET)).json()

        updated = await async_client.put(f"{BASE}/{created['id']}", json={**A_SHEET, "rows": 5})

        assert updated.status_code == 200
        assert updated.json()["rows"] == 5

    async def test_deleting_yours_works(self, async_client: AsyncClient):
        created = (await async_client.post(BASE, json=A_SHEET)).json()

        assert (await async_client.delete(f"{BASE}/{created['id']}")).status_code == 204
        assert all(row["id"] != created["id"] for row in (await async_client.get(BASE)).json())


A_DESIGN = {
    "name": "preview design",
    "width_mm": 60.0,
    "height_mm": 35.0,
    "elements": [{"type": "text", "content": "{brand}", "x_mm": 2, "y_mm": 2, "w_mm": 40, "h_mm": 6}],
}


class TestThePagePreview:
    @staticmethod
    async def _design(async_client: AsyncClient, **overrides) -> dict:
        created = await async_client.post("/api/v1/label-templates", json={**A_DESIGN, **overrides})
        assert created.status_code == 201, created.text
        return created.json()

    async def test_it_returns_a_page(self, async_client: AsyncClient):
        design = await self._design(async_client)

        answer = await async_client.post(f"{BASE}/preview", json={"sheet": A_SHEET, "template_id": design["id"]})

        assert answer.status_code == 200
        assert answer.headers["content-type"] == "application/pdf"
        assert answer.content.startswith(b"%PDF")

    async def test_a_design_too_big_for_the_cell_is_said_rather_than_scaled(self, async_client: AsyncClient):
        """⚠️ A design prints at its own size or not at all. Fractional scaling
        destroys bar ratios silently, so the answer is a sentence."""
        design = await self._design(async_client)

        answer = await async_client.post(
            f"{BASE}/preview",
            json={
                "sheet": {**A_SHEET, "cell_width_mm": 20.0, "cell_height_mm": 10.0, "cols": 2, "rows": 2},
                "template_id": design["id"],
            },
        )

        assert answer.status_code == 200
        assert "does not fit" in answer.headers.get("x-label-warnings", "")
