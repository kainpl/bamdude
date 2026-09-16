"""Per-printer AMS policies as ONE namespaced JSON object.

Spec: vault 60-specs/ams-backup-compatibility-emulation-spec-plan. The first
namespace is ``backup_compatibility`` (advertised-profile emulation for firmware
auto-refill). Future persistent AMS policies live beside it under their own keys
without schema churn. Deliberately NOT ``ams_settings``: that name is the BS AMS
Settings dialog (routes/schemas/audit) — runtime MQTT commands, not persisted
policy.
"""

from backend.app.migrations.helpers import add_column, json_column_type

version = 175
name = "printer_ams_policies"


async def upgrade(conn):
    await add_column(conn, "printers", f"ams_policies {json_column_type()} NOT NULL DEFAULT '{{}}'")
