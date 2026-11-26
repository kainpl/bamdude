"""The highest group number a database still remembers (N07 spec §B).

SQLite without ``AUTOINCREMENT`` gives the next row ``max(id) + 1``: deleting the
newest group handed its number to the next one, and whatever still named the old
number — an OIDC provider's default group, a Telegram chat's group, a membership
row SQLite never cascaded — belonged to the new group at once. N07's group mapping
stores group numbers, so a reissued number would silently put people into the
wrong group. The migration makes ``groups`` an ``AUTOINCREMENT`` table; its counter
has to start above every number a current reader could still pick up.

The registry is FROZEN at the schema N07 met, like P-ID's: the migration and the
SQLite→PostgreSQL import read databases at older levels. The group mapping
(``oidc_providers.group_mapping``) is not in it: it appears only together with the
counter, so any number it still names after a deletion is already behind the counter.
``ldap_group_mapping`` stores names, and product-variant groups have their own ids.

Presence follows P-ID: a present carrier always counts; ``guaranteed`` names the
migration whose application proves the carrier exists, asked of the applied set and
never of ``MAX(version)``; ``None`` means only ``create_all`` makes it, so its
absence is never an error. Values and domain: ``core/id_allocator.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.core import id_allocator


@dataclass(frozen=True)
class Carrier:
    table: str
    column: str
    guaranteed: int | None


CARRIERS: tuple[Carrier, ...] = (
    Carrier("groups", "id", None),
    Carrier("user_groups", "group_id", None),
    Carrier("oidc_providers", "default_group_id", 51),
    Carrier("telegram_chats", "group_id", None),
)


class GroupIdBoundError(RuntimeError):
    """A carrier the bound must read is missing or has an unexpected type, or no group number is left."""


def _missing(carrier: Carrier, applied: set[int], what: str) -> None:
    if carrier.guaranteed is not None and carrier.guaranteed in applied:
        raise GroupIdBoundError(
            f"{carrier.table}.{carrier.column}: {what}, although migration {carrier.guaranteed} creates it"
        )


def _best(best: int | None, values: list[int]) -> int | None:
    if not values:
        return best
    return max(values) if best is None else max(best, *values)


def sqlite_bound(conn, applied: set[int]) -> int | None:
    """The bound over a SQLite database, through any DB-API connection. Reads only."""
    best: int | None = None
    for carrier in CARRIERS:
        columns = {
            row[1] for row in id_allocator.rows(conn, f"PRAGMA table_xinfo({id_allocator.quote(carrier.table)})")
        }
        if not columns:
            _missing(carrier, applied, "table is missing")
            continue
        if carrier.column not in columns:
            _missing(carrier, applied, "column is missing")
            continue
        best = _best(best, id_allocator.sqlite_int_values(conn, carrier.table, carrier.column))
    return best


sqlite_applied_versions = id_allocator.sqlite_applied_versions


def sqlite_allocator(conn) -> int | None:
    return id_allocator.sqlite_allocator(conn, "groups")


def require_next_number(*highest: int | None) -> None:
    id_allocator.require_next_number("groups", "group", *highest, error=GroupIdBoundError)


_PG_INT_TYPES = {"smallint", "integer", "bigint"}


async def postgres_bound(conn, applied: set[int]) -> int | None:
    """The bound over a PostgreSQL database, on an SQLAlchemy async connection. Reads only."""
    from sqlalchemy import text

    best: int | None = None
    for carrier in CARRIERS:
        result = await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": carrier.table},
        )
        types = dict(result.tuples().all())
        if not types:
            _missing(carrier, applied, "table is missing")
            continue
        if carrier.column not in types:
            _missing(carrier, applied, "column is missing")
            continue
        if types[carrier.column] not in _PG_INT_TYPES:
            raise GroupIdBoundError(f"{carrier.table}.{carrier.column}: unexpected type {types[carrier.column]!r}")
        table, column = id_allocator.quote(carrier.table), id_allocator.quote(carrier.column)
        value = (
            await conn.execute(
                text(f"SELECT MAX({column}) FROM {table} WHERE {column} BETWEEN 1 AND :top"),  # noqa: S608
                {"top": id_allocator.MAX_ID},
            )
        ).scalar()
        best = _best(best, [value] if value is not None else [])
    return best
