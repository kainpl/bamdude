"""Add ``print_queue.gcode_injection`` for B.17 + A.17 (#422).

Per-job toggle for the new Auto-Print G-code Injection feature. When True,
``background_dispatch`` resolves the ``gcode_snippets`` server setting
(per-model JSON: ``{model: {start_gcode, end_gcode}}``), substitutes
``{header_keys}`` from the 3MF gcode header, and splices the snippets into
the plate gcode at ``; MACHINE_START_GCODE_END`` (start) and EOF (end)
before FTP upload. Defaults to False so existing queue items keep their
current behaviour exactly.
"""

from backend.app.migrations.helpers import add_column

version = 32
name = "gcode_injection"


async def upgrade(conn):
    await add_column(conn, "print_queue", "gcode_injection BOOLEAN NOT NULL DEFAULT 0")


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
