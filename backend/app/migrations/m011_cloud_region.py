"""Bambu Cloud region column on users.

Adds ``users.cloud_region VARCHAR(10) NULL`` so per-user cloud
credentials carry their region ("global" or "china") alongside the
token + email. Before this column, the cloud service was a process-wide
singleton that silently leaked whichever region was last logged in to
across all users and all subsequent requests — a china-region login on
user A would pin api.bambulab.cn for user B's next firmware-check hit.

The routes layer was refactored in the same batch to build a
per-request ``BambuCloudService(region=region)`` from the stored value
and ``await cloud.close()`` when done, eliminating the cross-tenant
leak. This migration just opens the storage for that refactor.

Settings-level storage (``settings`` table with ``key='bambu_cloud_region'``)
lives alongside ``bambu_cloud_token`` / ``bambu_cloud_email`` for
legacy installs that pre-date the user column — no schema change
needed there, the key is written at login time via ``store_token``.

Existing rows left at NULL are treated as ``"global"`` by
``_normalise_region`` in ``routes.cloud``; no backfill needed.
"""

from backend.app.migrations.helpers import add_column

version = 11
name = "cloud_region"


async def upgrade(conn):
    await add_column(conn, "users", "cloud_region VARCHAR(10)")


async def seed(session_factory):  # pragma: no cover — no-op, column is self-defaulting
    async with session_factory() as db:
        _ = db  # noqa: ARG001
