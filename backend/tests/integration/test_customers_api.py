import pytest
from sqlalchemy import select

from backend.app.models.project import Project

pytestmark = pytest.mark.integration

# ``committing_client``, not ``async_client``: these handlers never commit —
# production's ``get_db`` does it after the response. See the fixture docstrings
# in ``backend/tests/conftest.py``.


@pytest.mark.asyncio
async def test_customer_crud_and_figures(committing_client, db_session):
    r = await committing_client.post("/api/v1/customers", json={"name": "ACME", "contact": "acme@example.com"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert r.json()["figures"]["projects"] == 0

    db_session.add_all(
        [
            Project(name="A", customer_id=cid, status="active", price=50.0),
            Project(name="B", customer_id=cid, status="completed"),
        ]
    )
    await db_session.commit()

    r = await committing_client.get(f"/api/v1/customers/{cid}")
    figs = r.json()["figures"]
    assert figs["projects"] == 2 and figs["active"] == 1 and figs["completed"] == 1 and figs["total_price"] == 50.0

    r = await committing_client.patch(f"/api/v1/customers/{cid}", json={"notes": "pays late"})
    assert r.json()["notes"] == "pays late" and r.json()["contact"] == "acme@example.com"

    r = await committing_client.get("/api/v1/customers")
    assert [c["name"] for c in r.json()] == ["ACME"]

    r = await committing_client.delete(f"/api/v1/customers/{cid}")
    assert r.status_code == 200
    db_session.expire_all()
    kept = (await db_session.execute(select(Project).where(Project.name == "A"))).scalar_one()
    assert kept.customer_id is None  # projects survive, unlinked


@pytest.mark.asyncio
async def test_patch_null_clears_an_optional_field_but_never_the_name(committing_client):
    cid = (
        await committing_client.post("/api/v1/customers", json={"name": "ACME", "contact": "c", "notes": "n"})
    ).json()["id"]

    # Absent field: left alone. Explicit null on an optional field: cleared.
    r = await committing_client.patch(f"/api/v1/customers/{cid}", json={"notes": "x"})
    assert r.status_code == 200 and r.json()["notes"] == "x" and r.json()["contact"] == "c"
    r = await committing_client.patch(f"/api/v1/customers/{cid}", json={"contact": None})
    assert r.status_code == 200 and r.json()["contact"] is None and r.json()["notes"] == "x"

    # ``name`` is NOT NULL — 422 from the schema, never an IntegrityError.
    assert (await committing_client.patch(f"/api/v1/customers/{cid}", json={"name": None})).status_code == 422
    assert (await committing_client.get(f"/api/v1/customers/{cid}")).json()["name"] == "ACME"


@pytest.mark.asyncio
async def test_unknown_customer_is_404(committing_client):
    assert (await committing_client.get("/api/v1/customers/999")).status_code == 404
    assert (await committing_client.patch("/api/v1/customers/999", json={"name": "x"})).status_code == 404
