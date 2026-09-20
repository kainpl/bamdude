"""Persist semantic AutoQueue rules through promotion; backfill without files or MQTT."""

import json

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 169
name = "auto_queue_filament_routing"


async def upgrade(conn):
    added_feed = await add_column(conn, "auto_queue_items", "feed_policy VARCHAR(20) NOT NULL DEFAULT 'auto'")
    await add_column(conn, "print_queue", "filament_routing TEXT")
    if added_feed:
        await conn.execute(text("UPDATE auto_queue_items SET feed_policy = 'external_only' WHERE use_ams = false"))
    autos = {
        row["id"]: row
        for row in (
            await conn.execute(
                text("SELECT id, feed_policy, force_color_match, filament_overrides FROM auto_queue_items")
            )
        ).mappings()
    }
    rows = (
        (
            await conn.execute(
                text(
                    "SELECT p.id, p.archive_id, p.library_file_id, p.plate_id, p.ams_mapping, p.source_auto_item_id, "
                    "q.printer_id FROM print_queue p JOIN printer_queues q ON p.queue_id = q.id "
                    "WHERE p.filament_routing IS NULL"
                )
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        auto = autos.get(row["source_auto_item_id"])
        pins, overrides, review = {}, [], False
        try:
            if auto:
                overrides = json.loads(auto["filament_overrides"] or "[]")
                if not isinstance(overrides, list):
                    raise ValueError("Invalid overrides")
            else:
                mapping = json.loads(row["ams_mapping"] or "null")
                if isinstance(mapping, list):
                    pins = {
                        str(slot): {"source_id": tray}
                        for slot, tray in enumerate(mapping, 1)
                        if type(tray) is int and tray >= 0
                    }
                review = not bool(pins)
        except (ValueError, TypeError):
            review = True
        snapshot = {
            "version": 1,
            "exact_model": bool(auto),
            "mode": "auto" if auto else "pinned",
            "feed_policy": auto["feed_policy"] if auto else "auto",
            "force_color_match": bool(auto["force_color_match"]) if auto else False,
            "filament_overrides": overrides,
            "physical_pins": pins,
            "review_required": review,
            "source_identity": {
                "kind": "archive" if row["archive_id"] else "library",
                "id": row["archive_id"] or row["library_file_id"],
            },
            "resolved_plate_id": row["plate_id"],
            "printer_id": row["printer_id"],
        }
        await conn.execute(
            text("UPDATE print_queue SET filament_routing = :routing WHERE id = :id"),
            {"routing": json.dumps(snapshot), "id": row["id"]},
        )
