"""A dispatch note: who sees it, and who sees its recipient (WS-13 E13 T15, O25 / R10).

A note is the stock's document, an order's issue and a customer's: ``stock:read`` lists every
note, ``orders:read`` an order's (``project_id``), ``customers:read`` a customer's
(``customer_id``). Its sensitive snapshot — the recipient's name, phone and delivery, the note,
the supplier's details — goes to whoever keeps the contacts or ships the goods
(``customers:read`` or ``stock:move``): one rule for the list, the document, its print and the
PATCH answer. Without it a row is minimal and marked ``restricted``, and the document does not
open. A key with only the status scope reads no contact anywhere — ``customers:read`` rides the
Workshop's manage scope now.
"""

import pytest
from httpx import AsyncClient

from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = pytest.mark.integration

PHONE = "+380671234567"


@pytest.fixture
async def note(committing_client: AsyncClient):
    """As the admin: a customer with a contact, a finished unit, and a manual issue of it."""
    customer = await committing_client.post(
        "/api/v1/customers/",
        json={"name": "Note Co", "contacts": [{"name": "Ira", "phone": PHONE, "delivery_details": "Lviv, 7"}]},
    )
    product = (await committing_client.post("/api/v1/products/", json={"name": "Note lamp"})).json()["id"]
    receipt = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "receipt", "product_id": product, "qty": 2}
    )
    assert receipt.status_code == 200, receipt.text
    issued = await committing_client.post(
        "/api/v1/stock/moves",
        json={"kind": "issue", "item_id": receipt.json()["id"], "qty": 1, "customer_id": customer.json()["id"]},
    )
    assert issued.status_code == 200, issued.text
    return {"id": issued.json()["issue_id"], "customer": customer.json()["id"]}


def _mine(rows: list[dict], note_id: int) -> dict:
    [row] = [r for r in rows if r["id"] == note_id]
    return row


@pytest.mark.asyncio
async def test_a_stock_reader_lists_minimal_rows_and_cannot_open_the_document(committing_client, db_session, note):
    await _user(db_session, "dn_reader", ["stock:read"])
    headers = await _jwt("dn_reader")
    row = _mine((await committing_client.get("/api/v1/stock-issues/", headers=headers)).json()["items"], note["id"])
    assert row["restricted"] is True
    assert row["recipient_phone"] is None and row["delivery_details"] is None and row["recipient_name"] is None
    assert row["customer_name"] == "Note Co"
    opened = await committing_client.get(f"/api/v1/stock-issues/{note['id']}", headers=headers)
    assert opened.status_code == 403
    assert opened.json()["detail"]["error"] == "dispatch_note_restricted"


@pytest.mark.asyncio
async def test_the_shipping_right_opens_the_whole_document(committing_client, db_session, note):
    await _user(db_session, "dn_shipper", ["stock:read", "stock:move"])
    headers = await _jwt("dn_shipper")
    row = _mine((await committing_client.get("/api/v1/stock-issues/", headers=headers)).json()["items"], note["id"])
    assert row["recipient_phone"] == PHONE
    assert not row["restricted"]
    doc = await committing_client.get(f"/api/v1/stock-issues/{note['id']}", headers=headers)
    assert doc.status_code == 200, doc.text
    assert doc.json()["recipient_phone"] == PHONE
    patched = await committing_client.patch(
        f"/api/v1/stock-issues/{note['id']}", json={"waybill": "TTN-1"}, headers=headers
    )
    assert patched.status_code == 200 and patched.json()["recipient_phone"] == PHONE


@pytest.mark.asyncio
async def test_a_customers_reader_sees_that_customers_notes_and_no_others(committing_client, db_session, note):
    await _user(db_session, "dn_contacts", ["customers:read"])
    headers = await _jwt("dn_contacts")
    assert (await committing_client.get("/api/v1/stock-issues/", headers=headers)).status_code == 403
    rows = await committing_client.get(
        "/api/v1/stock-issues/", params={"customer_id": note["customer"]}, headers=headers
    )
    assert rows.status_code == 200, rows.text
    assert _mine(rows.json()["items"], note["id"])["recipient_phone"] == PHONE
    assert (await committing_client.get(f"/api/v1/stock-issues/{note['id']}", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_a_reader_without_any_of_the_three_contexts_is_refused(committing_client, db_session, note):
    await _user(db_session, "dn_stranger", ["inventory:read"])
    assert (await committing_client.get("/api/v1/stock-issues/", headers=await _jwt("dn_stranger"))).status_code == 403


@pytest.mark.asyncio
async def test_a_minimal_reader_cannot_find_a_note_by_its_recipient(committing_client, db_session, note):
    await _user(db_session, "dn_searcher", ["stock:read"])
    found = await committing_client.get("/api/v1/stock-issues/", params={"q": "Ira"}, headers=await _jwt("dn_searcher"))
    assert [r for r in found.json()["items"] if r["id"] == note["id"]] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["x-api-key", "bearer"])
async def test_a_status_key_reads_no_contact_by_any_route(committing_client, db_session, note, header):
    owner = await _user(db_session, f"dn_key_owner_{header}", ["stock:read", "customers:read", "stock:move"])
    raw = await _key(db_session, owner, can_read_status=True, can_manage_projects=False)
    headers = {"X-API-Key": raw} if header == "x-api-key" else {"Authorization": f"Bearer {raw}"}
    row = _mine((await committing_client.get("/api/v1/stock-issues/", headers=headers)).json()["items"], note["id"])
    assert row["recipient_phone"] is None
    assert (await committing_client.get(f"/api/v1/stock-issues/{note['id']}", headers=headers)).status_code == 403
    assert (await committing_client.get("/api/v1/customers/", headers=headers)).status_code == 403
    assert (await committing_client.get(f"/api/v1/customers/{note['customer']}", headers=headers)).status_code == 403
