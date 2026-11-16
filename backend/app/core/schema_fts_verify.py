"""Read-only proof of B's frozen FTS objects; never repairs during verification."""

from sqlalchemy import text

from backend.app.core.schema_repair_state import SchemaRepairPending
from backend.app.migrations._pg_schema_228_fts import FUNCTION_BODY, _space


async def verify_archive_fts(conn):
    function = (
        (
            await conn.execute(
                text("""
        SELECT p.prosrc,l.lanname,p.provolatile::text,p.prosecdef,p.proconfig,
               p.prorettype='trigger'::regtype AS returns_trigger
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname='public' AND p.proname='archive_search_vector_update' AND p.pronargs=0
    """)
            )
        )
        .mappings()
        .one_or_none()
    )
    if function is None or not (
        _space(function["prosrc"]) == _space(FUNCTION_BODY)
        and function["lanname"] == "plpgsql"
        and function["provolatile"] == "v"
        and not function["prosecdef"]
        and function["proconfig"] is None
        and function["returns_trigger"]
    ):
        raise SchemaRepairPending("unverified_fts_function")
    trigger = (
        (
            await conn.execute(
                text("""
        SELECT t.tgtype,t.tgenabled::text,t.tgattr::text AS attrs,t.tgqual IS NULL AS no_when,
               octet_length(t.tgargs)=0 AS no_args,
               t.tgfoid='public.archive_search_vector_update()'::regprocedure AS correct_function
        FROM pg_trigger t WHERE t.tgrelid='public.print_archives'::regclass
          AND t.tgname='archive_search_vector_trigger' AND NOT t.tgisinternal
    """)
            )
        )
        .mappings()
        .one_or_none()
    )
    if trigger is None or not (
        trigger["tgtype"] == 23
        and trigger["tgenabled"] == "O"
        and trigger["attrs"] == ""
        and trigger["no_when"]
        and trigger["no_args"]
        and trigger["correct_function"]
    ):
        raise SchemaRepairPending("unverified_fts_trigger")
