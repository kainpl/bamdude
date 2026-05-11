import asyncio
import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.app.core.config import settings
from backend.app.core.db_dialect import is_sqlite

logger = logging.getLogger(__name__)


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
        except BaseException:
            # Catch BaseException (not just Exception) so CancelledError —
            # raised when Starlette's BaseHTTPMiddleware cancels the inner
            # task scope on client disconnect — also triggers rollback.
            # `asyncio.shield` keeps the rollback running to completion even
            # when the await itself gets cancelled by the same scope, so the
            # SQLite write lock is released promptly instead of being held
            # until the connection is GC'd ages later. On Postgres the same
            # leak shape would surface as "QueuePool limit … overflow"
            # instead of "database is locked" (#1112 follow-up).
            try:
                await asyncio.shield(session.rollback())
            except BaseException:  # noqa: BLE001 — rollback failure must not mask the original
                pass
            raise
        finally:
            try:
                await asyncio.shield(session.close())
            except BaseException:  # noqa: BLE001 — close failure must not mask the original
                pass


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
        auto_queue,
        bug_report,
        color_catalog,
        external_link,
        filament_sku_settings,
        git_backup,
        group,
        kprofile_note,
        library,
        library_file_makerworld_meta,
        library_file_note,
        library_project_links,
        local_preset,
        long_lived_token,
        macro,
        maintenance,
        notification,
        notification_template,
        oidc_provider,
        orca_base_cache,
        print_options_preference,
        print_queue,
        printer,
        printer_queue,
        project,
        project_bom,
        project_print_plan,
        settings,
        shopping_list,
        slot_preset,
        smart_plug,
        smart_plug_energy_snapshot,
        spool,
        spool_assignment,
        spool_catalog,
        spool_k_profile,
        spool_usage_history,
        spoolman_k_profile,
        spoolman_slot_assignment,
        telegram_chat,
        user,
        user_email_pref,
        user_otp_code,
        user_totp,
        virtual_printer,
    )

    await run_all_migrations(engine, async_session)

    # Re-encrypt any legacy plaintext OIDC client_secret / TOTP secret rows
    # that exist from before the encryption key was configured. Runs on a
    # fresh AsyncSession (NOT the migration connection) to avoid SQLite WAL
    # writer contention.
    await _migrate_encrypt_legacy_secrets()


# Module-level counter exposing the number of rows skipped during the last
# _migrate_encrypt_legacy_secrets() invocation. Surfaced via /encryption-status
# (migration_error_count) so operators can spot poison rows that need attention.
_migration_error_count: int = 0


def get_migration_error_count() -> int:
    """Return the number of rows that failed to re-encrypt during the last
    _migrate_encrypt_legacy_secrets() run."""
    return _migration_error_count


async def _migrate_encrypt_legacy_secrets() -> None:
    """Re-encrypt OIDC ``client_secret`` and TOTP ``secret`` rows that are still
    stored as plaintext (no ``fernet:`` prefix).

    Called from :func:`init_db` after migrations finish. No-ops when no
    encryption key is configured (so plaintext storage stays the legacy
    behaviour for installs without a key).

    Per-row strategy — each row is committed in its own AsyncSession so a
    single corrupt row does NOT block other successful re-encryptions on
    every startup forever. The skipped-row count is exposed via
    :func:`get_migration_error_count` and surfaced on /encryption-status.

    Read-phase failures are startup-fatal — re-raise so operators see the
    problem instead of silent data corruption.

    Idempotent: rows that already start with ``fernet:`` are skipped, and the
    write-phase re-checks the prefix before encrypting (guards against double
    encryption from concurrent workers).
    """
    from sqlalchemy import not_, select

    from backend.app.core.encryption import is_encryption_active
    from backend.app.models.oidc_provider import OIDCProvider
    from backend.app.models.user_totp import UserTOTP

    global _migration_error_count

    if not is_encryption_active():
        # Reset stale counter from a previous active-key run — we no longer
        # have any rows to migrate, so the count must not leak across runs.
        _migration_error_count = 0
        return

    # Phase 1 (read): collect (id, stored_value) tuples for plaintext rows.
    # Read-phase failures are startup-fatal — re-raise.
    try:
        async with async_session() as ro:
            oidc_rows = await ro.execute(
                select(OIDCProvider.id, OIDCProvider._client_secret_enc).where(
                    not_(OIDCProvider._client_secret_enc.like("fernet:%"))
                )
            )
            oidc_candidates = [(r[0], r[1]) for r in oidc_rows.all()]
            totp_rows = await ro.execute(
                select(UserTOTP.id, UserTOTP._secret_enc).where(not_(UserTOTP._secret_enc.like("fernet:%")))
            )
            totp_candidates = [(r[0], r[1]) for r in totp_rows.all()]
    except Exception:
        logger.error("_migrate_encrypt_legacy_secrets: phase 1 read failed", exc_info=True)
        raise

    oidc_count = totp_count = error_count = 0

    # Phase 2 (write): each row in its own AsyncSession + transaction.
    # Failure of one row does NOT block the others.
    for oidc_id, stored in oidc_candidates:
        if not stored:
            continue  # defensive: skip empty strings
        try:
            async with async_session() as wr:
                provider = await wr.get(OIDCProvider, oidc_id)
                if provider is None:
                    continue  # row deleted between phase 1 and phase 2
                # Idempotent guard: re-check inside the write session in case
                # a concurrent worker beat us to it.
                if not provider._client_secret_enc.startswith("fernet:"):
                    provider.client_secret = stored  # setter -> mfa_encrypt
                    await wr.commit()
                    oidc_count += 1
        except Exception:
            logger.error(
                "Failed to re-encrypt OIDCProvider id=%s — skipping",
                oidc_id,
                exc_info=True,
            )
            error_count += 1

    for totp_id, stored in totp_candidates:
        if not stored:
            continue
        try:
            async with async_session() as wr:
                totp = await wr.get(UserTOTP, totp_id)
                if totp is None:
                    continue
                if not totp._secret_enc.startswith("fernet:"):
                    totp.secret = stored
                    await wr.commit()
                    totp_count += 1
        except Exception:
            logger.error(
                "Failed to re-encrypt UserTOTP id=%s — skipping",
                totp_id,
                exc_info=True,
            )
            error_count += 1

    _migration_error_count = error_count
    if oidc_count or totp_count:
        logger.info(
            "Re-encrypted legacy plaintext secrets: %d OIDC client_secret(s), %d TOTP secret(s)",
            oidc_count,
            totp_count,
        )
    elif error_count == 0:
        logger.debug("_migrate_encrypt_legacy_secrets: no rows needed re-encryption")
    if error_count:
        logger.error(
            "_migrate_encrypt_legacy_secrets: %d row(s) skipped due to errors. "
            "See /api/v1/auth/encryption-status (migration_error_count).",
            error_count,
        )
