"""N62: a group of people limited to printers and locations (spec r2, upstream Bambuddy #1727).

The farm below: a hall with a rack inside it, a lab beside them. Printer A stands on the rack,
B in the lab, C nowhere. "Team A" is limited to the HALL — so it reaches A through the rack
below it (D3) — and carries ``archives:update_all`` on top of what alice's Operators group
gives her, and ``api_keys:create`` for her own key (a permission group and a team group combine
without widening the team).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

PASSWORD = "UserPass1!"


async def _group_id(client: AsyncClient, name: str) -> int:
    response = await client.get("/api/v1/groups/")
    assert response.status_code == 200, response.text
    return next(g["id"] for g in response.json() if g["name"] == name)


async def _location(client: AsyncClient, name: str, parent_id: int | None = None) -> int:
    response = await client.post("/api/v1/printer-locations", json={"name": name, "parent_id": parent_id})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
    token = response.json().get("access_token")
    assert token, response.text
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def farm(async_client, printer_factory, archive_factory, db_session):
    hall = await _location(async_client, "Hall")
    rack = await _location(async_client, "Rack", hall)
    lab = await _location(async_client, "Lab")
    a = await printer_factory(name="A", location_id=rack)
    b = await printer_factory(name="B", location_id=lab)
    c = await printer_factory(name="C")
    team = await async_client.post(
        "/api/v1/groups/",
        json={
            "name": "Team A",
            "permissions": ["archives:update_all", "api_keys:create", "queue:delete_all"],
            "restrict_printers": True,
            "location_ids": [hall],
        },
    )
    assert team.status_code == 201, team.text
    operators = await _group_id(async_client, "Operators")
    user = await async_client.post(
        "/api/v1/users/",
        json={"username": "alice", "password": PASSWORD, "role": "user", "group_ids": [operators, team.json()["id"]]},
    )
    assert user.status_code in (200, 201), user.text
    archives = {
        "a": await archive_factory(a.id, print_name="On A", tags="shared"),
        "b": await archive_factory(b.id, print_name="On B", tags="shared"),
        "none": await archive_factory(None, print_name="Orphan"),
    }
    return {
        "a": a,
        "b": b,
        "c": c,
        "hall": hall,
        "rack": rack,
        "lab": lab,
        "team": team.json()["id"],
        "alice_id": user.json()["id"],
        "alice": await _login(async_client, "alice"),
        "archives": archives,
    }


class TestTheRule:
    async def test_the_group_reaches_the_printers_below_its_location_and_later_ones(
        self, farm, db_session, printer_factory
    ):
        from backend.app.core.printer_scope import group_reach

        assert await group_reach(db_session, [farm["team"]]) == {farm["a"].id}
        d = await printer_factory(name="D", location_id=farm["hall"])
        assert await group_reach(db_session, [farm["team"]]) == {farm["a"].id, d.id}

    async def test_a_parent_loop_does_not_hang(self, farm, db_session):
        from backend.app.core.printer_scope import group_reach
        from backend.app.models.printer_location import PrinterLocation

        hall = await db_session.get(PrinterLocation, farm["hall"])
        hall.parent_id = farm["rack"]
        await db_session.commit()
        assert await group_reach(db_session, [farm["team"]]) == {farm["a"].id}

    async def test_a_person_without_a_limited_group_and_an_admin_reach_everything(self, farm, db_session):
        from backend.app.core.printer_scope import user_scope
        from backend.app.models.user import User

        admin = await db_session.scalar(select(User).where(User.username == "test_admin"))
        alice = await db_session.get(User, farm["alice_id"])
        assert await user_scope(db_session, admin) is None
        assert await user_scope(db_session, alice) == {farm["a"].id}
        alice.is_active = False
        assert await user_scope(db_session, alice) == frozenset()

    async def test_a_key_is_its_list_and_its_owners_scope(self, farm, db_session):
        from types import SimpleNamespace

        from backend.app.core.printer_scope import key_scope
        from backend.app.models.user import User

        alice = await db_session.get(User, farm["alice_id"])
        key = SimpleNamespace(printer_ids=[farm["a"].id, farm["b"].id])
        assert await key_scope(db_session, key, alice) == {farm["a"].id}
        assert await key_scope(db_session, SimpleNamespace(printer_ids=None), alice) == {farm["a"].id}

    async def test_an_author_missing_or_none(self, db_session):
        from backend.app.core.printer_scope import author_scope

        assert await author_scope(db_session, None) is None
        assert await author_scope(db_session, 999_999) == frozenset()


class TestAPerson:
    async def test_me_says_limited(self, async_client, farm):
        me = await async_client.get("/api/v1/auth/me", headers=farm["alice"])
        assert me.json()["printers_restricted"] is True
        assert (await async_client.get("/api/v1/auth/me")).json()["printers_restricted"] is False

    async def test_the_printer_list_is_the_teams(self, async_client, farm):
        response = await async_client.get("/api/v1/printers/", headers=farm["alice"])
        assert response.status_code == 200, response.text
        assert {p["id"] for p in response.json()} == {farm["a"].id}

    async def test_another_printer_is_not_there(self, async_client, farm):
        own = await async_client.get(f"/api/v1/printers/{farm['a'].id}", headers=farm["alice"])
        other = await async_client.get(f"/api/v1/printers/{farm['b'].id}", headers=farm["alice"])
        assert own.status_code == 200, own.text
        assert other.status_code == 404
        assert other.json()["detail"] == "Printer not found"

    async def test_another_printers_archive_is_not_there(self, async_client, farm):
        response = await async_client.get(f"/api/v1/archives/{farm['archives']['b'].id}", headers=farm["alice"])
        assert response.status_code == 404
        assert response.json()["detail"] == "Archive not found"

    async def test_the_compared_archives_are_asked_of_their_printers(self, async_client, farm):
        ids = f"{farm['archives']['a'].id},{farm['archives']['b'].id}"
        response = await async_client.get(f"/api/v1/archives/compare?archive_ids={ids}", headers=farm["alice"])
        assert response.status_code == 404

    async def test_the_archive_list_keeps_the_team_and_the_orphans(self, async_client, farm):
        response = await async_client.get("/api/v1/archives/?all=true", headers=farm["alice"])
        assert response.status_code == 200, response.text
        names = {a["print_name"] for a in response.json()["data"]}
        assert names == {"On A", "Orphan"}

    async def test_search_and_stats_count_only_what_is_seen(self, async_client, farm):
        found = await async_client.get("/api/v1/archives/search?q=On", headers=farm["alice"])
        assert {a["print_name"] for a in found.json()} == {"On A"}
        stats = await async_client.get("/api/v1/statistics/overview", headers=farm["alice"])
        assert stats.status_code == 200, stats.text
        assert stats.json()["total_prints"] == 2  # A's and the orphan
        everyone = await async_client.get("/api/v1/statistics/overview")
        assert everyone.json()["total_prints"] == 3

    async def test_a_tag_is_renamed_on_the_teams_archives_only(self, async_client, farm, db_session):
        from backend.app.models.archive import PrintArchive

        b_id = farm["archives"]["b"].id
        response = await async_client.put(
            "/api/v1/archives/tags/shared", json={"new_name": "ours"}, headers=farm["alice"]
        )
        assert response.status_code == 200, response.text
        assert response.json()["affected"] == 1
        db_session.expire_all()
        b = await db_session.get(PrintArchive, b_id)
        assert b.tags == "shared"

    async def test_a_plug_of_another_printer_is_not_listed(self, async_client, farm, smart_plug_factory):
        await smart_plug_factory(name="on-b", printer_id=farm["b"].id)
        await smart_plug_factory(name="free", ip_address="192.168.1.250")
        response = await async_client.get("/api/v1/smart-plugs/", headers=farm["alice"])
        assert response.status_code == 200, response.text
        assert {p["name"] for p in response.json()} == {"free"}


class TestAKeyOfALimitedOwner:
    async def test_the_key_stays_with_its_owners_printers(self, async_client, farm):
        created = await async_client.post(
            "/api/v1/api-keys/", json={"name": "alice's", "can_read_status": True}, headers=farm["alice"]
        )
        assert created.status_code == 200, created.text
        key = {"X-API-Key": created.json()["key"]}
        own = await async_client.get(f"/api/v1/printers/{farm['a'].id}/status", headers=key)
        other = await async_client.get(f"/api/v1/printers/{farm['b'].id}/status", headers=key)
        assert own.status_code == 200, own.text
        assert other.status_code == 403  # a key is refused as before (spec §12.1)


class TestTokenDoors:
    async def _token(self, client, headers, scope):
        response = await client.post(
            "/api/v1/auth/tokens", json={"name": scope, "expires_in_days": 30, "scope": scope}, headers=headers
        )
        assert response.status_code == 201, response.text
        return response.json()["token"]

    async def test_the_cam_wall_shows_its_owners_printers(self, async_client, farm):
        token = await self._token(async_client, farm["alice"], "camwall")
        response = await async_client.get(f"/api/v1/camwall/printers?token={token}", headers={"Authorization": ""})
        assert response.status_code == 200, response.text
        assert {p["id"] for p in response.json()} == {farm["a"].id}

    async def test_the_overlay_names_no_other_printers_file(self, async_client, farm):
        token = await self._token(async_client, farm["alice"], "overlay")
        own = await async_client.get(
            f"/api/v1/printers/{farm['a'].id}/overlay-status?token={token}", headers={"Authorization": ""}
        )
        other = await async_client.get(
            f"/api/v1/printers/{farm['b'].id}/overlay-status?token={token}", headers={"Authorization": ""}
        )
        assert own.status_code == 200, own.text
        assert other.status_code == 404


class TestLocations:
    async def test_a_limited_person_does_not_move_a_printer_out_of_the_teams_location(self, async_client, farm):
        response = await async_client.patch(
            f"/api/v1/printers/{farm['a'].id}", json={"location_id": farm["lab"]}, headers=farm["alice"]
        )
        assert response.status_code == 403, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "printer_access_change"
        assert detail["groups"] == ["Team A"]

    async def test_an_administrator_does(self, async_client, farm, db_session):
        from backend.app.core.printer_scope import group_reach

        response = await async_client.patch(f"/api/v1/printers/{farm['b'].id}", json={"location_id": farm["rack"]})
        assert response.status_code == 200, response.text
        assert await group_reach(db_session, [farm["team"]]) == {farm["a"].id, farm["b"].id}

    async def test_a_limited_person_does_not_add_a_printer(self, async_client, farm):
        response = await async_client.post(
            "/api/v1/printers/",
            json={
                "name": "New",
                "serial_number": "00M09A999999999",
                "ip_address": "10.0.0.9",
                "access_code": "12345678",
            },
            headers=farm["alice"],
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "printer_create_restricted"

    async def test_moving_a_granted_branch_is_for_an_administrator(self, async_client, farm):
        response = await async_client.patch(
            f"/api/v1/printer-locations/{farm['rack']}", json={"parent_id": farm["lab"]}, headers=farm["alice"]
        )
        assert response.status_code == 403, response.text


class TestGroups:
    async def test_administrators_cannot_be_limited(self, async_client):
        admins = await _group_id(async_client, "Administrators")
        response = await async_client.patch(f"/api/v1/groups/{admins}", json={"restrict_printers": True})
        assert response.status_code == 400, response.text

    async def test_who_has_access_names_the_team_and_its_location(self, async_client, farm):
        response = await async_client.get(f"/api/v1/printers/{farm['a'].id}/access")
        assert response.status_code == 200, response.text
        assert response.json()["groups"] == [
            {"id": farm["team"], "name": "Team A", "picked": False, "location_ids": [farm["hall"]]}
        ]
        nobody = await async_client.get(f"/api/v1/printers/{farm['b'].id}/access")
        assert nobody.json()["groups"] == []

    async def test_a_deleted_group_leaves_no_grant(self, async_client, farm, db_session):
        from backend.app.models.group import group_locations

        response = await async_client.delete(f"/api/v1/groups/{farm['team']}")
        assert response.status_code in (200, 204), response.text
        rows = (await db_session.execute(select(group_locations))).all()
        assert rows == []


class TestBackgroundWork:
    async def test_the_inbox_does_not_tell_the_team_of_another_printer(self, farm, db_session):
        from backend.app.services.notification_inbox import recipients

        about_b = await recipients(db_session, "print_failed", printer_id=farm["b"].id)
        about_a = await recipients(db_session, "print_failed", printer_id=farm["a"].id)
        assert farm["alice_id"] not in about_b
        assert farm["alice_id"] in about_a

    async def test_a_deleted_limited_authors_jobs_wait_for_an_administrator(self, async_client, farm, db_session):
        from backend.app.models.auto_queue import AutoQueueItem

        job = AutoQueueItem(
            archive_id=farm["archives"]["a"].id, target_model="X1C", status="pending", created_by_id=farm["alice_id"]
        )
        db_session.add(job)
        await db_session.commit()
        job_id = job.id
        response = await async_client.delete(f"/api/v1/users/{farm['alice_id']}")
        assert response.status_code == 204, response.text
        db_session.expire_all()
        held = await db_session.get(AutoQueueItem, job_id)
        assert held.manual_start is True
        assert held.created_by_id is None


class TestAcceptanceR1:
    """The four doors acceptance r1 found open (A01-A04)."""

    async def test_a01_an_archive_named_in_the_body_is_its_printers(self, async_client, farm, db_session):
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.printer_queues import ensure_printer_queue

        await ensure_printer_queue(db_session, farm["a"].id)
        foreign = farm["archives"]["b"].id
        queued = await async_client.post(
            "/api/v1/queue/", json={"printer_id": farm["a"].id, "archive_id": foreign}, headers=farm["alice"]
        )
        assert queued.status_code == 404, queued.text
        assert queued.json()["detail"] == "Archive not found"
        rows = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.archive_id == foreign))).all()
        assert rows == []
        auto = await async_client.post(
            "/api/v1/auto-queue/", json={"archive_id": foreign, "target_model": "X1C"}, headers=farm["alice"]
        )
        assert auto.status_code == 404, auto.text
        preview = await async_client.post(
            "/api/v1/auto-queue/routing-preview", json={"archive_id": foreign}, headers=farm["alice"]
        )
        assert preview.status_code == 404, preview.text

    async def test_a01_a_key_with_a_list_is_refused_another_printers_archive(self, async_client, farm):
        created = await async_client.post(
            "/api/v1/api-keys/", json={"name": "q", "can_queue": True, "printer_ids": [farm["a"].id]}
        )
        key = {"X-API-Key": created.json()["key"]}
        response = await async_client.post(
            "/api/v1/queue/", json={"printer_id": farm["a"].id, "archive_id": farm["archives"]["b"].id}, headers=key
        )
        assert response.status_code == 403, response.text

    async def test_a02_system_information_counts_the_teams_printers(self, async_client, farm):
        mine = (await async_client.get("/api/v1/system/info", headers=farm["alice"])).json()
        everyone = (await async_client.get("/api/v1/system/info")).json()
        assert mine["printers"]["total"] == 1 and everyone["printers"]["total"] == 3
        assert mine["database"]["archives"] == 2 and everyone["database"]["archives"] == 3

    async def test_a03_an_auto_queue_job_on_another_teams_printer_is_not_listed(self, async_client, farm, db_session):
        from backend.app.models.auto_queue import AutoQueueItem
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.printer_queues import ensure_printer_queue

        await ensure_printer_queue(db_session, farm["b"].id)
        copy = PrintQueueItem(queue_id=farm["b"].id, archive_id=farm["archives"]["b"].id, status="printing", position=1)
        db_session.add(copy)
        await db_session.commit()
        handed = AutoQueueItem(
            archive_id=farm["archives"]["b"].id, target_model="X1C", status="assigned", assigned_to_item_id=copy.id
        )
        waiting = AutoQueueItem(archive_id=farm["archives"]["a"].id, target_model="X1C", status="pending")
        db_session.add_all([handed, waiting])
        await db_session.commit()
        handed_id, waiting_id = handed.id, waiting.id

        listed = await async_client.get("/api/v1/auto-queue/", headers=farm["alice"])
        assert {row["id"] for row in listed.json()} == {waiting_id}
        assert (await async_client.get(f"/api/v1/auto-queue/{handed_id}", headers=farm["alice"])).status_code == 404
        cancelled = await async_client.delete(f"/api/v1/auto-queue/{handed_id}", headers=farm["alice"])
        assert cancelled.status_code == 404, cancelled.text
        assert (await async_client.get(f"/api/v1/auto-queue/{waiting_id}", headers=farm["alice"])).status_code == 200
        admin = await async_client.get("/api/v1/auto-queue/")
        assert {row["id"] for row in admin.json()} >= {handed_id, waiting_id}

    async def test_a03_the_routing_preview_counts_the_callers_printers(self, async_client, farm, monkeypatch):
        from backend.app.api.routes import auto_queue

        seen = {}

        async def preview(db, data, user, *, printer_scope=None):
            seen["scope"] = printer_scope
            return {"groups": []}

        monkeypatch.setattr(auto_queue, "routing_preview", preview)
        own = farm["archives"]["a"].id
        response = await async_client.post(
            "/api/v1/auto-queue/routing-preview", json={"archive_id": own}, headers=farm["alice"]
        )
        assert response.status_code == 200, response.text
        assert seen["scope"] == {farm["a"].id}

    async def test_a04_a_limited_chats_digest_lists_its_printers_only(
        self, async_client, farm, db_session, monkeypatch
    ):
        import json

        from backend.app.models.notification import NotificationDigestQueue, NotificationProvider
        from backend.app.models.telegram_chat import TelegramChat
        from backend.app.services.notification_service import NotificationService

        provider = NotificationProvider(
            name="TG",
            provider_type="telegram",
            enabled=True,
            config=json.dumps({"bot_token": "1:test"}),
            subscribed_events=["on_print_failed"],
            daily_digest_enabled=True,
            daily_digest_time="20:00",
        )
        db_session.add(provider)
        await db_session.commit()
        db_session.add_all(
            [
                TelegramChat(
                    chat_id=101, provider_id=provider.id, is_active=True, daily_digest=True, group_id=farm["team"]
                ),
                TelegramChat(chat_id=202, provider_id=provider.id, is_active=True, daily_digest=True),
            ]
        )
        for printer, title in ((farm["a"], "A failed"), (farm["b"], "B failed"), (None, "Farm news")):
            db_session.add(
                NotificationDigestQueue(
                    provider_id=provider.id,
                    event_type="print_failed",
                    title=title,
                    message=title,
                    printer_id=printer.id if printer else None,
                    printer_name=printer.name if printer else None,
                )
            )
        await db_session.commit()
        sent: dict[str, str] = {}

        async def fake_send(self, config, message, chat_id="", **kwargs):
            sent[chat_id] = message
            return True, "ok"

        monkeypatch.setattr(NotificationService, "_send_telegram", fake_send)
        await NotificationService().send_digest(provider.id)

        limited, open_ = sent["101"], sent["202"]
        assert "A failed" in limited and "Farm news" in limited and "B failed" not in limited
        assert all(text in open_ for text in ("A failed", "B failed", "Farm news"))
