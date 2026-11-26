"""P-ID T5: a SQLite install's ZIP backup keeps the printer counter through the real restore route.

Run in a fresh process whose DATA_DIR holds only test files (``test_printer_id_zip_restore.py``).
``backup`` creates printers, deletes the newest, takes a ZIP of the live WAL database, lets the
live database move on, and restores the ZIP; ``after`` is the restart the restore asks for. Prints
one JSON report.
"""

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text


def _printer(index: int):
    from backend.app.models.printer import Printer

    return Printer(
        name=f"zip {index}", serial_number=f"ZIP{index:05d}", ip_address="192.0.2.30", access_code="00000000"
    )


async def _add(index: int) -> int:
    from backend.app.core import database

    async with database.async_session() as db:
        printer = _printer(index)
        db.add(printer)
        await db.commit()
        return printer.id


async def _delete(printer_id: int) -> None:
    from backend.app.core import database

    async with database.async_session() as db:
        await db.execute(text("DELETE FROM printers WHERE id = :i"), {"i": printer_id})
        await db.commit()


async def _scalar(sql: str):
    from backend.app.core import database

    async with database.async_session() as db:
        return (await db.execute(text(sql))).scalar()


async def main(mode: str, out: Path) -> dict:
    from fastapi import UploadFile

    from backend.app.api.routes.settings import create_backup_zip, restore_backup
    from backend.app.core import database

    try:
        await database.init_db()
        if mode == "backup":
            ids = [await _add(i) for i in range(3)]
            await _delete(ids[-1])  # the newest: max(id) + 1 would hand it out again
            journal = await _scalar("PRAGMA journal_mode")
            backup, _ = await create_backup_zip(out)
            later = await _add(9)  # the live database moves past the backup
            await _delete(later)
            async with database.async_session() as db:
                with backup.open("rb") as stream:
                    response = await restore_backup(file=UploadFile(file=stream, filename="backup.zip"), db=db, _=None)
            restored = isinstance(response, dict) and response.get("success") is True
            return {"ids": ids, "later": later, "journal_mode": journal, "restored": restored}
        if mode == "after":
            rows = await _scalar("SELECT group_concat(id) FROM (SELECT id FROM printers ORDER BY id)")
            counter = await _scalar("SELECT seq FROM sqlite_sequence WHERE name = 'printers'")
            ddl = await _scalar("SELECT sql FROM sqlite_schema WHERE name = 'printers'")
            return {
                "rows": rows,
                "counter": counter,
                "autoincrement": "AUTOINCREMENT" in ddl.upper(),
                "next": await _add(5),
            }
        raise ValueError(mode)
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1], Path(sys.argv[2])))))
