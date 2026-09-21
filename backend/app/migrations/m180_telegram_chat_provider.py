"""A Telegram chat belongs to the bot it wrote to: ``telegram_chats.provider_id``.

A chat in Telegram exists for exactly one bot — the one whose token was used
when the chat wrote to it — and a ``notification_providers`` row of type
telegram IS a bot (its token). The chat table never recorded which, because
there was only ever one poller and every chat was implicitly its. That held
until a second telegram provider row appeared (nothing prevents one, and the
per-printer providers of before m157 left exactly that shape): the fan-out in
``notification_service`` sent every provider's message to EVERY chat with that
provider's own token, one of them into "chat not found" for chats that had
never written to it, or into duplicates when the two rows carried the same
token.

The column is the binding. It is filled here for every existing chat and, from
now on, by the writers themselves (the auth middleware at registration, the
manual route from the provider it was asked for or the running bot); every
reader that fans a provider's message out to "its chats" filters on it.

**Backfill.** Every chat is bound to the row that registered it, and the only
candidate is the bot that was polling: the OLDEST ENABLED telegram provider
(``telegram_bot.current_bot_provider`` — ``ORDER BY id``, the rule that
migration m157 already leaned on). With no enabled row the oldest telegram
provider of any state takes them — the bot that registered them was enabled
then. With no telegram provider at all the chats are orphans of a bot that
was deleted: nothing can ever message them, so they are removed (counted in
the log). A chat that later writes to a new bot is registered afresh.

**NOT NULL.** The model declares it, so a fresh install gets it from
``create_all`` and a SQLite file moved to PostgreSQL gets it from
``conform_imported_schema`` after the chain (the backfill above leaves no
NULL behind). On PostgreSQL this migration also sets it, and the foreign key
with ``ON DELETE CASCADE``, directly. On SQLite the column stays nullable at
the DDL level, as every constraint this codebase adds by migration does —
SQLite enforces none of them and never gets ``PRAGMA foreign_keys``; the
provider delete route removes a bot's chats in code.

Idempotent: ``DEBUG=true`` re-runs the head migration on every boot.
"""

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column, table_exists

logger = logging.getLogger(__name__)

version = 180
name = "telegram_chat_provider"


async def upgrade(conn):
    if not await table_exists(conn, "telegram_chats"):
        return

    await add_column(conn, "telegram_chats", "provider_id INTEGER")
    await backfill(conn)
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_telegram_chats_provider_id ON telegram_chats (provider_id)"))

    if is_postgres():
        await conn.execute(text("ALTER TABLE telegram_chats ALTER COLUMN provider_id SET NOT NULL"))
        # The constraint is looked up BY COLUMN, never by an assumed name
        # (m018 / m173 are the pattern): a fresh install runs ``create_all``
        # before the chain and already carries this key under PostgreSQL's
        # own generated name, and a second CASCADE key beside it would be a
        # permanent divergence between fresh and upgraded installs.
        await conn.execute(
            text(
                """
                DO $$
                DECLARE
                    existing TEXT;
                BEGIN
                    SELECT c.conname INTO existing
                    FROM pg_constraint c
                    JOIN pg_class t ON c.conrelid = t.oid
                    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                    WHERE t.relname = 'telegram_chats' AND a.attname = 'provider_id' AND c.contype = 'f'
                    LIMIT 1;
                    IF existing IS NULL THEN
                        ALTER TABLE telegram_chats
                            ADD CONSTRAINT telegram_chats_provider_id_fkey
                            FOREIGN KEY (provider_id)
                            REFERENCES notification_providers (id) ON DELETE CASCADE;
                    END IF;
                END$$;
                """
            )
        )


async def backfill(conn) -> tuple[int, int]:
    """Bind every unbound chat to the bot that registered it; drop the orphans.

    Returns ``(bound, removed)``. The candidate order is the poller's own —
    enabled first, then the oldest — see the module docstring.
    """
    # Enabled rows first, then the oldest. Spelled as a CASE rather than
    # ``enabled DESC``: the column is nullable, and a NULL sorts FIRST under
    # DESC on PostgreSQL but last on SQLite — the CASE lands it in the ELSE
    # branch on both, behind every genuinely enabled row, which is also what
    # ``current_bot_provider`` (``enabled == True``) would pick.
    row = (
        await conn.execute(
            text(
                "SELECT id FROM notification_providers WHERE provider_type = 'telegram' "
                "ORDER BY CASE WHEN enabled THEN 0 ELSE 1 END, id ASC LIMIT 1"
            )
        )
    ).first()

    bound = 0
    if row is not None:
        result = await conn.execute(
            text("UPDATE telegram_chats SET provider_id = :pid WHERE provider_id IS NULL").bindparams(pid=int(row[0]))
        )
        bound = result.rowcount or 0
        if bound:
            logger.info("m180: bound %d Telegram chat(s) to provider %d", bound, int(row[0]))

    result = await conn.execute(text("DELETE FROM telegram_chats WHERE provider_id IS NULL"))
    removed = result.rowcount or 0
    if removed:
        logger.warning(
            "m180: removed %d Telegram chat(s) that belonged to no bot — no telegram provider exists; "
            "a chat that writes to a bot again is registered afresh",
            removed,
        )
    return bound, removed
