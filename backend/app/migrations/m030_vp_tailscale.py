"""Add ``virtual_printers.tailscale_disabled`` for B.6 + A.16 (#1070).

Per-VP toggle for Tailscale Let's Encrypt cert provisioning. When False,
``manager.py`` asks the local ``tailscale`` CLI for an LE cert and advertises
the tailnet FQDN over SSDP — slicers connect via a hostname that matches the
trusted cert, no manual CA install required. Defaults to True (opt-in) since
most installs don't run Tailscale.
"""

from backend.app.migrations.helpers import add_column

version = 30
name = "vp_tailscale"


async def upgrade(conn):
    await add_column(conn, "virtual_printers", "tailscale_disabled BOOLEAN NOT NULL DEFAULT 1")


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
