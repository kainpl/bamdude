"""Add icon-cache columns to ``oidc_providers``.

Server-side OIDC icon proxy (upstream Bambuddy #1333 / commit 8a7598f6).
The login page renders external IdP icons from `/oidc/providers/{id}/icon`
(same-origin) so the strict ``img-src 'self' data: blob:`` CSP doesn't have
to be relaxed for external IdP hosts. The bytes are fetched server-side at
admin-config time and cached here.

Three new columns alongside the existing ``icon_url`` admin-input field:

- ``icon_data``: BLOB (deferred-load) holding the fetched image bytes.
- ``icon_content_type``: short string ("image/png" etc.) used as the
  "has-icon" flag (read without triggering the deferred load on
  ``icon_data``).
- ``icon_etag``: SHA-256 hex of the bytes, served as the HTTP ``ETag``
  header so clients can revalidate via ``If-None-Match`` and get 304.

The application keeps the triplet co-NULL via the route layer's
``_fetch_icon_or_400`` and ``DELETE /icon``; we ALSO add a DB-level
``CheckConstraint`` on fresh PostgreSQL installs so raw-SQL maintenance
or incident-recovery scripts can't introduce drift. SQLite cannot ``ADD
CONSTRAINT`` to an existing table, so SQLite installs rely on the model's
``__table_args__`` ``CheckConstraint`` taking effect only on fresh
``create_all`` runs — same trade-off documented for other constraints in
the codebase.

Idempotent: ``add_column()`` skips columns that already exist.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column

version = 71
name = "oidc_icon_cache"


async def upgrade(conn):
    await add_column(conn, "oidc_providers", "icon_data BLOB")
    await add_column(conn, "oidc_providers", "icon_content_type VARCHAR(20)")
    await add_column(conn, "oidc_providers", "icon_etag VARCHAR(64)")
    # Stale PostgreSQL installs get the icon-triplet co-NULL CHECK; fresh
    # installs (any backend) already have it via metadata.create_all. SQLite
    # cannot ALTER TABLE ADD CONSTRAINT, so stale-SQLite-installs rely on
    # the application layer (route + DELETE /icon path) for invariant.
    if is_postgres():
        await conn.execute(
            text(
                "ALTER TABLE oidc_providers "
                "ADD CONSTRAINT IF NOT EXISTS ck_oidc_icon_triplet_co_null "
                "CHECK ((icon_data IS NULL) = (icon_content_type IS NULL) "
                "AND (icon_content_type IS NULL) = (icon_etag IS NULL))"
            )
        )
