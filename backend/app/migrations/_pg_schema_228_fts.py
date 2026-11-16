"""Frozen, guarded PostgreSQL archive-search repair shared with import conform.

Unlike m001, a correct component is not recreated, and a failed backfill must
abort the caller's transaction. No model metadata or default SQL evaluation.
"""

import re

from sqlalchemy import text

VECTOR = " || ".join(
    f"setweight(to_tsvector('simple', coalesce({column}, '')), '{weight}')"
    for column, weight in (
        ("print_name", "A"),
        ("filename", "B"),
        ("tags", "B"),
        ("designer", "C"),
        ("filament_type", "C"),
        ("notes", "D"),
    )
)
FUNCTION_BODY = f"BEGIN NEW.search_vector := {VECTOR.replace('coalesce(', 'coalesce(NEW.')}; RETURN NEW; END"


def _space(value):
    return re.sub(r"\s+", " ", value).strip()


async def ensure_archive_fts(conn, *, check_data=False):
    """Return after inspection if correct. Changed components require backfill."""
    dirty = False
    column = await conn.scalar(
        text("""
        SELECT format_type(a.atttypid,a.atttypmod) FROM pg_attribute a
        WHERE a.attrelid='public.print_archives'::regclass AND a.attname='search_vector'
          AND a.attnum>0 AND NOT a.attisdropped
    """)
    )
    if column is None:
        await conn.execute(text("ALTER TABLE public.print_archives ADD COLUMN search_vector tsvector"))
        dirty = True
    elif column != "tsvector":
        raise RuntimeError("m235: unexpected print_archives.search_vector type")

    function = (
        (
            await conn.execute(
                text("""
        SELECT p.oid, p.prosrc, l.lanname, p.provolatile::text AS provolatile, p.prosecdef, p.proconfig,
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
        await conn.execute(
            text(
                "CREATE OR REPLACE FUNCTION public.archive_search_vector_update() RETURNS trigger "
                f"AS $body${FUNCTION_BODY}$body$ LANGUAGE plpgsql VOLATILE SECURITY INVOKER"
            )
        )
        if function is not None and function["proconfig"] is not None:
            await conn.execute(text("ALTER FUNCTION public.archive_search_vector_update() RESET ALL"))
        dirty = True
    trigger = (
        (
            await conn.execute(
                text("""
        SELECT t.tgtype, t.tgenabled::text AS tgenabled, t.tgattr::text AS attrs, t.tgqual IS NULL AS no_when,
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
        and trigger["attrs"] == ""
        and trigger["no_when"]
        and trigger["no_args"]
        and trigger["correct_function"]
    ):
        if trigger is not None:
            await conn.execute(text("DROP TRIGGER archive_search_vector_trigger ON public.print_archives"))
        await conn.execute(
            text("""
            CREATE TRIGGER archive_search_vector_trigger BEFORE INSERT OR UPDATE ON public.print_archives
            FOR EACH ROW EXECUTE FUNCTION public.archive_search_vector_update()
        """)
        )
        dirty = True
    elif trigger["tgenabled"] != "O":
        await conn.execute(text("ALTER TABLE public.print_archives ENABLE TRIGGER archive_search_vector_trigger"))
        dirty = True

    index = (
        (
            await conn.execute(
                text("""
        SELECT pg_get_indexdef(i.indexrelid) AS definition, i.indisvalid, i.indisready
        FROM pg_index i WHERE i.indexrelid=to_regclass('public.idx_archives_search_vector')
    """)
            )
        )
        .mappings()
        .one_or_none()
    )
    expected = "CREATE INDEX idx_archives_search_vector ON public.print_archives USING gin (search_vector)"
    if index is not None and index["definition"] != expected:
        raise RuntimeError("m235: unexpected idx_archives_search_vector definition")
    if index is not None and (not index["indisvalid"] or not index["indisready"]):
        await conn.execute(text("DROP INDEX public.idx_archives_search_vector"))
        index = None
    if index is None:
        await conn.execute(text(expected))

    if dirty or check_data:
        # Even non-NULL vectors can be stale after a disabled trigger. An error
        # (including tsvector's size limit) escapes the savepoint and migration.
        async with conn.begin_nested():
            await conn.execute(
                text(
                    f"UPDATE public.print_archives SET search_vector = {VECTOR} "
                    f"WHERE search_vector IS DISTINCT FROM ({VECTOR})"
                )
            )
