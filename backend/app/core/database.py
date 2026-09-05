import asyncio
import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

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


# Resolved connection-pool configuration, captured at engine creation so
# /system/db-pool can report it without re-deriving the dialect defaults.
_pool_config: dict = {}


def _resolve_pool_kwargs() -> dict:
    """Build the pool kwargs for ``create_async_engine`` (issue #2572).

    Dialect-aware defaults, each overridable via env (``DB_POOL_SIZE`` etc.):
      - PostgreSQL: pool_size 20 + max_overflow 80, ``pool_pre_ping`` (recover
        server-dropped connections instead of erroring the request),
        ``pool_recycle`` 1800s, and ``pool_use_lifo`` (keep a small hot set on a
        bursty farm). The old hard-coded 10 + 20 exhausted on large farms while
        printer callbacks held connections.
      - SQLite: pool_size 20 + max_overflow 200 (unchanged); no pre-ping /
        recycle / lifo — the connection is a local file, not a server socket.
    """
    if is_sqlite():
        pool_size = settings.db_pool_size if settings.db_pool_size is not None else 20
        max_overflow = settings.db_max_overflow if settings.db_max_overflow is not None else 200
        kwargs = {"pool_size": pool_size, "max_overflow": max_overflow}
    else:
        pool_size = settings.db_pool_size if settings.db_pool_size is not None else 20
        max_overflow = settings.db_max_overflow if settings.db_max_overflow is not None else 80
        kwargs = {
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_pre_ping": True,
            "pool_recycle": settings.db_pool_recycle if settings.db_pool_recycle is not None else 1800,
            "pool_use_lifo": settings.db_pool_use_lifo if settings.db_pool_use_lifo is not None else True,
        }
    if settings.db_pool_timeout is not None:
        kwargs["pool_timeout"] = settings.db_pool_timeout
    return kwargs


def _create_engine():
    """Create the async engine with dialect-appropriate settings."""
    kwargs = _resolve_pool_kwargs()

    global _pool_config
    _pool_config = {
        "pool_size": kwargs["pool_size"],
        "max_overflow": kwargs["max_overflow"],
        # SQLAlchemy's own defaults when we don't pass the kwarg.
        "pool_timeout": kwargs.get("pool_timeout", 30),
        "pool_recycle": kwargs.get("pool_recycle", -1),
        "pool_pre_ping": kwargs.get("pool_pre_ping", False),
        "pool_use_lifo": kwargs.get("pool_use_lifo", False),
    }

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


def get_pool_status() -> dict:
    """Snapshot the DB connection pool for diagnostics (issue #2572).

    Returns the resolved configuration plus live gauges (checked-out /
    checked-in / overflow). Reads the pool's own counters — it does NOT check
    out a connection, so it stays truthful even when the pool is exhausted.
    Gauges a given pool implementation doesn't expose come back as ``None``
    rather than raising.
    """
    pool = engine.sync_engine.pool
    gauges: dict = {}
    for key, method_name in (
        ("current_size", "size"),
        ("checked_out", "checkedout"),
        ("checked_in", "checkedin"),
        ("overflow", "overflow"),
    ):
        method = getattr(pool, method_name, None)
        try:
            gauges[key] = method() if callable(method) else None
        except Exception:
            # A gauge should never take down the diagnostics endpoint.
            gauges[key] = None
    return {
        "dialect": "sqlite" if is_sqlite() else "postgresql",
        "config": dict(_pool_config),
        **gauges,
    }


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


@event.listens_for(Session, "before_flush")
def _recompute_archive_time_metrics(session, flush_context, instances):
    """Keep ``PrintArchive.actual_time_seconds`` / ``time_accuracy`` in sync with the
    row's timestamps + status on every flush, across all write paths (completion,
    cleanup-closure, dispatch finalize, attach_3mf, manual create, re-parse). Pure
    arithmetic on each dirty/new archive's own attributes — no queries, no re-loop."""
    from backend.app.models.archive import PrintArchive  # lazy: avoid models↔database cycle
    from backend.app.services.archive_time_metrics import compute_time_metrics

    for obj in (*session.new, *session.dirty):
        if isinstance(obj, PrintArchive):
            obj.actual_time_seconds, obj.time_accuracy = compute_time_metrics(
                obj.started_at, obj.completed_at, obj.status, obj.print_time_seconds
            )


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


def import_all_models() -> None:
    """Import every module under ``backend.app.models`` so its tables land on ``Base.metadata``.

    ``Base.metadata.create_all`` emits DDL only for tables the mapper has
    actually seen, and a mapper sees a table only once the module defining it
    has been imported. A model nobody imports is therefore invisible: the
    application boots, the ORM statement is valid Python, and the database
    answers "no such table" at the first query.

    The list lives here and is called from two places — ``init_db`` for the
    application and ``tests/conftest.py::test_engine`` for the test database —
    because it used to be written out twice. The copies drifted: ``hms_mute``
    (m163) was missing from the test one and surfaced only when the
    printer-delete cascade grew to touch it, and ``printer_tag`` reached the
    test metadata by accident, through a runtime import inside
    ``models/printer.py``.

    ``tests/unit/test_every_model_module_is_registered.py`` compares the names
    below against the directory listing, so a new model module fails a test
    rather than a query.

    The imports are inside the function on purpose: every model module imports
    ``Base`` from this one, so importing them at module scope would be a cycle.
    """
    from backend.app.models import (  # noqa: F401
        active_print_session,
        active_print_spoolman,
        ams_history,
        ams_label,
        ams_setting_audit,
        api_key,
        archive,
        archive_part,
        auth_ephemeral,
        auto_queue,
        bug_report,
        calibration_audit,
        calibration_session,
        cloud_link,
        color_catalog,
        customer,
        external_link,
        filament_calibration,
        filament_sku_settings,
        firmware,
        git_backup,
        group,
        hms_mute,
        kprofile_note,
        label_device,
        label_template,
        library,
        library_file_makerworld_meta,
        library_file_note,
        library_scan,
        local_preset,
        location,
        long_lived_token,
        macro,
        maintenance,
        notification,
        notification_template,
        oidc_provider,
        orca_base_cache,
        part_stock,
        print_options_preference,
        print_queue,
        print_usage_event,
        printer,
        printer_location,
        printer_queue,
        printer_sensor_history,
        printer_setting_audit,
        printer_tag,
        product,
        project,
        project_line,
        settings,
        shopping_list,
        slicer_pipeline,
        smart_plug,
        smart_plug_energy_snapshot,
        smart_plug_power_history,
        smart_sensor,
        smart_sensor_history,
        smart_sensor_threshold,
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
        user_filament,
        user_otp_code,
        user_totp,
        virtual_printer,
        zigbee_device,
    )


async def init_db():
    """Initialize the database: create tables, run migrations, seed data."""
    from backend.app.migrations import run_all_migrations

    # Register every model on Base.metadata before create_all — see the function.
    import_all_models()

    await run_all_migrations(engine, async_session)

    # Boot self-check: fail loudly if the tri-state calibration columns are
    # missing rather than serving a drifted schema (see the function docstring).
    await _verify_calibration_mode_columns(engine)

    # Re-encrypt any legacy plaintext OIDC client_secret / TOTP secret rows
    # that exist from before the encryption key was configured. Runs on a
    # fresh AsyncSession (NOT the migration connection) to avoid SQLite WAL
    # writer contention.
    await _migrate_encrypt_legacy_secrets()


async def _verify_calibration_mode_columns(engine) -> None:
    """Boot self-check: assert the tri-state calibration ``*_mode`` columns
    exist on ``print_queue`` after migrations have run.

    Guardrail against the ORM↔DB schema drift that broke production once: the
    moment ``PrintQueueItem`` maps these columns, SQLAlchemy names them in
    EVERY ``select(PrintQueueItem)``; if the running DB lacks them (a migration
    that never applied — e.g. a copied DB, or a ``--reload`` window before
    ``init_db`` finished), the failure cascades across dispatch, the completion
    handler, notifications and ``GET /queue`` as a mid-request 500. Surfacing it
    here converts that silent catastrophe into a loud, obvious startup failure.

    ``getattr`` fallbacks do NOT help — the fault is at the SQL SELECT layer,
    before any Python attribute access — which is exactly why this check exists.
    """
    from backend.app.migrations.helpers import column_exists

    required = ("bed_levelling_mode", "flow_cali_mode", "nozzle_offset_cali_mode")
    async with engine.begin() as conn:
        missing = [c for c in required if not await column_exists(conn, "print_queue", c)]
    if missing:
        raise RuntimeError(
            "Startup aborted: print_queue is missing calibration-mode column(s) "
            f"{missing}. Migration m106 (print_queue_calibration_modes) did not apply. "
            "Refusing to serve with a drifted schema — inspect the _migrations table "
            "and re-run migrations before starting."
        )


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
