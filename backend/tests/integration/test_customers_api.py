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


@pytest.mark.asyncio
async def test_list_figures_are_light_while_the_detail_stays_full(committing_client, db_session):
    acme = (await committing_client.post("/api/v1/customers", json={"name": "ACME"})).json()["id"]
    beta = (await committing_client.post("/api/v1/customers", json={"name": "Beta"})).json()["id"]
    await committing_client.post("/api/v1/customers", json={"name": "Zed"})  # no projects at all
    db_session.add_all(
        [
            Project(name="a1", customer_id=acme, status="active", price=50.0),
            Project(name="a2", customer_id=acme, status="cancelled", price=25.5),
            Project(name="b1", customer_id=beta, status="completed", price=10.0),
        ]
    )
    await db_session.commit()

    by_name = {c["name"]: c["figures"] for c in (await committing_client.get("/api/v1/customers")).json()}
    assert by_name["ACME"] == {"projects": 2, "active": 1, "completed": 0, "cancelled": 1, "total_price": 75.5}
    assert by_name["Beta"] == {"projects": 1, "active": 0, "completed": 1, "cancelled": 0, "total_price": 10.0}
    assert by_name["Zed"] == {"projects": 0, "active": 0, "completed": 0, "cancelled": 0, "total_price": 0.0}

    # The archive-derived keys are the detail endpoint's job, and only its.
    assert "ordered" not in by_name["ACME"] and "printed" not in by_name["ACME"]
    detail = (await committing_client.get(f"/api/v1/customers/{acme}")).json()["figures"]
    assert detail["projects"] == 2 and detail["total_price"] == 75.5
    assert {"ordered", "printed", "total_cost"} <= set(detail)


@pytest.mark.asyncio
async def test_the_name_is_trimmed_on_create_and_on_update(committing_client):
    r = await committing_client.post("/api/v1/customers", json={"name": "  ACME  "})
    assert r.status_code == 200 and r.json()["name"] == "ACME"
    cid = r.json()["id"]

    r = await committing_client.patch(f"/api/v1/customers/{cid}", json={"name": "  Renamed  "})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"

    # Whitespace-only is the same as empty: 422 on both paths, never a stored blank.
    assert (await committing_client.post("/api/v1/customers", json={"name": "   "})).status_code == 422
    assert (await committing_client.patch(f"/api/v1/customers/{cid}", json={"name": "   "})).status_code == 422
