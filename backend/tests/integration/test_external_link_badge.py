"""A count an app reports on its sidebar link (upstream Bambuddy 29a81520d, N45).

An app added to the sidebar shows a number on its link, the way the queue shows its
waiting jobs: it reports the count for its own address with an API key allowed to send
notifications (``PUT /api/v1/external-links/badge``). A count not reported again for ten
minutes is not shown, so an app that stopped leaves no old number. The count belongs to
the link's address: a deleted link or a changed address takes it along.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from backend.app.api.routes.external_links import BADGE_FRESH_FOR
from backend.app.core.auth import generate_api_key
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.external_link import ExternalLink, ExternalLinkBadge
from backend.app.models.group import Group
from backend.app.models.user import User
from backend.tests.auth_helpers import bind_test_key

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

BADGE = "/api/v1/external-links/badge"
LINKS = "/api/v1/external-links/"


async def _sender(db_session, *, flag: bool = True, permissions: list[str] | None = None) -> str:
    group = Group(
        name="grp-orders-app",
        description="test",
        permissions=permissions if permissions is not None else [Permission.NOTIFICATIONS_UPDATE.value],
    )
    db_session.add(group)
    await db_session.flush()
    owner = User(username="orders-app", email="orders@example.com", password_hash="x", role="user", is_active=True)
    owner.groups.append(group)
    db_session.add(owner)
    await db_session.commit()
    await db_session.refresh(owner)
    raw, key_hash, key_prefix = generate_api_key()
    key = APIKey(
        name="Orders",
        key_hash=key_hash,
        key_prefix=key_prefix,
        enabled=True,
        user_id=owner.id,
        can_send_notifications=flag,
    )
    await bind_test_key(db_session, key, owner)
    db_session.add(key)
    await db_session.commit()
    return raw


async def _link(db_session, url: str, name: str = "Orders") -> ExternalLink:
    link = ExternalLink(name=name, url=url, icon="link")
    db_session.add(link)
    await db_session.commit()
    await db_session.refresh(link)
    return link


async def _counts(async_client) -> dict[int, int | None]:
    response = await async_client.get(LINKS)
    assert response.status_code == 200, response.text
    return {row["id"]: row["badge_count"] for row in response.json()}


async def _badge_rows(db_session) -> list[ExternalLinkBadge]:
    db_session.expire_all()
    return list((await db_session.execute(select(ExternalLinkBadge))).scalars().all())


# --- who may report ------------------------------------------------------------------------------------------


async def test_a_key_without_send_notifications_is_refused(async_client, db_session):
    await _link(db_session, "http://orders.lan:8080")
    raw = await _sender(db_session, flag=False)
    response = await async_client.put(
        BADGE, json={"url": "http://orders.lan:8080", "count": 3}, headers={"X-API-Key": raw}
    )
    assert response.status_code == 403, response.text
    assert await _badge_rows(db_session) == []


async def test_a_browser_session_is_refused(async_client, db_session):
    # The test client signs in as the administrator: the door is for programs, not people.
    await _link(db_session, "http://orders.lan:8080")
    response = await async_client.put(BADGE, json={"url": "http://orders.lan:8080", "count": 3})
    assert response.status_code == 403, response.text


async def test_an_owner_without_notifications_update_is_refused(async_client, db_session):
    await _link(db_session, "http://orders.lan:8080")
    raw = await _sender(db_session, permissions=[])
    response = await async_client.put(
        BADGE, json={"url": "http://orders.lan:8080", "count": 3}, headers={"X-API-Key": raw}
    )
    assert response.status_code == 403, response.text


# --- what it sets ----------------------------------------------------------------------------------------------


async def test_the_count_shows_on_every_link_with_the_apps_address(async_client, db_session):
    first = await _link(db_session, "http://orders.lan:8080/")
    second = await _link(db_session, "http://Orders.LAN:8080", name="Orders (tab)")
    other = await _link(db_session, "http://spoolman.lan:7912")
    raw = await _sender(db_session)

    response = await async_client.put(
        BADGE, json={"url": "http://ORDERS.lan:8080", "count": 7}, headers={"X-API-Key": raw}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"links": 2}
    assert await _counts(async_client) == {first.id: 7, second.id: 7, other.id: None}


async def test_a_login_and_the_default_port_do_not_count(async_client, db_session):
    link = await _link(db_session, "https://orders.lan/app")
    raw = await _sender(db_session)
    response = await async_client.put(
        BADGE, json={"url": "https://user@orders.lan:443/app/", "count": 2}, headers={"X-API-Key": raw}
    )
    assert response.status_code == 200, response.text
    assert (await _counts(async_client))[link.id] == 2


async def test_no_link_at_the_address_is_404(async_client, db_session):
    await _link(db_session, "http://orders.lan:8080")
    raw = await _sender(db_session)
    response = await async_client.put(
        BADGE, json={"url": "http://orders.lan:9090", "count": 1}, headers={"X-API-Key": raw}
    )
    assert response.status_code == 404, response.text


async def test_a_new_report_replaces_the_count(async_client, db_session):
    link_id = (await _link(db_session, "http://orders.lan:8080")).id
    raw = await _sender(db_session)
    for count in (5, 0):
        response = await async_client.put(
            BADGE, json={"url": "http://orders.lan:8080", "count": count}, headers={"X-API-Key": raw}
        )
        assert response.status_code == 200, response.text
    assert [(row.link_id, row.count) for row in await _badge_rows(db_session)] == [(link_id, 0)]
    assert (await _counts(async_client))[link_id] == 0


async def test_a_count_not_reported_again_is_not_shown(async_client, db_session):
    link = await _link(db_session, "http://orders.lan:8080")
    raw = await _sender(db_session)
    await async_client.put(BADGE, json={"url": "http://orders.lan:8080", "count": 4}, headers={"X-API-Key": raw})
    old = datetime.now(timezone.utc).replace(tzinfo=None) - BADGE_FRESH_FOR - timedelta(seconds=1)
    await db_session.execute(update(ExternalLinkBadge).values(updated_at=old))
    await db_session.commit()
    assert (await _counts(async_client))[link.id] is None


@pytest.mark.parametrize("count", [-1, 1_000_001])
async def test_an_impossible_count_is_refused(async_client, db_session, count):
    await _link(db_session, "http://orders.lan:8080")
    raw = await _sender(db_session)
    response = await async_client.put(
        BADGE, json={"url": "http://orders.lan:8080", "count": count}, headers={"X-API-Key": raw}
    )
    assert response.status_code == 422, response.text


# --- the count belongs to the link's address ----------------------------------------------------------------


async def test_deleting_the_link_removes_its_count(async_client, db_session):
    link = await _link(db_session, "http://orders.lan:8080")
    raw = await _sender(db_session)
    await async_client.put(BADGE, json={"url": "http://orders.lan:8080", "count": 4}, headers={"X-API-Key": raw})
    response = await async_client.delete(f"{LINKS}{link.id}")
    assert response.status_code == 200, response.text
    assert await _badge_rows(db_session) == []


async def test_another_address_drops_the_count_and_a_new_name_keeps_it(async_client, db_session):
    link = await _link(db_session, "http://orders.lan:8080")
    raw = await _sender(db_session)
    await async_client.put(BADGE, json={"url": "http://orders.lan:8080", "count": 4}, headers={"X-API-Key": raw})

    renamed = await async_client.patch(
        f"{LINKS}{link.id}", json={"name": "Orders app", "url": "http://orders.lan:8080/"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["badge_count"] == 4

    moved = await async_client.patch(f"{LINKS}{link.id}", json={"url": "http://other.lan:8080"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["badge_count"] is None
    assert await _badge_rows(db_session) == []
