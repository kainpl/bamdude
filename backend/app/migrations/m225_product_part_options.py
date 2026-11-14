"""A part is bound to a SET of variant options, across groups (spec product-part-variant-combinations).

Until now a part named at most ONE option (``product_parts.variant_option_id``) and was
in a line's kit only when the line chose it. ``product_part_options (part_id, option_id,
group_id)`` replaces the column: a part is in a line's kit when, for EVERY group it has
rows in, the line chose one of the options it names there; a part with no rows is in
every kit. ``group_id`` is the option's own group, stored so the kit rule needs no join —
an option never changes group. The primary key is composite, so the table needs no
AUTOINCREMENT on SQLite: its rows are never addressed by a code.

* The table, the same DDL on both dialects, and its ``group_id`` index (named as the
  model's ``index=True`` names it, so a fresh install and a migrated one agree).
* Every existing binding becomes ONE row. The kit a binding gave is exactly the kit its
  row gives, so a saved line's or stock position's ``config_key`` does not move, and
  nothing here writes a line or a position. A binding to an option that no longer
  exists has no group to file it under and is not moved; the service never lets a bound
  option or group be deleted, so there should be none.
* The column goes. SQLite's DROP COLUMN refuses an indexed column, so the index is
  dropped first; and it refuses a column a table-level FOREIGN KEY names — which is how
  ``create_all`` wrote the column on every fresh install between m191 and here (m191's
  own ``add_column`` wrote an inline REFERENCES, which goes with the column). Such a
  table is rebuilt from ITS OWN stored DDL with the column and that one constraint cut
  out — never from a hand-written CREATE TABLE, which is how NOT NULL, DEFAULT and
  UNIQUE get lost — and its other indexes are carried over by ``recreate_table``.
  On an SQLite too old for DROP COLUMN, ``drop_column`` leaves the column in place,
  unread; it is nulled so a re-run cannot bring back a binding the operator removed.

* The move tolerates a row already there (``INSERT OR IGNORE`` on SQLite, ``ON CONFLICT
  DO NOTHING`` on PostgreSQL), so a retry after a run that stopped half way does not fail
  on its own earlier rows. One INFO line says how many bindings moved and how many named
  an option that no longer exists and so did not.
* ``product_variant_groups.requires_part`` — «every value needs its own part», a boolean,
  NOT NULL, default false. Only in a group so marked does a chosen value that no part of
  the kit names close a combination (spec п. 10, owner's decision 2026-11-14): without it
  an optional add-on («Magnet: no / yes», the magnet bound to «yes») would make «no»
  unorderable. Every existing group gets false — the upgrade closes nothing by this rule;
  only an unsliced printed part does.

Idempotent: ``DEBUG=true`` re-runs the newest migration on every start, and with the
column gone there is nothing left to move; the flag column is added only when absent.
"""

import logging
import re

from sqlalchemy import text

from backend.app.migrations.helpers import (
    add_column,
    column_exists,
    drop_column,
    get_table_columns,
    recreate_table,
    table_exists,
)

logger = logging.getLogger(__name__)

version = 225
name = "product_part_options"

_TABLE = "product_parts"
_COLUMN = "variant_option_id"
_INDEX = "ix_product_parts_variant_option_id"

# An identifier as SQLite stores it: bare, "quoted", `quoted` or [quoted].
_IDENT = rf"[\"`\[]?{_COLUMN}[\"`\]]?"
_COLUMN_DEFINITION = re.compile(rf"^{_IDENT}(\s|$)", re.IGNORECASE)
_FOREIGN_KEY_ON_COLUMN = re.compile(rf"^(CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(\s*{_IDENT}\s*\)", re.IGNORECASE)
_MENTION = re.compile(rf"\b{_COLUMN}\b", re.IGNORECASE)


async def upgrade(conn):
    if await table_exists(conn, "product_variant_groups"):
        await add_column(conn, "product_variant_groups", "requires_part BOOLEAN NOT NULL DEFAULT 0")
    if not await table_exists(conn, _TABLE):
        return
    if not await table_exists(conn, "product_part_options"):
        await conn.exec_driver_sql(
            """
            CREATE TABLE product_part_options (
                part_id INTEGER NOT NULL REFERENCES product_parts(id) ON DELETE CASCADE,
                option_id INTEGER NOT NULL REFERENCES product_variant_options(id) ON DELETE CASCADE,
                group_id INTEGER NOT NULL REFERENCES product_variant_groups(id) ON DELETE CASCADE,
                PRIMARY KEY (part_id, option_id)
            )
            """
        )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_product_part_options_group_id ON product_part_options (group_id)"
    )

    if not await column_exists(conn, _TABLE, _COLUMN):
        return
    select_bindings = (
        "SELECT p.id, p.variant_option_id, o.group_id FROM product_parts p "
        "JOIN product_variant_options o ON o.id = p.variant_option_id"
    )
    if conn.dialect.name == "sqlite":
        move = f"INSERT OR IGNORE INTO product_part_options (part_id, option_id, group_id) {select_bindings}"
    else:
        move = (
            f"INSERT INTO product_part_options (part_id, option_id, group_id) {select_bindings} ON CONFLICT DO NOTHING"
        )
    moved = await conn.execute(text(move))
    dangling = await conn.scalar(
        text(
            f"SELECT COUNT(*) FROM {_TABLE} p WHERE p.{_COLUMN} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM product_variant_options o WHERE o.id = p.{_COLUMN})"
        )
    )
    logger.info(
        "m225: moved %d part binding(s) into product_part_options; %d named an option that no longer exists "
        "and were not moved",
        moved.rowcount,
        dangling or 0,
    )
    await _drop_binding_column(conn)


async def _drop_binding_column(conn) -> None:
    await conn.exec_driver_sql(f"DROP INDEX IF EXISTS {_INDEX}")
    if conn.dialect.name == "sqlite":
        stored = (
            await conn.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"), {"t": _TABLE})
        ).scalar_one()
        if any(_FOREIGN_KEY_ON_COLUMN.match(item) for item in _body_items(stored)[0]):
            kept = [c for c in await get_table_columns(conn, _TABLE) if c != _COLUMN]
            await recreate_table(conn, _TABLE, _without_column(stored), ", ".join(kept))
            return
    if not await drop_column(conn, _TABLE, _COLUMN):
        await conn.exec_driver_sql(f"UPDATE {_TABLE} SET {_COLUMN} = NULL WHERE {_COLUMN} IS NOT NULL")


def _body_items(table_sql: str) -> tuple[list[str], str]:
    """``(items, tail)`` of a stored CREATE TABLE: the column definitions and table
    constraints split on the commas at parenthesis depth 0, outside quotes, and what
    follows the closing parenthesis (``WITHOUT ROWID``, ``STRICT``)."""
    open_at, close_at = table_sql.index("("), table_sql.rindex(")")
    body = table_sql[open_at + 1 : close_at]
    items: list[str] = []
    depth, closing, start = 0, None, 0
    for i, ch in enumerate(body):
        if closing:
            if ch == closing:
                closing = None
        elif ch in "'\"`":
            closing = ch
        elif ch == "[":
            closing = "]"
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(body[start:i].strip())
            start = i + 1
    items.append(body[start:].strip())
    return items, table_sql[close_at + 1 :]


def _without_column(table_sql: str) -> str:
    """The table's own DDL with the column's definition and its FOREIGN KEY constraint cut out.
    Anything else that still names the column (a CHECK, a composite key) cannot be cut
    safely, and the migration stops rather than rewrite the table around it."""
    items, tail = _body_items(table_sql)
    kept = [i for i in items if not (_COLUMN_DEFINITION.match(i) or _FOREIGN_KEY_ON_COLUMN.match(i))]
    if any(_MENTION.search(i) for i in kept):
        raise RuntimeError(f"m225: {_TABLE}.{_COLUMN} is named by a constraint it cannot drop: {table_sql}")
    return f"CREATE TABLE {_TABLE} (\n    " + ",\n    ".join(kept) + f"\n){tail}"
