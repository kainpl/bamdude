import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.project import Project

pytestmark = pytest.mark.integration


@pytest.fixture
async def async_client(async_client, test_engine):
    """The conftest client, with ``get_db`` committing like the real one.

    The customers routes never ``commit()`` — production's ``get_db`` does it
    after the response (spec §API). The shared conftest override only yields a
    session and closes it, so a handler that merely flushes has its write
    rolled back and the next request cannot see it. Existing route modules hide
    this by committing themselves; these ones deliberately do not.

    Overriding the fixture here keeps the change to this module. ``async_client``
    clears ``app.dependency_overrides`` on teardown, so nothing leaks.
    """
    from backend.app.core.database import get_db
    from backend.app.main import app

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with maker() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_get_db
    yield async_client


@pytest.mark.asyncio
async def test_customer_crud_and_figures(async_client, db_session):
    r = await async_client.post("/api/v1/customers", json={"name": "ACME", "contact": "acme@example.com"})
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

    r = await async_client.get(f"/api/v1/customers/{cid}")
    figs = r.json()["figures"]
    assert figs["projects"] == 2 and figs["active"] == 1 and figs["completed"] == 1 and figs["total_price"] == 50.0

    r = await async_client.patch(f"/api/v1/customers/{cid}", json={"notes": "pays late"})
    assert r.json()["notes"] == "pays late" and r.json()["contact"] == "acme@example.com"

    r = await async_client.get("/api/v1/customers")
    assert [c["name"] for c in r.json()] == ["ACME"]

    r = await async_client.delete(f"/api/v1/customers/{cid}")
    assert r.status_code == 200
    db_session.expire_all()
    kept = (await db_session.execute(select(Project).where(Project.name == "A"))).scalar_one()
    assert kept.customer_id is None  # projects survive, unlinked


@pytest.mark.asyncio
async def test_unknown_customer_is_404(async_client):
    assert (await async_client.get("/api/v1/customers/999")).status_code == 404
    assert (await async_client.patch("/api/v1/customers/999", json={"name": "x"})).status_code == 404
