"""Capture existing order kits and finished stock at the visible cutover state."""

from collections import defaultdict
from datetime import datetime, timezone
from logging import getLogger
from uuid import uuid4

from sqlalchemy import JSON, bindparam, text

from backend.app.migrations.helpers import add_column

version = 258
name = "order_line_composition"
logger = getLogger(__name__)


async def upgrade(conn):
    await add_column(conn, "product_parts", "composition_identity VARCHAR(36)")
    await add_column(conn, "project_lines", "composition_revisions JSON")
    await add_column(conn, "stock_items", "variant_key VARCHAR(512)")
    await add_column(conn, "stock_items", "composition_parts JSON")
    ids = (await conn.execute(text("SELECT id FROM product_parts WHERE composition_identity IS NULL"))).scalars().all()
    if ids:
        await conn.execute(
            text("UPDATE product_parts SET composition_identity = :identity WHERE id = :id"),
            [{"identity": str(uuid4()), "id": pid} for pid in ids],
        )
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_product_parts_composition_identity ON product_parts (composition_identity)"
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_stock_items_variant_key ON stock_items (variant_key)"))
    await _backfill(conn, is_line=True)
    await _backfill(conn, is_line=False)


async def _backfill(conn, *, is_line):
    table = "project_lines" if is_line else "stock_items"
    column = "composition_revisions" if is_line else "composition_parts"
    mode = "mode" if is_line else "'product' AS mode"
    owner = "line_id" if is_line else "item_id"
    config_table = "project_line" if is_line else "stock_item"
    assignments = f"{column} = :snapshot"
    if not is_line:
        assignments += ", variant_key = :variant_key"
    write = text(f"UPDATE {table} SET {assignments} WHERE id = :id").bindparams(bindparam("snapshot", type_=JSON))
    last_id = 0
    captured_at = datetime.now(timezone.utc).isoformat()
    while True:
        rows = (
            (
                await conn.execute(
                    text(
                        f"SELECT id, product_id, config_key, {mode} FROM {table} "
                        f"WHERE id > :last AND {column} IS NULL ORDER BY id LIMIT 250"
                    ),
                    {"last": last_id},
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return
        owners = [row["id"] for row in rows]
        choices_by_owner = await _config_rows(conn, config_table, "choices", owner, "group_id, option_id", owners)
        counts_by_owner = await _config_rows(conn, config_table, "part_counts", owner, "part_id, qty", owners)
        catalog = {}
        updates = []
        for row in rows:
            pid = row["product_id"]
            if pid not in catalog:
                catalog[pid] = await _catalog(conn, pid)
            ident = row["id"]
            choices = choices_by_owner[ident]
            overrides = counts_by_owner[ident]
            catalog_parts = {part["id"] for part in catalog[pid][0]}
            catalog_options = {
                (gid, option["option_id"]) for gid, options in catalog[pid][1].items() for option in options
            }
            if overrides.keys() - catalog_parts or choices.items() - catalog_options:
                logger.warning("Composition capture found stale references in %s row %s", table, ident)
            parts, configuration = _capture(catalog[pid], row["mode"], choices, overrides)
            if is_line:
                snapshot = [
                    {
                        "origin": "migration",
                        "actor_id": None,
                        "command": None,
                        "captured_at": captured_at,
                        "parts": parts,
                        "configuration": configuration,
                    }
                ]
                updates.append({"id": ident, "snapshot": snapshot})
            else:
                updates.append(
                    {
                        "id": ident,
                        "snapshot": parts,
                        "variant_key": row["config_key"].partition("|")[0],
                    },
                )
        await conn.execute(write, updates)
        last_id = rows[-1]["id"]


async def _config_rows(conn, table, suffix, owner, columns, ids):
    statement = text(f"SELECT {owner}, {columns} FROM {table}_{suffix} WHERE {owner} IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    out = defaultdict(dict)
    for identifier, key, value in (await conn.execute(statement, {"ids": ids})).all():
        out[identifier][key] = value
    return out


def _capture(catalog, mode, choices, overrides):
    parts, groups, bindings = catalog
    parts_mode = mode == "parts"
    selected = (
        [
            option
            for group, options in groups.items()
            for option in options
            if option["option_id"] == choices.get(group, options[0]["default_option_id"])
        ]
        if not parts_mode
        else []
    )
    # The legacy reader passes explicit choices through even for a dangling
    # option. Derive its actual picked set independently of the display names.
    picked = set(choices.values()) | {
        options[0]["default_option_id"]
        for group, options in groups.items()
        if group not in choices and options[0]["default_option_id"] is not None
    }
    snapshot, changed = [], []
    for part in parts:
        bound = bindings[part["id"]]
        standard = part["qty_per_unit"] if parts_mode or all(picked & ids for ids in bound.values()) else 0
        qty = overrides.get(part["id"], 0 if parts_mode else standard)
        if qty > 0:
            snapshot.append(
                {
                    "id": part["id"],
                    "identity": part["composition_identity"],
                    "qty": qty,
                    "name": part["name"],
                    "kind": part["kind"],
                    "ignored": bool(part["ignored"]),
                    "variant": bool(bound),
                }
            )
        if part["id"] in overrides:
            changed.append({"part_id": part["id"], "name": part["name"], "qty": qty, "standard_qty": standard})
    return snapshot, {
        "choices": [{key: val for key, val in option.items() if key != "default_option_id"} for option in selected],
        "changed_parts": changed,
    }


async def _catalog(conn, pid):
    args = {"pid": pid}
    parts = (
        (
            await conn.execute(
                text(
                    "SELECT id, name, kind, ignored, qty_per_unit, composition_identity FROM product_parts "
                    "WHERE product_id = :pid ORDER BY sort_order, id"
                ),
                args,
            )
        )
        .mappings()
        .all()
    )
    groups = defaultdict(list)
    for row in (
        await conn.execute(
            text(
                "SELECT g.id AS group_id, g.name AS group_name, g.default_option_id, "
                "o.id AS option_id, o.name AS option_name FROM product_variant_groups g "
                "JOIN product_variant_options o ON o.group_id = g.id WHERE g.product_id = :pid "
                "ORDER BY g.position, g.id, o.position, o.id"
            ),
            args,
        )
    ).mappings():
        option = dict(row)
        option["is_default"] = option["option_id"] == option["default_option_id"]
        groups[row["group_id"]].append(option)
    bindings = defaultdict(lambda: defaultdict(set))
    for part, group, option in (
        await conn.execute(
            text(
                "SELECT b.part_id, b.group_id, b.option_id FROM product_part_options b "
                "JOIN product_parts p ON p.id = b.part_id WHERE p.product_id = :pid"
            ),
            args,
        )
    ).all():
        bindings[part][group].add(option)
    return parts, groups, bindings
