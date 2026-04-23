from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.app.core.config import settings
from backend.app.core.db_dialect import is_sqlite


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Set SQLite pragmas on each new connection for concurrency and performance."""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.execute("PRAGMA busy_timeout = 15000")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()


def _strip_tz_from_params(conn, cursor, statement, parameters, context, executemany):
    """Strip timezone info from aware datetimes before they reach asyncpg.

    asyncpg rejects timezone-aware values for TIMESTAMP WITHOUT TIME ZONE columns.
    The codebase uses datetime.now(timezone.utc) in many places - this makes
    Postgres behave like SQLite which ignores timezone info entirely.

    Recursive: SQLAlchemy passes parameters in several shapes depending on the
    path - a dict for named binds, a tuple for positional, a list of dicts/tuples
    for executemany, and for insertmanyvalues sometimes a list of tuples inside
    an outer list. Strip datetimes at any depth (upstream #941 follow-up).
    """
    import datetime

    if parameters is None:
        return statement, parameters

    def _strip(val):
        if isinstance(val, datetime.datetime) and val.tzinfo is not None:
            return val.replace(tzinfo=None)
        if isinstance(val, dict):
            return {k: _strip(v) for k, v in val.items()}
        if isinstance(val, list):
            return [_strip(v) for v in val]
        if isinstance(val, tuple):
            return tuple(_strip(v) for v in val)
        return val

    return statement, _strip(parameters)


def _create_engine():
    """Create the async engine with dialect-appropriate settings."""
    if is_sqlite():
        kwargs = {"pool_size": 20, "max_overflow": 200}
    else:
        kwargs = {"pool_size": 10, "max_overflow": 20}
    eng = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        **kwargs,
    )
    if is_sqlite():
        event.listen(eng.sync_engine, "connect", _set_sqlite_pragmas)
    else:
        event.listen(eng.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    return eng


engine = _create_engine()

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def close_all_connections():
    """Close all database connections for backup/restore operations."""
    global engine
    await engine.dispose()


async def reinitialize_database():
    """Reinitialize database connection after restore."""
    global engine, async_session
    engine = _create_engine()
    async_session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Initialize the database: create tables, run migrations, seed data."""
    # Import models to register them with SQLAlchemy
    from backend.app.migrations import run_all_migrations
    from backend.app.models import (  # noqa: F401
        active_print_spoolman,
        ams_history,
        ams_label,
        api_key,
        archive,
        auth_ephemeral,
        color_catalog,
        external_link,
        git_backup,
        group,
        kprofile_note,
        library,
        library_file_note,
        local_preset,
        macro,
        maintenance,
        notification,
        notification_template,
        oidc_provider,
        orca_base_cache,
        pending_upload,
        print_queue,
        printer,
        printer_queue,
        project,
        project_bom,
        project_print_plan,
        settings,
        slot_preset,
        smart_plug,
        smart_plug_energy_snapshot,
        spool,
        spool_assignment,
        spool_catalog,
        spool_k_profile,
        spool_usage_history,
        telegram_chat,
        user,
        user_email_pref,
        user_otp_code,
        user_totp,
        virtual_printer,
    )

    await run_all_migrations(engine, async_session)
