"""Add Project.url + Project.cover_image_filename for B.2 (#1155).

Both nullable so existing projects keep their current shape on upgrade —
the UI treats NULL as "no link / no cover image" and renders nothing in
that slot. Validation that ``url`` is http(s)-only lives in the Pydantic
layer; the column itself is plain text so backups + manual SQL stay
ergonomic.
"""

from backend.app.migrations.helpers import add_column

version = 27
name = "project_url_and_cover"


async def upgrade(conn):
    await add_column(conn, "projects", "url VARCHAR(2048)")
    await add_column(conn, "projects", "cover_image_filename VARCHAR(255)")


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
