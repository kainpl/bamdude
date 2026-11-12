"""Filing a print under an order asks for the print too (WS-13 E13 B03, B04, B05).

``add-archives`` / ``remove-archives`` move an archive's order, so beside
``orders:update`` they ask the archive's own update right through the canonical
ownership gate: ``archives:update_all`` files any print, ``archives:update_own`` only
the caller's own, an ownerless print only ``all``; one print out of reach refuses the
whole batch and nothing is written. The archive editor is the mirror image: a PATCH
that actually moves the order or the line asks ``orders:update`` as well, and one
that merely sends the binding it already has asks nothing more.

A print taken from another order by ``add-archives`` leaves that order, so the order
it leaves is asked first, exactly as the two other exits ask it
(``order_fulfilment.ensure_prints_can_leave``): a print whose output went onto that
order's shelf stays; a spare may go.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.user import User
from backend.tests.integration.test_order_issue_review_fixes import LEAVE, _fulfil, _lamp_order
from backend.tests.integration.test_orders_api import _completed_print, catalog  # noqa: F401 — the shared fixture
from backend.tests.integration.test_workshop_library_rights import _jwt, _key, _user

pytestmark = pytest.mark.integration

_PU = Permission.ORDERS_UPDATE.value
_ALL = Permission.ARCHIVES_UPDATE_ALL.value
_OWN = Permission.ARCHIVES_UPDATE_OWN.value
_REFUSED = "You can only update your own archives"


async def _archive(db, *, owner: User | None = None, project_id: int | None = None) -> PrintArchive:
    a = PrintArchive(
        filename="cube",
        file_path="",
        file_size=0,
        status="completed",
        created_by_id=owner.id if owner else None,
        project_id=project_id,
    )
    db.add(a)
    await db.commit()
    return a


async def _order_of(db, archive_id: int) -> tuple[int | None, int | None]:
    a = await db.get(PrintArchive, archive_id, populate_existing=True)
    return a.project_id, a.project_line_id


@pytest.fixture
async def desk(db_session, committing_client, catalog):  # noqa: F811 — the fixture's value
    """Users: ``ar_own`` (orders:update + archives:update_own), ``ar_all`` (+ update_all),
    ``ar_none`` (orders:update only), ``ar_editor`` (archives:update_all only). Order A
    with two lines, order B with one; three unfiled prints — ``ar_own``'s, ``ar_all``'s
    and an ownerless one."""
    own = await _user(db_session, "ar_own", [_PU, _OWN])
    every = await _user(db_session, "ar_all", [_PU, _ALL])
    await _user(db_session, "ar_none", [_PU])
    await _user(db_session, "ar_editor", [_ALL])
    order_a, lines_a = await _lamp_order(committing_client, catalog, [1, 1])
    order_b, lines_b = await _lamp_order(committing_client, catalog, [1])
    return {
        "a": order_a,
        "lines_a": lines_a,
        "b": order_b,
        "lines_b": lines_b,
        "own_user": own,
        "every_user": every,
        "mine": (await _archive(db_session, owner=own)).id,
        "theirs": (await _archive(db_session, owner=every)).id,
        "ownerless": (await _archive(db_session)).id,
    }


async def _add(client: AsyncClient, order_id: int, ids: list[int], who: str | None = None, **extra):
    headers = await _jwt(who) if who else None
    return await client.post(
        f"/api/v1/projects/{order_id}/add-archives", json={"archive_ids": ids, **extra}, headers=headers
    )


async def _remove(client: AsyncClient, order_id: int, ids: list[int], who: str | None = None):
    headers = await _jwt(who) if who else None
    return await client.post(f"/api/v1/projects/{order_id}/remove-archives", json={"archive_ids": ids}, headers=headers)


class TestFilingAsksForThePrint:
    """B03."""

    @pytest.mark.asyncio
    async def test_own_right_files_only_the_callers_own_print(self, committing_client, db_session, desk):
        r = await _add(committing_client, desk["a"], [desk["mine"]], "ar_own")
        assert r.status_code == 200, r.text
        assert (await _order_of(db_session, desk["mine"]))[0] == desk["a"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("other", ["theirs", "ownerless"])
    async def test_one_print_out_of_reach_refuses_the_batch(self, committing_client, db_session, desk, other):
        r = await _add(committing_client, desk["a"], [desk["mine"], desk[other]], "ar_own")
        assert (r.status_code, r.json()["detail"]) == (403, _REFUSED)
        assert await _order_of(db_session, desk["mine"]) == (None, None)
        assert await _order_of(db_session, desk[other]) == (None, None)

    @pytest.mark.asyncio
    async def test_all_right_files_any_print(self, committing_client, db_session, desk):
        r = await _add(committing_client, desk["a"], [desk["theirs"], desk["ownerless"]], "ar_all")
        assert r.status_code == 200, r.text
        assert (await _order_of(db_session, desk["ownerless"]))[0] == desk["a"]

    @pytest.mark.asyncio
    async def test_no_archive_right_files_nothing(self, committing_client, db_session, desk):
        r = await _add(committing_client, desk["a"], [desk["mine"]], "ar_none")
        assert r.status_code == 403, r.text
        assert await _order_of(db_session, desk["mine"]) == (None, None)

    @pytest.mark.asyncio
    async def test_unfiling_asks_for_the_print_too(self, committing_client, db_session, desk):
        ids = [desk["mine"], desk["theirs"]]
        assert (await _add(committing_client, desk["a"], ids)).status_code == 200  # the admin files both
        r = await _remove(committing_client, desk["a"], ids, "ar_own")
        assert (r.status_code, r.json()["detail"]) == (403, _REFUSED)
        assert (await _order_of(db_session, desk["mine"]))[0] == desk["a"]
        r = await _remove(committing_client, desk["a"], [desk["mine"]], "ar_own")
        assert r.status_code == 200, r.text
        assert await _order_of(db_session, desk["mine"]) == (None, None)
        r = await _remove(committing_client, desk["a"], [desk["theirs"]], "ar_none")
        assert r.status_code == 403, r.text
        assert (await _order_of(db_session, desk["theirs"]))[0] == desk["a"]

    @pytest.mark.asyncio
    async def test_an_api_key_needs_its_archive_scope(self, committing_client, db_session, desk):
        without = await _key(db_session, desk["every_user"], can_manage_projects=True, can_manage_archives=False)
        url = f"/api/v1/projects/{desk['a']}/add-archives"
        body = {"archive_ids": [desk["ownerless"]]}
        r = await committing_client.post(url, json=body, headers={"X-API-Key": without})
        assert r.status_code == 403, r.text
        assert await _order_of(db_session, desk["ownerless"]) == (None, None)
        allowed = await _key(db_session, desk["every_user"], can_manage_projects=True, can_manage_archives=True)
        r = await committing_client.post(url, json=body, headers={"Authorization": f"Bearer {allowed}"})
        assert r.status_code == 200, r.text
        assert (await _order_of(db_session, desk["ownerless"]))[0] == desk["a"]


class TestTheWorkshopsOwnRightFilesAnyPrint:
    """Owner's ruling 2026-10-04 (E13 final review #1): a print from the printer's screen or a
    slicer has no owner, so ``archives:update_own`` never reaches it — and the default Operators
    hold only that. ``orders:file_prints`` files and unfiles ANY print under an order without
    ``archives:update_all`` (which would also open other people's photos and files). Since the
    Workshop's own rights (§O, O21) it needs no ``orders:update`` beside it — the filing clerk's
    doors are pinned in ``test_workshop_filing_doors``."""

    @pytest.mark.asyncio
    async def test_the_right_files_and_unfiles_ownerless_and_others_prints(self, committing_client, db_session, desk):
        await _user(db_session, "ar_filer", [_PU, _OWN, Permission.ORDERS_FILE_PRINTS.value])
        ids = [desk["ownerless"], desk["theirs"]]
        r = await _add(committing_client, desk["a"], ids, "ar_filer")
        assert r.status_code == 200, r.text
        assert (await _order_of(db_session, desk["ownerless"]))[0] == desk["a"]
        assert (await _order_of(db_session, desk["theirs"]))[0] == desk["a"]
        r = await _remove(committing_client, desk["a"], ids, "ar_filer")
        assert r.status_code == 200, r.text
        assert await _order_of(db_session, desk["ownerless"]) == (None, None)

    @pytest.mark.asyncio
    async def test_a_key_files_with_the_right_only_inside_its_projects_scope(self, committing_client, db_session, desk):
        filer = await _user(db_session, "ar_filer_key", [_PU, Permission.ORDERS_FILE_PRINTS.value])
        url = f"/api/v1/projects/{desk['a']}/add-archives"
        body = {"archive_ids": [desk["ownerless"]]}
        without = await _key(db_session, filer, can_manage_projects=False, can_manage_archives=True)
        r = await committing_client.post(url, json=body, headers={"X-API-Key": without})
        assert r.status_code == 403, r.text
        allowed = await _key(db_session, filer, can_manage_projects=True, can_manage_archives=False)
        r = await committing_client.post(url, json=body, headers={"X-API-Key": allowed})
        assert r.status_code == 200, r.text


class TestTheEditorAsksForTheOrder:
    """B04: only a binding that actually changes asks ``orders:update``."""

    @pytest.fixture
    async def filed(self, committing_client, desk):
        line = desk["lines_a"][0]
        r = await _add(committing_client, desk["a"], [desk["theirs"]], project_line_id=line)
        assert r.status_code == 200, r.text
        return desk["theirs"], line

    @pytest.mark.asyncio
    async def test_resending_the_binding_it_has_asks_nothing_more(self, committing_client, db_session, desk, filed):
        archive_id, line = filed
        body = {"notes": "checked", "project_id": desk["a"], "project_line_id": line}
        r = await committing_client.patch(f"/api/v1/archives/{archive_id}", json=body, headers=await _jwt("ar_editor"))
        assert r.status_code == 200, r.text
        assert r.json()["notes"] == "checked"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["order", "line", "unfile"])
    async def test_a_changed_binding_asks_projects_update(self, committing_client, db_session, desk, filed, change):
        archive_id, line = filed
        body = {
            "order": {"project_id": desk["b"]},
            "line": {"project_line_id": desk["lines_a"][1]},
            "unfile": {"project_id": None},
        }[change]
        r = await committing_client.patch(f"/api/v1/archives/{archive_id}", json=body, headers=await _jwt("ar_editor"))
        assert r.status_code == 403, r.text
        assert await _order_of(db_session, archive_id) == (desk["a"], line)
        r = await committing_client.patch(f"/api/v1/archives/{archive_id}", json=body, headers=await _jwt("ar_all"))
        assert r.status_code == 200, r.text
        assert await _order_of(db_session, archive_id) != (desk["a"], line)

    @pytest.mark.asyncio
    async def test_an_unfiled_print_sent_unfiled_asks_nothing_more(self, committing_client, db_session, desk):
        body = {"notes": "loose", "project_id": None, "project_line_id": None}
        r = await committing_client.patch(
            f"/api/v1/archives/{desk['ownerless']}", json=body, headers=await _jwt("ar_editor")
        )
        assert r.status_code == 200, r.text


class TestAPrintLeavesItsOldOrderByTheRules:
    """B05: ``add-archives`` asks the order a print leaves, before anything moves."""

    @pytest.mark.asyncio
    async def test_a_received_print_cannot_be_taken_by_another_order(
        self,
        committing_client,
        db_session,
        catalog,  # noqa: F811
    ):
        order_a, [line_a] = await _lamp_order(committing_client, catalog, [1])
        order_b, _ = await _lamp_order(committing_client, catalog, [1])
        received = await _completed_print(db_session, order_a, catalog["file"].id)
        loose = await _archive(db_session)
        assert (await _fulfil(committing_client, order_a, [{"line_id": line_a, "receive": 1}])).status_code == 200
        r = await _add(committing_client, order_b, [loose.id, received.id])
        assert (r.status_code, r.json()["detail"]) == (409, LEAVE)
        assert (await _order_of(db_session, received.id))[0] == order_a
        assert await _order_of(db_session, loose.id) == (None, None)

    @pytest.mark.asyncio
    async def test_a_spare_print_may_move_to_another_order(
        self,
        committing_client,
        db_session,
        catalog,  # noqa: F811
    ):
        order_a, [line_a] = await _lamp_order(committing_client, catalog, [1])
        order_b, _ = await _lamp_order(committing_client, catalog, [1])
        await _completed_print(db_session, order_a, catalog["file"].id)
        spare = await _completed_print(db_session, order_a, catalog["file"].id)
        assert (await _fulfil(committing_client, order_a, [{"line_id": line_a, "receive": 1}])).status_code == 200
        r = await _add(committing_client, order_b, [spare.id])
        assert r.status_code == 200, r.text
        assert (await _order_of(db_session, spare.id))[0] == order_b
