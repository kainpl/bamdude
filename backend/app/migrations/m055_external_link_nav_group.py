"""Per-link sidebar group for external links.

External links now declare which sidebar group they live in
(``operations / workshop / resources / care / system / external``) so
operators can fold third-party shortcuts into the same section as the
related built-in entries instead of being stuck in a separate Links
bucket at the bottom. The frontend ``Layout.tsx`` reads this column to
decide group adjacency for drag-and-drop validation (m054-era cycle:
items may only swap within the same group; whole groups reorder
together via the group header).

Backfill: every existing external link gets ``nav_group='external'`` —
the legacy bucket. Operators move them via the AddExternalLinkModal
group dropdown after upgrading.

Fresh installs go through ``Base.metadata.create_all`` which creates the
column with the same default from ``backend/app/models/external_link.py``.
"""

from backend.app.migrations.helpers import add_column

version = 55
name = "external_link_nav_group"


async def upgrade(conn):
    await add_column(conn, "external_links", "nav_group VARCHAR(20) NOT NULL DEFAULT 'external'")
