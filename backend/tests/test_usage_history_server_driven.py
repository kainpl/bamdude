"""Server-driven filament-usage history — the query layer + ``GET /inventory/usage``
(2026-09-01, the Inventory page's History view).

Same two layers as ``test_inventory_list_server_driven.py``, for the same
reasons: the service functions are exercised directly for the filter/sort
matrix, and the route for the envelope, the param parsing and the legacy pin —
that endpoint is public API and its unpaged shape predates this view.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import spool_usage_service

pytestmark = pytest.mark.asyncio


# ── seeding helpers ──────────────────────────────────────────────────────────

BASE = datetime(2026, 8, 1, 12, 0, 0)


async def _spool(db_session, **fields) -> Spool:
    defaults = {
        "material": "PLA",
        "brand": "Generic",
        "color_name": "Black",
        "rgba": "000000FF",
        "label_weight": 1000,
        "weight_used": 0,
    }
    defaults.update(fields)
    spool = Spool(**defaults)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    return spool


async def _usage(db_session, spool, **fields) -> SpoolUsageHistory:
    defaults = {
        "spool_id": spool.id,
        "print_name": "benchy.3mf",
        "weight_used": 10.0,
        "percent_used": 1,
        "status": "completed",
        "created_at": BASE,
    }
    defaults.update(fields)
    row = SpoolUsageHistory(**defaults)
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _ids(db_session, *, sort_by=None, **filter_kwargs) -> list[int]:
    filters = spool_usage_service.build_usage_filters(**filter_kwargs)
    rows = await spool_usage_service.list_usage(db_session, filters=filters, sort_by=sort_by)
    return [row.id for row, _spool, _printer in rows]


# ── the filter matrix ────────────────────────────────────────────────────────


async def test_newest_first_is_the_default_and_the_id_breaks_the_tie(db_session):
    """A history is read from its end, and a runout close-out lands in the same
    second as the print's own rows — without the id tiebreak the two swap places
    between requests, which on a PAGED list means a row appearing twice."""
    spool = await _spool(db_session)
    older = await _usage(db_session, spool, created_at=BASE - timedelta(hours=1))
    same_a = await _usage(db_session, spool, created_at=BASE)
    same_b = await _usage(db_session, spool, created_at=BASE)

    assert await _ids(db_session) == [same_b.id, same_a.id, older.id]


async def test_the_search_reaches_the_print_the_spool_and_the_printer(db_session, printer_factory):
    """One box, three sources — an operator remembers the print's name, the
    reel's brand or the machine, and should not have to know which."""
    printer = await printer_factory(name="Printer 5")
    sunlu = await _spool(db_session, brand="SUNLU", color_name="Red")
    generic = await _spool(db_session, brand="Generic", color_name="Black")

    by_print = await _usage(db_session, generic, print_name="dragon_body.3mf")
    by_brand = await _usage(db_session, sunlu, print_name="benchy.3mf")
    by_printer = await _usage(db_session, generic, print_name="cube.3mf", printer_id=printer.id)

    assert await _ids(db_session, q="dragon") == [by_print.id]
    assert await _ids(db_session, q="sunlu") == [by_brand.id]
    assert await _ids(db_session, q="Printer 5") == [by_printer.id]


async def test_the_search_reaches_the_name_the_template_builds(db_session):
    """The same fix the spool list got (2026-09-01): this view renders the
    operator's naming template, so a search that could not match what it shows
    would be the identical bug, one screen over. ``{lot}`` is in no raw search
    column — it is only in the name."""
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spool_display_template", value="{brand} {material} #{lot}"))
    await db_session.commit()

    target_spool = await _spool(db_session, brand="SUNLU", material="PETG", lot=42)
    other = await _spool(db_session, brand="SUNLU", material="PETG", lot=None)
    hit = await _usage(db_session, target_spool)
    await _usage(db_session, other)

    filters = spool_usage_service.build_usage_filters(q="#42", display_name_template="{brand} {material} #{lot}")
    rows = await spool_usage_service.list_usage(db_session, filters=filters)
    assert [row.id for row, _s, _p in rows] == [hit.id]


async def test_the_spool_id_is_searchable_here_too(db_session):
    spool = await _spool(db_session, brand="SUNLU")
    other = await _spool(db_session, brand="SUNLU")
    hit = await _usage(db_session, spool)
    await _usage(db_session, other)

    assert await _ids(db_session, q=str(spool.id)) == [hit.id]


async def test_every_search_token_must_match_something(db_session):
    """AND across tokens, OR across columns — 'sunlu red' finds the SUNLU Red
    row and not every SUNLU row plus every Red one."""
    sunlu_red = await _spool(db_session, brand="SUNLU", color_name="Red")
    sunlu_black = await _spool(db_session, brand="SUNLU", color_name="Black")
    hit = await _usage(db_session, sunlu_red)
    await _usage(db_session, sunlu_black)

    assert await _ids(db_session, q="sunlu red") == [hit.id]


async def test_the_printer_filter_carries_a_no_printer_sentinel(db_session, printer_factory):
    printer = await printer_factory(name="P1")
    spool = await _spool(db_session)
    on_printer = await _usage(db_session, spool, printer_id=printer.id)
    orphaned = await _usage(db_session, spool, printer_id=None)

    assert await _ids(db_session, printer_id=str(printer.id)) == [on_printer.id]
    assert await _ids(db_session, printer_id="__none__") == [orphaned.id]


async def test_statuses_are_a_set_not_a_single_choice(db_session):
    spool = await _spool(db_session)
    done = await _usage(db_session, spool, status="completed")
    runout = await _usage(db_session, spool, status="runout")
    await _usage(db_session, spool, status="ams_sync")

    assert set(await _ids(db_session, statuses=["completed", "runout"])) == {done.id, runout.id}


async def test_the_date_window_is_inclusive_at_the_start_and_exclusive_at_the_end(db_session):
    """``date_to`` is exclusive so the client can send the start of the day
    after and lose nothing to an off-by-one at midnight."""
    spool = await _spool(db_session)
    before = await _usage(db_session, spool, created_at=BASE - timedelta(days=1))
    at_start = await _usage(db_session, spool, created_at=BASE)
    at_end = await _usage(db_session, spool, created_at=BASE + timedelta(days=1))

    ids = await _ids(db_session, date_from=BASE, date_to=BASE + timedelta(days=1))
    assert ids == [at_start.id]
    assert before.id not in ids
    assert at_end.id not in ids


async def test_an_archived_spool_keeps_its_history(db_session):
    """Retiring the reel does not un-burn the filament — hiding those rows would
    make the totals here disagree with the archives."""
    archived = await _spool(db_session, archived_at=datetime(2026, 8, 20, 0, 0, 0))
    row = await _usage(db_session, archived)

    assert await _ids(db_session) == [row.id]


async def test_the_spool_state_filters_are_off_until_asked(db_session, printer_factory):
    """Added 2026-09-01 on request, with a third answer the spool list has no
    equivalent of: ``all``, and it is the DEFAULT. Retiring or unloading a reel
    does not un-burn what it printed, so nothing here is hidden unasked — and a
    default that hid rows would make this view's own totals disagree with the
    archives."""
    from backend.app.models.spool_assignment import SpoolAssignment

    printer = await printer_factory()
    shelf = await _spool(db_session, brand="Shelf")
    loaded = await _spool(db_session, brand="Loaded")
    retired = await _spool(db_session, brand="Retired", archived_at=datetime(2026, 8, 20, 0, 0, 0))

    db_session.add(SpoolAssignment(spool_id=loaded.id, printer_id=printer.id, ams_id=0, tray_id=0))
    await db_session.commit()

    on_shelf = await _usage(db_session, shelf)
    in_printer = await _usage(db_session, loaded)
    archived_row = await _usage(db_session, retired)

    # The default answers for all three.
    assert set(await _ids(db_session)) == {on_shelf.id, in_printer.id, archived_row.id}

    assert set(await _ids(db_session, archived="active")) == {on_shelf.id, in_printer.id}
    assert await _ids(db_session, archived="archived") == [archived_row.id]
    assert await _ids(db_session, assigned="assigned") == [in_printer.id]
    assert set(await _ids(db_session, assigned="unassigned")) == {on_shelf.id, archived_row.id}


async def test_an_orphaned_row_is_neither_active_nor_assigned(db_session):
    """⚠️ ``archived_at IS NULL`` is also true when the whole spool row is NULL.
    A bare check would count NULL-because-deleted as NULL-because-not-archived
    and file an orphan under "active", where there is no spool to be active."""
    spool = await _spool(db_session)
    row = await _usage(db_session, spool)
    await db_session.delete(spool)
    await db_session.commit()

    assert await _ids(db_session) == [row.id]
    assert await _ids(db_session, archived="active") == []
    assert await _ids(db_session, assigned="unassigned") == [row.id]


async def test_a_row_whose_spool_is_gone_still_shows(db_session):
    """SQLite never has ``PRAGMA foreign_keys = ON`` here, so a deleted spool
    leaves its usage rows behind. An inner join would make them disappear from
    the only screen that could show somebody they exist."""
    spool = await _spool(db_session)
    row = await _usage(db_session, spool)
    await db_session.delete(spool)
    await db_session.commit()

    filters = spool_usage_service.build_usage_filters()
    rows = await spool_usage_service.list_usage(db_session, filters=filters)
    assert [r.id for r, _s, _p in rows] == [row.id]
    assert rows[0][1] is None


# ── sorting ──────────────────────────────────────────────────────────────────


async def test_sorting_by_weight_and_by_the_composite_spool_key(db_session):
    heavy_sunlu = await _spool(db_session, brand="SUNLU", material="PETG")
    light_generic = await _spool(db_session, brand="Generic", material="PLA")
    heavy = await _usage(db_session, heavy_sunlu, weight_used=90.0)
    light = await _usage(db_session, light_generic, weight_used=5.0)

    assert await _ids(db_session, sort_by="weight_used_desc") == [heavy.id, light.id]
    assert await _ids(db_session, sort_by="weight_used_asc") == [light.id, heavy.id]
    # material first, then brand, then colour — PETG sorts before PLA
    assert await _ids(db_session, sort_by="spool_asc") == [heavy.id, light.id]


async def test_an_unknown_sort_falls_back_instead_of_failing(db_session):
    """A stale bookmark should still open the page — the same permissive
    convention every other server-driven list here follows."""
    spool = await _spool(db_session)
    older = await _usage(db_session, spool, created_at=BASE - timedelta(hours=1))
    newer = await _usage(db_session, spool, created_at=BASE)

    assert await _ids(db_session, sort_by="nonsense_asc") == [newer.id, older.id]
    assert await _ids(db_session, sort_by="created_at") == [newer.id, older.id]


# ── totals ───────────────────────────────────────────────────────────────────


async def test_the_totals_cover_the_filter_not_the_page(db_session):
    spool = await _spool(db_session)
    await _usage(db_session, spool, weight_used=10.0, cost=1.5)
    await _usage(db_session, spool, weight_used=25.0, cost=2.5)

    filters = spool_usage_service.build_usage_filters()
    # One row on the page; the totals still answer for both.
    page = await spool_usage_service.list_usage(db_session, filters=filters, limit=1)
    totals = await spool_usage_service.usage_totals(db_session, filters=filters)

    assert len(page) == 1
    assert totals["weight_used"] == pytest.approx(35.0)
    assert totals["cost"] == pytest.approx(4.0)


async def test_an_unpriced_history_reports_no_cost_rather_than_zero(db_session):
    """0.00 reads as "free"; None reads as "unpriced", which is the truth."""
    spool = await _spool(db_session)
    await _usage(db_session, spool, weight_used=10.0, cost=None)

    totals = await spool_usage_service.usage_totals(db_session, filters=spool_usage_service.build_usage_filters())
    assert totals["cost"] is None
    assert totals["weight_used"] == pytest.approx(10.0)


# ── facets ───────────────────────────────────────────────────────────────────


async def test_the_facets_name_the_printer_that_burned_it(db_session, printer_factory):
    printer = await printer_factory(name="Printer 5")
    spool = await _spool(db_session, brand="SUNLU", material="PETG")
    await _usage(db_session, spool, printer_id=printer.id, status="runout")
    await _usage(db_session, spool, printer_id=None, status="completed")

    facets = await spool_usage_service.usage_facets(db_session, filters=[])

    assert facets["printers"] == [{"id": printer.id, "name": "Printer 5", "archived": False}]
    assert set(facets["statuses"]) == {"completed", "runout"}
    assert facets["materials"] == ["PETG"]
    assert facets["brands"] == ["SUNLU"]


async def test_a_retired_printer_is_still_offered_and_flagged(db_session, async_client, printer_factory):
    """The client labels a retired printer generically rather than by a name that
    may since have been reused (``utils/printerLabel.ts``), so the flag has to
    ride along with the name — dropping the row instead would hide a whole
    machine's history the moment it was retired."""
    printer = await printer_factory(name="Old P1S", archived=True)
    spool = await _spool(db_session)
    await _usage(db_session, spool, printer_id=printer.id)

    facets = await spool_usage_service.usage_facets(db_session, filters=[])
    assert facets["printers"] == [{"id": printer.id, "name": "Old P1S", "archived": True}]

    body = (await async_client.get("/api/v1/inventory/usage", params={"page": 1})).json()
    assert body["items"][0]["printer_archived"] is True


# ── the route ────────────────────────────────────────────────────────────────


async def test_the_bare_call_still_answers_with_the_old_flat_array(db_session, async_client):
    """The unpaged shape is public API and predates the History view."""
    spool = await _spool(db_session)
    await _usage(db_session, spool)

    response = await async_client.get("/api/v1/inventory/usage")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body[0]["spool_id"] == spool.id
    assert "printer_name" not in body[0]


async def test_the_paged_call_carries_the_spool_and_the_printer_with_each_row(
    db_session, async_client, printer_factory
):
    printer = await printer_factory(name="Printer 5")
    spool = await _spool(db_session, brand="SUNLU", color_name="Red", rgba="FF0000FF")
    await _usage(db_session, spool, printer_id=printer.id, weight_used=42.0, cost=3.0)

    response = await async_client.get("/api/v1/inventory/usage", params={"page": 1, "per_page": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["meta"] == {"total": 1, "current_page": 1, "per_page": 10, "last_page": 1}
    assert body["totals"] == {"weight_used": 42.0, "cost": 3.0}
    item = body["items"][0]
    assert item["printer_name"] == "Printer 5"
    # Every field a display-name template can read travels with the row: the
    # name is composed in the browser, and a shorter payload would silently drop
    # whichever placeholder the operator actually configured.
    assert item["spool"]["brand"] == "SUNLU"
    assert item["spool"]["rgba"] == "FF0000FF"
    assert item["spool"]["archived"] is False
    assert item["spool"]["label_weight"] == 1000
    assert item["spool"]["filament_diameter"] == "1.75"


async def test_the_paged_call_pages(db_session, async_client):
    spool = await _spool(db_session)
    for offset in range(5):
        await _usage(db_session, spool, created_at=BASE + timedelta(minutes=offset))

    first = (await async_client.get("/api/v1/inventory/usage", params={"page": 1, "per_page": 2})).json()
    second = (await async_client.get("/api/v1/inventory/usage", params={"page": 2, "per_page": 2})).json()

    assert first["meta"]["total"] == 5
    assert first["meta"]["last_page"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})


async def test_a_status_param_can_repeat(db_session, async_client):
    spool = await _spool(db_session)
    await _usage(db_session, spool, status="completed")
    await _usage(db_session, spool, status="runout")
    await _usage(db_session, spool, status="ams_sync")

    response = await async_client.get(
        "/api/v1/inventory/usage",
        params=[("page", 1), ("status", "completed"), ("status", "runout")],
    )

    assert response.status_code == 200
    assert response.json()["meta"]["total"] == 2


async def test_a_bogus_printer_id_is_a_400_not_a_500(async_client):
    """The params here get baked into a shareable URL, so a hand-edited one must
    fail as a bad request rather than blowing up inside ``int()``."""
    response = await async_client.get("/api/v1/inventory/usage", params={"page": 1, "printer_id": "everything"})
    assert response.status_code == 422


async def test_the_facets_route_answers_the_dropdowns(db_session, async_client, printer_factory):
    printer = await printer_factory(name="Printer 5")
    spool = await _spool(db_session, brand="SUNLU", material="PETG")
    await _usage(db_session, spool, printer_id=printer.id, status="runout")

    response = await async_client.get("/api/v1/inventory/usage/facets")

    assert response.status_code == 200
    body = response.json()
    assert body["statuses"] == ["runout"]
    assert body["printers"] == [{"id": printer.id, "name": "Printer 5", "archived": False}]
    assert body["materials"] == ["PETG"]
    assert body["brands"] == ["SUNLU"]


async def test_the_utc_window_the_client_sends_is_honoured(db_session, async_client):
    spool = await _spool(db_session)
    inside = await _usage(db_session, spool, created_at=BASE)
    await _usage(db_session, spool, created_at=BASE - timedelta(days=3))

    response = await async_client.get(
        "/api/v1/inventory/usage",
        params={
            "page": 1,
            "date_from": (BASE - timedelta(hours=1)).replace(tzinfo=timezone.utc).isoformat(),
            "date_to": (BASE + timedelta(hours=1)).replace(tzinfo=timezone.utc).isoformat(),
        },
    )

    assert response.status_code == 200
    assert [i["id"] for i in response.json()["items"]] == [inside.id]
