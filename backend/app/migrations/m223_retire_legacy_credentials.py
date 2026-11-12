"""Irreversibly retire every pre-contract API/LL credential; record notice facts.

The owner selected strict reissue, including disabled/expired/orphan/ownerless
rows. Historical numeric owners or creation-time gaps cannot prove ownership.
Keep the existing key UUID and connector receipt namespace unchanged. This
migration does not import current models, services, templates or recipient
policy and never sends mail, WebSockets or inbox rows. Runtime delivers the
durable pending episode only after technical admission. A repeated run preserves
its original counts and marker and never retires a current guarded credential.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from backend.app.migrations.helpers import table_exists

version = 223
name = "retire_legacy_credentials"

CONTRACT_KEY = "g01-credential-contract-v1"


async def upgrade(conn):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ownerless = owned = long_lived = 0
    if await table_exists(conn, "api_keys"):
        counts = (
            await conn.execute(
                text(
                    "SELECT user_id IS NULL AS ownerless, COUNT(*) FROM api_keys "
                    "WHERE COALESCE(contract_version, 0) < 1 GROUP BY user_id IS NULL"
                )
            )
        ).all()
        ownerless = sum(count for missing, count in counts if missing)
        owned = sum(count for missing, count in counts if not missing)
        await conn.execute(
            text(
                "UPDATE api_keys SET enabled = :disabled, retired_at = COALESCE(retired_at, :now), "
                "retired_reason = COALESCE(retired_reason, CASE WHEN user_id IS NULL "
                "THEN 'legacy_ownerless_reissue_required' ELSE 'legacy_credential_reissue_required' END) "
                "WHERE COALESCE(contract_version, 0) < 1"
            ),
            {"disabled": False, "now": now},
        )
    if await table_exists(conn, "long_lived_tokens"):
        long_lived = await conn.scalar(
            text("SELECT COUNT(*) FROM long_lived_tokens WHERE COALESCE(contract_version, 0) < 1")
        )
        await conn.execute(
            text(
                "UPDATE long_lived_tokens SET revoked_at = COALESCE(revoked_at, :now), "
                "retired_at = COALESCE(retired_at, :now), "
                "retired_reason = COALESCE(retired_reason, 'legacy_credential_reissue_required') "
                "WHERE COALESCE(contract_version, 0) < 1"
            ),
            {"now": now},
        )
    if not (ownerless or owned or long_lived):
        return
    await conn.execute(
        text(
            "INSERT INTO credential_rollout_episodes "
            "(id, contract_key, ownerless_api_key_count, owned_api_key_count, long_lived_token_count, created_at, delivered_at) "
            "VALUES (:id, :contract, :ownerless, :owned, :ll, :now, NULL) "
            "ON CONFLICT (contract_key) DO NOTHING"
        ),
        {
            "id": str(uuid.uuid4()),
            "contract": CONTRACT_KEY,
            "ownerless": ownerless,
            "owned": owned,
            "ll": long_lived,
            "now": now,
        },
    )
