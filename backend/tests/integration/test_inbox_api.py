"""/api/v1/inbox — a user sees only their own rows; filters, cursor, read/clear, subscriptions.

Spec: vault 60-specs/notification-center-spec §6.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.app.core.auth import create_access_token
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification


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
    assert body["unread_count"] == 1 and body["next_before_id"] is None
    assert body["items"][0]["group"] == "print" and body["items"][0]["severity"] == "error"

    r = await async_client.get("/api/v1/inbox/unread-count")
    assert r.json() == {"unread_count": 1}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filters_and_cursor(async_client, db_session):
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
    r = await async_client.get("/api/v1/inbox/", params={"event_type": "print_paused"})
    assert [i["title"] for i in r.json()["items"]] == ["old-warning"]
    assert (await async_client.get("/api/v1/inbox/", params={"severity": "loud"})).status_code == 422

    r = await async_client.get("/api/v1/inbox/", params={"limit": 2})
    page = r.json()
    assert len(page["items"]) == 2 and page["next_before_id"] == page["items"][-1]["id"]
    r = await async_client.get("/api/v1/inbox/", params={"limit": 2, "before_id": page["next_before_id"]})
    page2 = r.json()
    assert len(page2["items"]) == 2 and page2["next_before_id"] is None
    assert {i["id"] for i in page["items"]}.isdisjoint({i["id"] for i in page2["items"]})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_read_read_all_delete_and_clear_are_mine_only(async_client, db_session):
    me = await _admin_id(db_session)
    other = await _other_user(db_session)
    mine = _row(me, "mine")
    mine_warn = _row(me, "mine-warn", severity="warning", event_type="print_paused")
    theirs = _row(other.id, "theirs")
    db_session.add_all([mine, mine_warn, theirs])
    await db_session.commit()
    for row in (mine, mine_warn, theirs):
        await db_session.refresh(row)

    r = await async_client.post(f"/api/v1/inbox/{mine.id}/read")
    assert r.status_code == 200 and r.json()["read_at"] is not None
    assert (await async_client.post(f"/api/v1/inbox/{mine.id}/read")).status_code == 200  # idempotent
    assert (await async_client.post(f"/api/v1/inbox/{theirs.id}/read")).status_code == 404

    r = await async_client.post("/api/v1/inbox/read-all", params={"severity": "warning"})
    assert r.json() == {"updated": 1}
    assert (await async_client.get("/api/v1/inbox/unread-count")).json() == {"unread_count": 0}

    assert (await async_client.delete(f"/api/v1/inbox/{theirs.id}")).status_code == 404
    assert (await async_client.delete(f"/api/v1/inbox/{mine.id}")).status_code == 204
    r = await async_client.delete("/api/v1/inbox/")
    assert r.json() == {"deleted": 1}
    db_session.expire_all()
    left = (await db_session.execute(select(UserNotification.title))).scalars().all()
    assert left == ["theirs"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_subscriptions_default_custom_reset_and_unknown(async_client, db_session):
    r = await async_client.get("/api/v1/inbox/subscriptions")
    assert r.status_code == 200
    body = r.json()
    assert body["is_default"] is True and len(body["events"]) == 37
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
