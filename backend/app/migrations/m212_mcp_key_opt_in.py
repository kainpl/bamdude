"""Explicit MCP and MCP-log opt-ins, denied on every existing API key.

Read-status permission predates MCP; it must never opt a key into a new
transport or into model-visible logs. Both fresh and upgraded databases
therefore default these flags to false. There is no permission/group seed.
"""

from backend.app.migrations.helpers import add_column

version = 212
name = "mcp_key_opt_in"


async def upgrade(conn):
    await add_column(conn, "api_keys", "can_access_mcp BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "api_keys", "can_read_mcp_logs BOOLEAN NOT NULL DEFAULT 0")
