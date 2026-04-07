from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.app.core.config import settings


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    """Set SQLite pragmas on each new connection for concurrency and performance."""
    cursor = dbapi_conn.cursor()
    # WAL mode allows concurrent readers + one writer (vs default DELETE mode which locks entirely)
    cursor.execute("PRAGMA journal_mode = WAL")
    # Wait up to 5 seconds when the database is locked instead of failing immediately
    cursor.execute("PRAGMA busy_timeout = 15000")
    cursor.execute("PRAGMA synchronous = NORMAL")
    cursor.close()


engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=20,
    max_overflow=200,
)

# Register the pragma listener on the underlying sync engine
event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)

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
    engine = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_size=20,
        max_overflow=200,
    )
    event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
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
        color_catalog,
        external_link,
        filament,
        github_backup,
        group,
        kprofile_note,
        library,
        local_preset,
        macro,
        maintenance,
        notification,
        notification_template,
        orca_base_cache,
        pending_upload,
        print_log,
        print_queue,
        printer,
        printer_queue,
        project,
        project_bom,
        settings,
        slot_preset,
        smart_plug,
        spool,
        spool_assignment,
        spool_catalog,
        spool_k_profile,
        spool_usage_history,
        spoolbuddy_device,
        telegram_chat,
        user,
        user_email_pref,
        virtual_printer,
    )

    await run_all_migrations(engine, async_session)

