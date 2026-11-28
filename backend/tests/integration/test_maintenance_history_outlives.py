"""The maintenance history outlives its printer, task and performer (N09, upstream 8afcb3fbd).

A reset keeps the names as they are at that moment; deleting the printer, unassigning the
task or deleting a custom type detaches the history instead of deleting it. A reset takes
a trimmed, optional comment; an API key's reset is its owner's. The history can be cleaned
up by age or one entry at a time, under the printer scope, and exported as CSV.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app.models.maintenance import MaintenanceHistory, MaintenanceType, PrinterMaintenance
from backend.app.models.printer import Printer
from backend.tests.integration.test_printer_scope_n62 import farm  # noqa: F401 -- N62's person fixture

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _item(db, printer_id: int, *, custom: bool = False, name: str = "Clean Nozzle") -> tuple[int, int]:
    kind = MaintenanceType(name=name, is_system=not custom, default_interval_hours=100.0, printer_models='["*"]')
    db.add(kind)
    await db.commit()
    await db.refresh(kind)
    item = PrinterMaintenance(printer_id=printer_id, maintenance_type_id=kind.id)
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return kind.id, item.id


async def _reset(client, item_id: int, notes=None, headers=None):
    response = await client.post(
        f"/api/v1/maintenance/items/{item_id}/perform", json={"notes": notes}, headers=headers or {}
    )
    assert response.status_code == 200, response.text


async def _rows(db) -> list[MaintenanceHistory]:
    db.expire_all()
    return list((await db.execute(select(MaintenanceHistory).order_by(MaintenanceHistory.id))).scalars().all())


async def _entry(db, printer_id: int | None, *, days_ago: int = 0, name: str = "P", notes: str | None = None) -> int:
    row = MaintenanceHistory(
        printer_id=printer_id,
        printer_name=name,
        maintenance_type_name="Clean Nozzle",
        performed_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago),
        hours_at_maintenance=1.0,
        notes=notes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


class TestTheHistoryOutlives:
    async def test_a_reset_keeps_the_names_as_they_are(self, async_client, db_session, printer_factory):
        printer = await printer_factory(name="Farm-1")
        printer_id = printer.id
        _kind, item_id = await _item(db_session, printer_id)

        await _reset(async_client, item_id)

        [row] = await _rows(db_session)
        assert (row.printer_id, row.printer_maintenance_id) == (printer_id, item_id)
        assert (row.printer_name, row.maintenance_type_name) == ("Farm-1", "Clean Nozzle")
        assert row.performed_by_name  # the signed-in user

    async def test_the_history_outlives_its_printer(self, async_client, db_session, printer_factory):
        printer = await printer_factory(name="Gone-1")
        printer_id = printer.id
        _kind, item_id = await _item(db_session, printer_id)
        await _reset(async_client, item_id, notes="greased")

        response = await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives=false")
        assert response.status_code == 200, response.text

        [row] = await _rows(db_session)
        assert (row.printer_id, row.printer_maintenance_id) == (None, None)
        assert (row.printer_name, row.notes) == ("Gone-1", "greased")

    async def test_unassigning_a_task_keeps_its_history(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        printer_id = printer.id
        _kind, item_id = await _item(db_session, printer_id, custom=True, name="Grease the rods")
        await _reset(async_client, item_id)

        response = await async_client.delete(f"/api/v1/maintenance/items/{item_id}")
        assert response.status_code == 200, response.text

        [row] = await _rows(db_session)
        assert (row.printer_maintenance_id, row.printer_id) == (None, printer_id)
        assert row.maintenance_type_name == "Grease the rods"

    async def test_deleting_a_custom_type_keeps_its_history(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        kind_id, item_id = await _item(db_session, printer.id, custom=True, name="Check the fans")
        await _reset(async_client, item_id)

        response = await async_client.delete(f"/api/v1/maintenance/types/{kind_id}")
        assert response.status_code == 200, response.text

        [row] = await _rows(db_session)
        assert row.printer_maintenance_id is None
        assert row.maintenance_type_name == "Check the fans"

    async def test_a_renamed_printer_keeps_its_old_name_in_the_history(self, async_client, db_session, printer_factory):
        printer = await printer_factory(name="Old name")
        printer_id = printer.id
        _kind, item_id = await _item(db_session, printer_id)
        await _reset(async_client, item_id)
        (await db_session.get(Printer, printer_id)).name = "New name"
        await db_session.commit()

        response = await async_client.get("/api/v1/maintenance/history")

        assert [entry["printer_name"] for entry in response.json()["items"]] == ["Old name"]


class TestTheReset:
    async def test_an_api_key_reset_is_its_owners(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        _kind, item_id = await _item(db_session, printer.id)
        created = await async_client.post(
            "/api/v1/api-keys/", json={"name": "maint", "can_manage_maintenance": True, "can_read_status": True}
        )
        assert created.status_code == 200, created.text
        await _reset(async_client, item_id)  # signed in: the owner's name
        await _reset(async_client, item_id, headers={"X-API-Key": created.json()["key"]})

        signed_in, by_key = await _rows(db_session)
        assert by_key.performed_by_name == signed_in.performed_by_name is not None
        assert by_key.performed_by_user_id == signed_in.performed_by_user_id is not None

    @pytest.mark.parametrize(("sent", "kept"), [("   ", None), ("", None), ("  greased  ", "greased"), (None, None)])
    async def test_the_comment_is_trimmed_and_blank_is_none(
        self, async_client, db_session, printer_factory, sent, kept
    ):
        printer = await printer_factory()
        _kind, item_id = await _item(db_session, printer.id)

        await _reset(async_client, item_id, notes=sent)

        [row] = await _rows(db_session)
        assert row.notes == kept

    async def test_the_comment_has_a_limit_spaces_around_it_do_not_count(
        self, async_client, db_session, printer_factory
    ):
        printer = await printer_factory()
        _kind, item_id = await _item(db_session, printer.id)

        await _reset(async_client, item_id, notes="  " + "x" * 10000 + "  ")
        too_long = await async_client.post(f"/api/v1/maintenance/items/{item_id}/perform", json={"notes": "x" * 10001})

        assert too_long.status_code == 422


class TestCleanup:
    async def test_entries_older_than_n_days_go_and_the_task_stays_reset(
        self, async_client, db_session, printer_factory
    ):
        printer = await printer_factory()
        printer_id = printer.id
        _kind, item_id = await _item(db_session, printer_id)
        await _reset(async_client, item_id)  # today
        old = await _entry(db_session, printer_id, days_ago=40)

        response = await async_client.delete("/api/v1/maintenance/history?older_than_days=30")

        assert response.json() == {"deleted": 1}
        assert old not in [row.id for row in await _rows(db_session)]
        db_session.expire_all()
        assert (await db_session.get(PrinterMaintenance, item_id)).last_performed_at is not None

    async def test_a_cleanup_for_one_printer_leaves_the_others(self, async_client, db_session, printer_factory):
        a, b = (await printer_factory()).id, (await printer_factory()).id
        await _entry(db_session, a, days_ago=40)
        kept = await _entry(db_session, b, days_ago=40)

        response = await async_client.delete(f"/api/v1/maintenance/history?older_than_days=30&printer_id={a}")

        assert response.json() == {"deleted": 1}
        assert [row.id for row in await _rows(db_session)] == [kept]

    @pytest.mark.parametrize("days", [0, -1, 36501])
    async def test_the_age_has_bounds(self, async_client, days):
        response = await async_client.delete(f"/api/v1/maintenance/history?older_than_days={days}")
        assert response.status_code == 422

    async def test_one_entry_can_go(self, async_client, db_session, printer_factory):
        printer = await printer_factory()
        entry = await _entry(db_session, printer.id)

        gone = await async_client.delete(f"/api/v1/maintenance/history/{entry}")
        again = await async_client.delete(f"/api/v1/maintenance/history/{entry}")

        assert gone.json() == {"deleted": 1}
        assert again.status_code == 404


class TestThePrinterScope:
    @pytest.fixture
    async def keyed(self, async_client, db_session, printer_factory):
        a, b = (await printer_factory(name="A")).id, (await printer_factory(name="B")).id
        entries = {
            "a": await _entry(db_session, a, name="A"),
            "b": await _entry(db_session, b, name="B"),
            "gone": await _entry(db_session, None, name="Deleted"),
        }
        created = await async_client.post(
            "/api/v1/api-keys/",
            json={"name": "only-a", "can_manage_maintenance": True, "can_read_status": True, "printer_ids": [a]},
        )
        assert created.status_code == 200, created.text
        return a, b, entries, {"X-API-Key": created.json()["key"]}

    async def test_a_scoped_key_reads_only_its_printers_entries(self, async_client, keyed):
        _a, _b, entries, headers = keyed

        response = await async_client.get("/api/v1/maintenance/history", headers=headers)

        assert [entry["id"] for entry in response.json()["items"]] == [entries["a"]]

    async def test_a_scoped_key_deletes_neither_another_printers_entry_nor_a_deleted_ones(
        self, async_client, db_session, keyed
    ):
        _a, _b, entries, headers = keyed

        other = await async_client.delete(f"/api/v1/maintenance/history/{entries['b']}", headers=headers)
        orphan = await async_client.delete(f"/api/v1/maintenance/history/{entries['gone']}", headers=headers)
        own = await async_client.delete(f"/api/v1/maintenance/history/{entries['a']}", headers=headers)

        assert other.status_code == 403  # the gate asks the entry's printer
        assert orphan.status_code == 404  # no printer: only an unrestricted caller
        assert own.json() == {"deleted": 1}
        assert sorted(row.id for row in await _rows(db_session)) == sorted([entries["b"], entries["gone"]])

    async def test_a_scoped_keys_cleanup_stays_with_its_printers(self, async_client, db_session, keyed):
        _a, b, entries, headers = keyed

        cleaned = await async_client.delete("/api/v1/maintenance/history?older_than_days=1", headers=headers)
        assert cleaned.status_code == 200
        named = await async_client.delete(
            f"/api/v1/maintenance/history?older_than_days=1&printer_id={b}", headers=headers
        )

        assert named.status_code == 403
        assert len(await _rows(db_session)) == 3  # nothing older than a day

    async def test_an_unrestricted_caller_sees_a_deleted_printers_entries(self, async_client, keyed):
        _a, _b, entries, _headers = keyed

        response = await async_client.get("/api/v1/maintenance/history")

        assert entries["gone"] in [entry["id"] for entry in response.json()["items"]]


class TestAPersonsPrinterScope:
    """N62 for a person (acceptance A1-01): another printer's entry reads as no entry at all."""

    async def test_another_printers_entry_cannot_be_told_from_none(self, async_client, db_session, farm):  # noqa: F811
        entry = await _entry(db_session, farm["b"].id, name="B")

        other = await async_client.delete(f"/api/v1/maintenance/history/{entry}", headers=farm["alice"])
        missing = await async_client.delete("/api/v1/maintenance/history/999999", headers=farm["alice"])

        assert other.status_code == missing.status_code == 404
        assert other.json()["detail"] == missing.json()["detail"]
        assert entry in [row.id for row in await _rows(db_session)]

    async def test_a_person_reads_only_their_printers_entries(self, async_client, db_session, farm):  # noqa: F811
        own = await _entry(db_session, farm["a"].id, name="A")
        await _entry(db_session, farm["b"].id, name="B")
        await _entry(db_session, None, name="Deleted")

        response = await async_client.get("/api/v1/maintenance/history", headers=farm["alice"])

        assert [entry["id"] for entry in response.json()["items"]] == [own]


class TestAPerformerGone:
    async def test_a_deleted_users_name_stays_in_the_history(self, async_client, db_session, farm):  # noqa: F811
        # The reset as the writer makes it (a person's id and name), then the person goes.
        row = MaintenanceHistory(
            printer_id=farm["a"].id,
            printer_name="A",
            maintenance_type_name="Clean Nozzle",
            hours_at_maintenance=1.0,
            performed_by_user_id=farm["alice_id"],
            performed_by_name="alice",
        )
        db_session.add(row)
        await db_session.commit()

        gone = await async_client.delete(f"/api/v1/users/{farm['alice_id']}")
        assert gone.status_code == 204, gone.text

        [entry] = (await async_client.get("/api/v1/maintenance/history")).json()["items"]
        assert entry["performed_by_name"] == "alice"

    async def test_a_telegram_reset_keeps_the_names_the_bound_users_first(
        self, async_client, db_session, printer_factory, monkeypatch
    ):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from backend.app.models.notification import NotificationProvider
        from backend.app.models.telegram_chat import TelegramChat
        from backend.app.models.user import User
        from backend.app.services.telegram_handlers import maintenance_handlers

        printer = await printer_factory(name="Farm-9")
        printer_id = printer.id
        _kind, item_id = await _item(db_session, printer_id)
        provider = NotificationProvider(
            name="bot", provider_type="telegram", enabled=True, config='{"bot_token": "1:x"}'
        )
        db_session.add(provider)
        await db_session.flush()
        user = User(username="tg-operator")
        db_session.add(user)
        await db_session.flush()
        bound = TelegramChat(chat_id=4242, provider_id=provider.id, is_active=True, label="Farm chat", user_id=user.id)
        alone = TelegramChat(chat_id=4343, provider_id=provider.id, is_active=True, label="Night shift")
        db_session.add_all([bound, alone])
        await db_session.commit()
        monkeypatch.setattr(maintenance_handlers, "has_perm", lambda chat, perm: True)
        monkeypatch.setattr(maintenance_handlers, "deny_out_of_scope", AsyncMock(return_value=False))

        for chat_id in (4242, 4343):
            callback = SimpleNamespace(
                data=f"maint:done:{item_id}:{printer_id}:n",
                message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), reply_markup=None, delete=AsyncMock()),
                answer=AsyncMock(),
            )
            await maintenance_handlers.cb_maintenance_done(callback, None)

        rows = await _rows(db_session)
        assert [(r.printer_id, r.printer_name, r.maintenance_type_name) for r in rows] == [
            (printer_id, "Farm-9", "Clean Nozzle")
        ] * 2
        assert [r.performed_by_name for r in rows] == ["tg-operator", "Night shift"]


class TestCsv:
    async def test_the_csv_has_a_bom_the_columns_and_formulas_as_text(self, async_client, db_session, printer_factory):
        printer = await printer_factory(name="Принтер-1")
        await _entry(db_session, printer.id, name="Принтер-1", notes="=1+1")

        response = await async_client.get("/api/v1/maintenance/history/export?format=csv")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        body = response.content.decode("utf-8")
        assert body.startswith("﻿")
        lines = body.lstrip("﻿").splitlines()
        assert lines[0] == "Date,Printer,Type,Hours,Performed by,Notes"
        assert "Принтер-1" in lines[1] and "'=1+1" in lines[1]

    async def test_the_csv_follows_the_printer_scope(self, async_client, db_session, printer_factory):
        a, b = (await printer_factory(name="A")).id, (await printer_factory(name="B")).id
        await _entry(db_session, a, name="A")
        await _entry(db_session, b, name="B")
        created = await async_client.post(
            "/api/v1/api-keys/", json={"name": "csv-a", "can_read_status": True, "printer_ids": [a]}
        )

        response = await async_client.get(
            "/api/v1/maintenance/history/export?format=csv", headers={"X-API-Key": created.json()["key"]}
        )

        rows = response.content.decode("utf-8").lstrip("﻿").splitlines()[1:]
        assert [row.split(",")[1] for row in rows] == ["A"]

    async def test_an_unknown_format_is_refused(self, async_client):
        response = await async_client.get("/api/v1/maintenance/history/export?format=pdf")
        assert response.status_code == 422
