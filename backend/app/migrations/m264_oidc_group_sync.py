"""OIDC group sync on every login (N07 spec §G).

- ``oidc_providers.group_claim`` — the ID-token claim carrying the IdP groups
  (``groups`` by default, the same name rules as ``email_claim``).
- ``oidc_providers.group_mapping`` — ``{"<IdP group>": <BamDude group id>}``. An
  empty object means no sync. It stores group ids, never names: m263 made group
  numbers never reissued first, so a deleted group's number names nobody.
- ``oidc_providers.policy_revision`` — a random value that changes with every
  effective change of a policy field (``models/oidc_provider.py``). A login attempt
  carries the revision it started under and is refused when the provider changed
  since. Existing rows start with one constant value; any change replaces it.
- ``user_oidc_links.first_login_pending`` — set on a link the callback created
  together with a new account. The account has no groups until its first issued
  session applies the default group and the managed part as one result.
- ``auth_ephemeral_tokens.oidc_login_evidence`` — the login evidence an OIDC state,
  exchange or pre-auth row carries (``services/oidc_group_sync.py``). Tokens issued
  before this migration have NULL and lead to a new login; local and LDAP pre-auth
  rows keep NULL as their normal value.

SQLite takes the same declared types as ``create_all`` (``JSON``, ``VARCHAR``,
``BOOLEAN``), the same NOT NULL and the same defaults, so the migrated and the fresh
schema are equivalent; the SQLite→PostgreSQL import carries the values as they are.
"""

from backend.app.migrations.helpers import add_column

version = 264
name = "oidc_group_sync"


async def upgrade(conn):
    await add_column(conn, "oidc_providers", "group_claim VARCHAR(64) NOT NULL DEFAULT 'groups'")
    await add_column(conn, "oidc_providers", "group_mapping JSON NOT NULL DEFAULT '{}'")
    await add_column(conn, "oidc_providers", "policy_revision VARCHAR(32) NOT NULL DEFAULT '0'")
    await add_column(conn, "user_oidc_links", "first_login_pending BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "auth_ephemeral_tokens", "oidc_login_evidence JSON")


replay = upgrade
