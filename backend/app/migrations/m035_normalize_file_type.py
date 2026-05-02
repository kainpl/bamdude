"""Normalise ``library_files.file_type`` for sliced 3MFs.

Three code paths historically wrote different ``file_type`` values for
the same physical container (a ``.gcode.3mf`` zip with embedded G-code):

- The upload route (``routes/library.py:240``) naïvely took the trailing
  extension and stored ``"3mf"`` — same as a project / unsliced 3MF.
- The external folder scan (``routes/library.py:1194``) recognised the
  compound suffix and stored the unique value ``"gcode.3mf"`` (only used
  in this one path; nothing in the UI ever rendered it).
- The slicer-output path (``routes/library.py:1710``) stored ``"gcode"``,
  treating sliced output as ready-to-print regardless of container.

The result was an inconsistent file-manager: an uploaded sliced 3MF
showed the green "3MF" badge, while the same file produced by the slicer
showed the blue "GCODE" badge. m035 picks the slicer's interpretation as
the canonical one (the sliced 3MF *is* a G-code carrier) and rewrites
both legacy values to ``"gcode"`` in one shot.

Composite identity ("this is a 3MF *and* it carries G-code") is exposed
separately by :data:`library_files.file_tags` (added by m036 right after
this migration) so the green-3MF + blue-GCODE distinction the UI used
to make is still available — just rendered as two badges instead of
diverging primary types.

Tested against fresh installs (no-op — no rows match) and upgrade
installs that have a mix of historical ``"3mf"``, ``"gcode"``, and
``"gcode.3mf"`` rows.
"""

from sqlalchemy import text

version = 35
name = "normalize_file_type"


async def upgrade(conn):
    # Sliced 3MF rows that the upload path or external-scan helper wrote
    # with the wrong primary type. Driven by filename (the bytes we have)
    # rather than file_type so we catch both legacy values in one query.
    await conn.execute(
        text(
            """
            UPDATE library_files
               SET file_type = 'gcode'
             WHERE LOWER(filename) LIKE '%.gcode.3mf'
               AND file_type <> 'gcode'
            """
        )
    )


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
