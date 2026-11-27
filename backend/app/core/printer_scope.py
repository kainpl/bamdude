"""Which printers a caller may see and act on — one rule for every caller (N62, upstream Bambuddy #1727).

Spec: vault ``60-specs/n62-printer-scoped-groups-spec.md``. Permissions answer WHAT a caller
may do; a printer scope answers ON WHICH PRINTERS. A scope is ``frozenset[int] | None``:
``None`` is every printer, an empty set is none.

* **A person** (:func:`user_scope`): an administrator reaches every printer. Anyone else —
  the union of the reach of their groups with ``restrict_printers``; a group's reach is
  the printers picked for it plus every printer standing in one of its locations OR IN A
  LOCATION BELOW IT (owner decision D3), printers added there later included. A person in
  no such group reaches every printer: permission groups (Operators, Viewers) combine with
  team groups without widening them. A deactivated person reaches none.
* **An API key** (:func:`key_scope`): its own ``printer_ids`` ∩ its owner's scope (D5) — a
  key never reaches a printer its owner cannot.
* **A Telegram chat** (:func:`chat_scope`): its own ``printer_ids`` ∩ the reach of its group
  when that group is restricted (D4). A linked user widens nothing — the chat is the
  authority.
* **Background work on a person's behalf** (:func:`author_scope`): the author's scope, read
  again at every decision; no author (a Virtual Printer's job, a chat without a user, an
  old row) is no person — every printer.

⚠️ The stored lists (``APIKey.printer_ids``, ``TelegramChat.printer_ids``) are read HERE and
nowhere else (guard ``test_printer_lists_are_read_by_the_scope``); a reader that went to
the column directly would miss the owner's or the group's narrowing.

A request's scope is worked out once, by the permission gate or by a door with its own
token (``core/api_key_scope``), and kept on ``request.state``: :func:`request_printer_scope`
is the one reader, :func:`in_request_scope` the one test.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import Request
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.group import Group, group_locations, group_printers, user_groups

Scope = frozenset[int] | None

_STATE_ATTR = "printer_scope"
# Who the scope on the request belongs to: "person" refuses with 404, "key" with 403 (spec §12.1).
_KIND_ATTR = "printer_scope_kind"
_LISTED_ATTR = "printer_scope_key_listed"


# -- the arithmetic -----------------------------------------------------------------


def intersect(first: Scope, second: Scope) -> Scope:
    if first is None:
        return second
    if second is None:
        return first
    return first & second


def allows(scope: Scope, printer_id: int | None) -> bool:
    """Is ``printer_id`` inside ``scope``? No printer (``None``) always is — a row bound to no
    printer (an orphaned archive, an Auto Queue job not placed yet) is nobody's to hide."""
    return scope is None or printer_id is None or printer_id in scope


def where(scope: Scope, column):
    """A WHERE clause keeping rows of ``column`` (a printer id) inside ``scope``, rows bound to
    no printer included; ``None`` when there is nothing to narrow."""
    if scope is None:
        return None
    return column.is_(None) | column.in_(scope)


def where_strict(scope: Scope, column):
    """Like :func:`where` but rows bound to no printer go too."""
    if scope is None:
        return None
    return column.in_(scope)


# -- a request's scope ----------------------------------------------------------------


def set_request_scope(request: Request | None, scope: Scope, kind: str, *, key_listed: bool = False) -> None:
    if request is not None:
        setattr(request.state, _STATE_ATTR, scope)
        setattr(request.state, _KIND_ATTR, kind)
        setattr(request.state, _LISTED_ATTR, key_listed)


def request_key_listed(request: Request | None) -> bool:
    """Does this request come from an API key with a printer list of its OWN?

    The key policies older than N62 — a printer-scoped key reads no storage sensor and no
    whole-order recount — are about such a key, an integration bound to its printers. A key
    narrowed only by its owner's groups is held where the owner is, and no further.
    """
    return bool(getattr(request.state, _LISTED_ATTR, False)) if request is not None else False


def request_printer_scope(request: Request | None) -> Scope:
    """The printers this request may reach; ``None`` = no restriction.

    ``request`` is None only when a route function is called directly from Python —
    FastAPI always injects it — and such a call carries no caller.
    """
    if request is None:
        return None
    return getattr(request.state, _STATE_ATTR, None)


def request_scope_kind(request: Request | None) -> str | None:
    return getattr(request.state, _KIND_ATTR, None) if request is not None else None


def in_request_scope(request: Request | None, printer_id: int | None) -> bool:
    return allows(request_printer_scope(request), printer_id)


# -- a group's reach ----------------------------------------------------------------


async def _location_subtree(db: AsyncSession, location_ids: Iterable[int]) -> set[int]:
    """``location_ids`` and every location below them; a ``parent_id`` loop cannot hang."""
    from backend.app.models.printer_location import PrinterLocation  # noqa: PLC0415

    roots = set(location_ids)
    if not roots:
        return set()
    children: dict[int | None, list[int]] = {}
    for loc_id, parent_id in (await db.execute(select(PrinterLocation.id, PrinterLocation.parent_id))).all():
        children.setdefault(parent_id, []).append(loc_id)
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        loc = stack.pop()
        if loc in seen:
            continue
        seen.add(loc)
        stack.extend(children.get(loc, []))
    return seen


async def group_reach(db: AsyncSession, group_ids: Iterable[int]) -> frozenset[int]:
    """Every printer the given groups reach: picked, or standing in one of their locations or
    below it."""
    from backend.app.models.printer import Printer  # noqa: PLC0415

    ids = list(group_ids)
    if not ids:
        return frozenset()
    picked = set(
        (await db.execute(select(group_printers.c.printer_id).where(group_printers.c.group_id.in_(ids)))).scalars()
    )
    granted = (
        await db.execute(select(group_locations.c.location_id).where(group_locations.c.group_id.in_(ids)))
    ).scalars()
    places = await _location_subtree(db, granted)
    located: set[int] = set()
    if places:
        located = set((await db.execute(select(Printer.id).where(Printer.location_id.in_(places)))).scalars())
    return frozenset(picked | located)


async def group_access(db: AsyncSession, group_id: int) -> tuple[list[int], list[int]]:
    """``(printer_ids, location_ids)`` given to a group, sorted."""
    printers = (
        await db.execute(
            select(group_printers.c.printer_id)
            .where(group_printers.c.group_id == group_id)
            .order_by(group_printers.c.printer_id)
        )
    ).scalars()
    locations = (
        await db.execute(
            select(group_locations.c.location_id)
            .where(group_locations.c.group_id == group_id)
            .order_by(group_locations.c.location_id)
        )
    ).scalars()
    return list(printers), list(locations)


async def set_group_access(
    db: AsyncSession,
    group: Group,
    *,
    restrict: bool | None,
    printer_ids: list[int] | None,
    location_ids: list[int] | None,
) -> None:
    """Write a group's printer access: ``None`` leaves a part as it is, a list replaces it.

    Every printer and location named must exist — checked before the first write. Flushes,
    never commits. The live layer hears of it through its bulk-mutation tap on these tables.
    """
    from fastapi import HTTPException  # noqa: PLC0415
    from sqlalchemy import delete, insert  # noqa: PLC0415

    from backend.app.models.printer import Printer  # noqa: PLC0415
    from backend.app.models.printer_location import PrinterLocation  # noqa: PLC0415

    if printer_ids is not None:
        wanted = set(printer_ids)
        found = set((await db.execute(select(Printer.id).where(Printer.id.in_(wanted)))).scalars()) if wanted else set()
        if wanted - found:
            raise HTTPException(status_code=404, detail="Printer not found")
    if location_ids is not None:
        wanted_places = set(location_ids)
        found_places = (
            set((await db.execute(select(PrinterLocation.id).where(PrinterLocation.id.in_(wanted_places)))).scalars())
            if wanted_places
            else set()
        )
        if wanted_places - found_places:
            raise HTTPException(status_code=404, detail="Location not found")
    if restrict is not None:
        group.restrict_printers = restrict
    if printer_ids is not None:
        await db.execute(delete(group_printers).where(group_printers.c.group_id == group.id))
        if printer_ids:
            await db.execute(
                insert(group_printers), [{"group_id": group.id, "printer_id": pid} for pid in sorted(set(printer_ids))]
            )
    if location_ids is not None:
        await db.execute(delete(group_locations).where(group_locations.c.group_id == group.id))
        if location_ids:
            await db.execute(
                insert(group_locations),
                [{"group_id": group.id, "location_id": lid} for lid in sorted(set(location_ids))],
            )
    await db.flush()


async def forget_group_access(db: AsyncSession, group_id: int) -> None:
    """A deleted group's printers and locations (SQLite keeps foreign keys off)."""
    from sqlalchemy import delete  # noqa: PLC0415

    await db.execute(delete(group_printers).where(group_printers.c.group_id == group_id))
    await db.execute(delete(group_locations).where(group_locations.c.group_id == group_id))


async def forget_printer_access(db: AsyncSession, printer_ids: Iterable[int]) -> None:
    """A deleted printer leaves every group that picked it (SQLite keeps foreign keys off)."""
    from sqlalchemy import delete  # noqa: PLC0415

    ids = list(printer_ids)
    if ids:
        await db.execute(delete(group_printers).where(group_printers.c.printer_id.in_(ids)))


async def forget_location_access(db: AsyncSession, location_ids: Iterable[int]) -> None:
    """A deleted location leaves every group it was given to (SQLite keeps foreign keys off)."""
    from sqlalchemy import delete  # noqa: PLC0415

    ids = list(location_ids)
    if ids:
        await db.execute(delete(group_locations).where(group_locations.c.location_id.in_(ids)))


async def _ancestors(db: AsyncSession, location_ids: Iterable[int | None]) -> set[int]:
    """These locations and every location above them; a ``parent_id`` loop cannot hang."""
    from backend.app.models.printer_location import PrinterLocation  # noqa: PLC0415

    start = {loc for loc in location_ids if loc is not None}
    if not start:
        return set()
    parent = dict((await db.execute(select(PrinterLocation.id, PrinterLocation.parent_id))).all())
    seen: set[int] = set()
    for loc in start:
        while loc is not None and loc not in seen:
            seen.add(loc)
            loc = parent.get(loc)
    return seen


async def location_grantees(db: AsyncSession, location_ids: Iterable[int | None]) -> list[str]:
    """Names of the RESTRICTED groups whose reach a printer standing in one of ``location_ids``
    depends on: those given one of these locations or a location above it (D3)."""
    places = await _ancestors(db, location_ids)
    if not places:
        return []
    rows = await db.execute(
        select(Group.name)
        .join(group_locations, group_locations.c.group_id == Group.id)
        .where(group_locations.c.location_id.in_(places), Group.restrict_printers.is_(True))
        .distinct()
        .order_by(Group.name)
    )
    return list(rows.scalars())


async def printer_grantees(db: AsyncSession, printer_id: int, location_id: int | None) -> list[dict]:
    """The RESTRICTED groups that reach one printer (spec §6 "who has access"): ``picked`` when the
    printer is picked for the group, ``location_ids`` — the group's locations that hold it (its own
    or one above it, D3). A group without a limit reaches every printer and is not listed."""
    places = await _ancestors(db, [location_id])
    picked = set(
        (
            await db.execute(
                select(group_printers.c.group_id)
                .join(Group, Group.id == group_printers.c.group_id)
                .where(group_printers.c.printer_id == printer_id, Group.restrict_printers.is_(True))
            )
        ).scalars()
    )
    through: dict[int, list[int]] = {}
    if places:
        rows = await db.execute(
            select(group_locations.c.group_id, group_locations.c.location_id)
            .join(Group, Group.id == group_locations.c.group_id)
            .where(group_locations.c.location_id.in_(places), Group.restrict_printers.is_(True))
        )
        for group_id, loc in rows.all():
            through.setdefault(group_id, []).append(loc)
    ids = picked | set(through)
    if not ids:
        return []
    names = dict((await db.execute(select(Group.id, Group.name).where(Group.id.in_(ids)))).all())
    return sorted(
        (
            {"id": gid, "name": names[gid], "picked": gid in picked, "location_ids": sorted(through.get(gid, []))}
            for gid in ids
            if gid in names
        ),
        key=lambda g: g["name"].lower(),
    )


async def ensure_reach_unchanged(db: AsyncSession, user: Any, location_ids: Iterable[int | None]) -> None:
    """Refuse a non-administrator a change that moves a printer, or a branch of the location tree,
    into or out of a location a restricted group is given (spec §5.6) — it would hand printers
    to a team, or take them away. 403 names the groups."""
    from fastapi import HTTPException  # noqa: PLC0415

    if user is not None and user.is_admin:
        return
    names = await location_grantees(db, location_ids)
    if names:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "printer_access_change",
                "message": "This changes which printers a limited group reaches; ask an administrator",
                "groups": names,
            },
        )


# -- the callers ----------------------------------------------------------------------


async def _restricted_group_ids(db: AsyncSession, user: Any) -> list[int]:
    """The ids of the user's restricted groups — from the loaded groups (``User.groups`` is
    ``selectin``), or asked of the database when the user came without them."""
    state = inspect(user, raiseerr=False)
    if state is not None and "groups" not in state.unloaded:
        return [g.id for g in user.groups if g.restrict_printers]
    rows = await db.execute(
        select(Group.id)
        .join(user_groups, user_groups.c.group_id == Group.id)
        .where(user_groups.c.user_id == user.id, Group.restrict_printers.is_(True))
    )
    return list(rows.scalars())


def user_restricted(user: Any) -> bool:
    """Is this person limited to some printers? From the loaded groups, no database — for an
    answer that only says whether (``/auth/me``), never which."""
    if user is None or user.is_admin:
        return False
    return any(g.restrict_printers for g in user.groups)


async def hold_departing_author(db: AsyncSession, user: Any) -> int:
    """A limited person is being deleted: their waiting Auto Queue jobs would read as "no author —
    every printer" and spread over the farm, so they wait for an administrator to start them
    (``manual_start``) — decided here, at the deletion, never from a NULL author later (N62 r1 B03).
    Flushes; returns how many were held.

    The rows are written one by one after ``lock_auto_rows`` has locked the whole set ascending
    and re-read it (G03 B, A20): a row that left ``pending`` or changed author meanwhile is not
    touched. No row changes its queue or position, so no lane is claimed."""
    from backend.app.models.auto_queue import AutoQueueItem  # noqa: PLC0415
    from backend.app.services.queue_counters import lock_auto_rows  # noqa: PLC0415

    if await user_scope(db, user) is None:
        return 0
    ids = list(
        (
            await db.execute(
                select(AutoQueueItem.id).where(
                    AutoQueueItem.created_by_id == user.id, AutoQueueItem.status == "pending"
                )
            )
        ).scalars()
    )
    held = 0
    locked: dict[int, AutoQueueItem] = await lock_auto_rows(db, ids)
    for row_id in sorted(locked):
        row: AutoQueueItem = locked[row_id]
        if row.status == "pending" and row.created_by_id == user.id and not row.manual_start:
            row.manual_start = True
            held += 1
    await db.flush()
    return held


async def user_scope(db: AsyncSession, user: Any) -> Scope:
    """A person's scope (spec §4). ``None`` = every printer."""
    if user is None:
        return None
    if not user.is_active:
        return frozenset()
    if user.is_admin:
        return None
    restricted = await _restricted_group_ids(db, user)
    if not restricted:
        return None
    return await group_reach(db, restricted)


def key_list(api_key: Any) -> Scope:
    """The key's own ``printer_ids`` — part of :func:`key_scope`, never a scope on its own."""
    if api_key.printer_ids is None:
        return None
    return frozenset(int(pid) for pid in api_key.printer_ids)


async def key_scope(db: AsyncSession, api_key: Any, owner: Any) -> Scope:
    """An API key's scope: its own list ∩ its owner's scope (D5)."""
    return intersect(key_list(api_key), await user_scope(db, owner) if owner is not None else None)


def chat_list(chat: Any) -> Scope:
    """The chat's own ``printer_ids`` (m157) — part of :func:`chat_scope`."""
    if chat is None or chat.printer_ids is None:
        return None
    return frozenset(int(pid) for pid in chat.printer_ids)


async def chat_scope(db: AsyncSession, chat: Any) -> Scope:
    """A Telegram chat's scope: its own list ∩ the reach of its group, when restricted (D4)."""
    if chat is None:
        return None
    group = chat.group
    reach = await group_reach(db, [group.id]) if group is not None and group.restrict_printers else None
    return intersect(chat_list(chat), reach)


async def load_chat_scope(db: AsyncSession, chat: Any) -> Scope:
    """Work out the chat's scope where the chat is loaded and keep it on the row, so its
    synchronous ``allows_printer`` answers with the group's narrowing (N62 r1 B02)."""
    if chat is None:
        return None
    scope = await chat_scope(db, chat)
    chat.__dict__["_loaded_printer_scope"] = scope
    return scope


async def users_outside(db: AsyncSession, user_ids: Iterable[int], printer_id: int | None) -> set[int]:
    """Those of ``user_ids`` whose scope leaves ``printer_id`` out — for a fan-out to people (the
    in-app inbox, spec §5.5). An event of no printer leaves nobody out. One read of the restricted
    memberships; a reach worked out once per set of groups."""
    from backend.app.models.user import User  # noqa: PLC0415

    ids = set(user_ids)
    if printer_id is None or not ids:
        return set()
    limited = set(
        (
            await db.execute(
                select(user_groups.c.user_id)
                .join(Group, Group.id == user_groups.c.group_id)
                .where(user_groups.c.user_id.in_(ids), Group.restrict_printers.is_(True))
            )
        ).scalars()
    )
    if not limited:
        return set()
    reach: dict[frozenset[int], frozenset[int]] = {}
    outside: set[int] = set()
    for user in (await db.execute(select(User).where(User.id.in_(limited)))).scalars():
        if user.is_admin:
            continue
        groups = frozenset(g.id for g in user.groups if g.restrict_printers)
        if groups not in reach:
            reach[groups] = await group_reach(db, groups)
        if printer_id not in reach[groups]:
            outside.add(user.id)
    return outside


async def author_scope(db: AsyncSession, user_id: int | None) -> Scope:
    """The scope background work holds a person's job to (spec §5.4): ``user_id`` None — no
    person, every printer; a missing or deactivated author — none."""
    from backend.app.models.user import User  # noqa: PLC0415

    if user_id is None:
        return None
    user = await db.get(User, user_id)
    if user is None:
        return frozenset()
    return await user_scope(db, user)
