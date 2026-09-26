"""/api/v1/inbox — a user sees only their own rows; filters, cursor, read/clear, subscriptions.

Spec: vault 60-specs/notification-center-spec §6.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.core.auth import create_access_token
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.services.notification_events import EVENT_CATALOG


async def _admin_id(db) -> int:
    return (await db.execute(select(User.id).where(User.username == "test_admin"))).scalar_one()


async def _other_user(db) -> User:
    user = User(username="other", password_hash="x", role="user", is_active=True)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _row(user_id, title, severity="error", event_type="print_failed", printer_id=None, read=False, age_days=0):
    created = datetime.utcnow() - timedelta(days=age_days)
    return UserNotification(
        user_id=user_id,
        event_type=event_type,
        severity=severity,
        title=title,
        message="m",
        printer_id=printer_id,
        printer_name=f"P{printer_id}" if printer_id else None,
        created_at=created,
        read_at=created if read else None,
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_shows_only_my_rows_newest_first_with_unread_count(async_client, db_session):
    me = await _admin_id(db_session)
    other = await _other_user(db_session)
    db_session.add_all([_row(me, "a"), _row(me, "b", read=True), _row(other.id, "theirs")])
    await db_session.commit()

    r = await async_client.get("/api/v1/inbox/")
    assert r.status_code == 200
    body = r.json()
    assert [i["title"] for i in body["items"]] == ["b", "a"]
    assert body["unread_count"] == 1
    assert (body["total"], body["current_page"], body["per_page"], body["last_page"]) == (2, 1, 24, 1)
    assert body["items"][0]["group"] == "print" and body["items"][0]["severity"] == "error"

    r = await async_client.get("/api/v1/inbox/unread-count")
    assert r.json() == {"unread_count": 1}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filters_and_pages(async_client, db_session):
    me = await _admin_id(db_session)
    db_session.add_all(
        [
            _row(me, "old-warning", severity="warning", event_type="print_paused", age_days=10),
            _row(me, "err-p1", printer_id=1),
            _row(me, "err-p2", printer_id=2),
            _row(me, "err-p1-read", printer_id=1, read=True),
        ]
    )
    await db_session.commit()

    r = await async_client.get("/api/v1/inbox/", params={"severity": "warning"})
    assert [i["title"] for i in r.json()["items"]] == ["old-warning"]
    r = await async_client.get("/api/v1/inbox/", params={"printer_id": 1, "unread_only": "true"})
    assert [i["title"] for i in r.json()["items"]] == ["err-p1"]
    since = (datetime.utcnow() - timedelta(days=1)).isoformat()
    r = await async_client.get("/api/v1/inbox/", params={"since": since})
    assert "old-warning" not in [i["title"] for i in r.json()["items"]]
    # ⚠️ An offset-bearing ``since`` is converted to UTC, not stripped of its
    # offset. One hour ago, spelled in +03:00, reads as two hours from now if the
    # offset is merely dropped — which would return nothing at all here.
    aware_since = (datetime.now(timezone.utc) - timedelta(hours=1)).astimezone(timezone(timedelta(hours=3)))
    r = await async_client.get("/api/v1/inbox/", params={"since": aware_since.isoformat()})
    assert sorted(i["title"] for i in r.json()["items"]) == ["err-p1", "err-p1-read", "err-p2"]
    r = await async_client.get("/api/v1/inbox/", params={"event_type": "print_paused"})
    assert [i["title"] for i in r.json()["items"]] == ["old-warning"]
    assert (await async_client.get("/api/v1/inbox/", params={"severity": "loud"})).status_code == 422

    # Numbered pages, the same shape the Archives and Inventory tables use, so
    # the shared PaginationBar can draw this list without a translation layer.
    r = await async_client.get("/api/v1/inbox/", params={"per_page": 2})
    page = r.json()
    assert len(page["items"]) == 2
    assert (page["total"], page["current_page"], page["per_page"], page["last_page"]) == (4, 1, 2, 2)
    r = await async_client.get("/api/v1/inbox/", params={"per_page": 2, "page": 2})
    page2 = r.json()
    assert len(page2["items"]) == 2 and page2["current_page"] == 2
    assert {i["id"] for i in page["items"]}.isdisjoint({i["id"] for i in page2["items"]})
    # A page past the end is empty, not an error — the table can ask for one
    # after a filter shrinks the result.
    r = await async_client.get("/api/v1/inbox/", params={"per_page": 2, "page": 9})
    assert r.status_code == 200 and r.json()["items"] == []
    # "All" is the size selector's last option: one page holding everything.
    r = await async_client.get("/api/v1/inbox/", params={"all": "true"})
    every = r.json()
    assert len(every["items"]) == 4 and every["last_page"] == 1 and every["per_page"] == 4


@pytest.mark.asyncio
@pytest.mark.integration
async def test_read_read_all_delete_and_clear_are_mine_only(async_client, db_session):
    me = await _admin_id(db_session)
    other = await _other_user(db_session)
    mine = _row(me, "mine")
    mine_warn = _row(me, "mine-warn", severity="warning", event_type="print_paused")
    theirs = _row(other.id, "theirs")
    # ⚠️ This one MATCHES the read-all filter below. Without it the severity gate
    # alone excluded the other user's row, so dropping ``user_id`` from read-all's
    # WHERE clause left the suite green — the scoping went unproven.
    theirs_warn = _row(other.id, "theirs-warn", severity="warning", event_type="print_paused")
    db_session.add_all([mine, mine_warn, theirs, theirs_warn])
    await db_session.commit()
    for row in (mine, mine_warn, theirs, theirs_warn):
        await db_session.refresh(row)
    mine_id, theirs_id, theirs_warn_id = mine.id, theirs.id, theirs_warn.id

    r = await async_client.post(f"/api/v1/inbox/{mine_id}/read")
    assert r.status_code == 200 and r.json()["read_at"] is not None
    assert (await async_client.post(f"/api/v1/inbox/{mine_id}/read")).status_code == 200  # idempotent
    assert (await async_client.post(f"/api/v1/inbox/{theirs_id}/read")).status_code == 404

    r = await async_client.post("/api/v1/inbox/read-all", params={"severity": "warning"})
    assert r.json() == {"updated": 1}
    assert (await async_client.get("/api/v1/inbox/unread-count")).json() == {"unread_count": 0}
    db_session.expire_all()
    theirs_warn_read_at = (
        await db_session.execute(select(UserNotification.read_at).where(UserNotification.id == theirs_warn_id))
    ).scalar_one()
    assert theirs_warn_read_at is None, "read-all reached into another user's inbox"

    assert (await async_client.delete(f"/api/v1/inbox/{theirs_id}")).status_code == 404
    assert (await async_client.delete(f"/api/v1/inbox/{mine_id}")).status_code == 204
    r = await async_client.delete("/api/v1/inbox/")
    assert r.json() == {"deleted": 1}
    db_session.expire_all()
    left = (await db_session.execute(select(UserNotification.title))).scalars().all()
    assert sorted(left) == ["theirs", "theirs-warn"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_subscriptions_default_custom_reset_and_unknown(async_client, db_session):
    r = await async_client.get("/api/v1/inbox/subscriptions")
    assert r.status_code == 200
    body = r.json()
    # Every catalogued event, whatever the count — a literal here went stale with each new event.
    assert body["is_default"] is True and len(body["events"]) == len(EVENT_CATALOG)
    by_type = {e["event_type"]: e for e in body["events"]}
    assert by_type["print_failed"]["subscribed"] is True and by_type["print_complete"]["subscribed"] is False
    assert by_type["print_failed"]["group"] == "print" and by_type["print_failed"]["severity"] == "error"

    r = await async_client.put("/api/v1/inbox/subscriptions", json={"events": ["print_complete"]})
    body = r.json()
    assert body["is_default"] is False
    assert {e["event_type"] for e in body["events"] if e["subscribed"]} == {"print_complete"}

    r = await async_client.put("/api/v1/inbox/subscriptions", json={"events": []})
    assert not [e for e in r.json()["events"] if e["subscribed"]]

    r = await async_client.put("/api/v1/inbox/subscriptions", json={"events": None})
    assert r.json()["is_default"] is True

    r = await async_client.put("/api/v1/inbox/subscriptions", json={"events": ["no_such_event"]})
    assert r.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_viewer_sees_their_own_inbox_and_nobody_elses(async_client, db_session):
    from backend.app.models.group import Group

    me = await _admin_id(db_session)
    other = await _other_user(db_session)
    viewers = (await db_session.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()
    other.groups.append(viewers)  # Viewers hold notifications:inbox (DEFAULT_GROUPS, Task 2)
    db_session.add_all([_row(other.id, "theirs"), _row(me, "mine")])
    await db_session.commit()

    token = create_access_token(data={"sub": other.username})
    r = await async_client.get("/api/v1/inbox/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert [i["title"] for i in r.json()["items"]] == ["theirs"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_user_without_the_permission_is_refused(async_client, db_session):
    other = await _other_user(db_session)  # in no group at all
    token = create_access_token(data={"sub": other.username})
    r = await async_client.get("/api/v1/inbox/", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_token_is_401(async_client):
    r = await async_client.get("/api/v1/inbox/", headers={"Authorization": ""})
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_api_key_is_refused_the_inbox(async_client, db_session):
    """The inbox belongs to a person (spec §10).

    ``Permission.NOTIFICATIONS_INBOX`` sits in ``_APIKEY_DENIED_PERMISSIONS`` — a
    key has no ``user_id`` to put anything in. The drift guard proves the
    permission landed in exactly one bucket; only this proves the door is shut.
    The owner here DOES hold the permission, so a 403 can only come from the key
    bucket, never from the owner lacking authority.
    """
    from backend.app.core.auth import generate_api_key
    from backend.app.models.api_key import APIKey
    from backend.app.models.group import Group

    other = await _other_user(db_session)
    viewers = (await db_session.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()
    other.groups.append(viewers)
    raw, key_hash, key_prefix = generate_api_key()
    db_session.add(
        APIKey(
            name=f"k-{key_prefix}",
            key_hash=key_hash,
            key_prefix=key_prefix,
            enabled=True,
            user_id=other.id,
            can_read_status=True,
        )
    )
    await db_session.commit()

    del async_client.headers["Authorization"]
    r = await async_client.get("/api/v1/inbox/", headers={"X-API-Key": raw})
    assert r.status_code == 403, r.text
