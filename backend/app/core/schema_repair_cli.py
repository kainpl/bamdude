"""Offline recovery entry point. Status imports no application configuration.

Resolving normal config can create an embedded password/port and directories.
The read-only command instead reads existing environment/.env and connection
files. Neither command starts a farm, cluster, migration bootstrap or server.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import make_url, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.schema_repair_state import DEBT, RESIDUALS, SchemaRepairPending, read_record
from backend.app.services.render_runtime import _try_lock, _unlock


def configuration():
    values = {}
    if not os.environ.get("BAMDUDE_IGNORE_DOTENV"):
        from dotenv import dotenv_values

        values.update({k.upper(): v for k, v in dotenv_values(".env").items() if v is not None})
    values.update({k.upper(): v for k, v in os.environ.items()})
    root = Path(values.get("DATA_DIR") or Path(__file__).resolve().parents[3] / "data").resolve()
    raw = (values.get("DATABASE_URL") or "").strip()
    if raw.lower() in ("embedded", "embedded://"):
        password = (root / "postgres/password").read_text(encoding="utf-8").strip()
        port = int(values.get("EMBEDDED_PG_PORT") or (root / "postgres/port").read_text(encoding="utf-8").strip())
        if not password or not 1 <= port <= 65535:
            raise ValueError("Embedded connection is unavailable")
        url = URL.create(
            "postgresql+asyncpg", username="bamdude", password=password, host="127.0.0.1", port=port, database="bamdude"
        )
    else:
        url = make_url(raw or f"sqlite+aiosqlite:///{root / 'bamdude.db'}")
        if url.get_backend_name() == "postgresql":
            url = url.set(drivername="postgresql+asyncpg")
        elif url.get_backend_name() == "sqlite":
            url = url.set(drivername="sqlite+aiosqlite")
        else:
            raise ValueError("Unsupported connection")
    return root, url


@contextmanager
def exclusive(root, *, readonly):
    path = root / "runtime-state/farm-startup/owner.lock"
    for parent in (root, path.parent.parent, path.parent, path):
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("Installation lock must not follow links")
    handle = None
    locked = False
    try:
        if not readonly:
            path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or not readonly:
            handle = path.open("r+b" if readonly else "a+b")
            locked = _try_lock(handle)
            if not locked:
                raise ValueError("Stop BamDude before using schema_repair")
        yield
    finally:
        if locked:
            _unlock(handle)
        if handle is not None:
            handle.close()


async def status(url):
    source = None
    if url.get_backend_name() == "sqlite":
        from backend.app.core.sqlite_import_source import PreparedSqliteSource

        path = Path(url.database)
        if not path.is_file():
            print(json.dumps({"debt": False, "database_present": False}))
            return 0
        source = PreparedSqliteSource(path)
        try:
            await asyncio.to_thread(source.copy)
        except BaseException:
            source.close()
            raise
        url = make_url(f"sqlite+aiosqlite:///{source.path}")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                await conn.execute(text("SET TRANSACTION READ ONLY"))
            debt = await read_record(conn, DEBT)
            residuals = await read_record(conn, RESIDUALS)
            remaining = debt.get("automatic_attempts_remaining") if debt else None
            if remaining is not None and (type(remaining) is not int or not 0 <= remaining <= 2):
                raise SchemaRepairPending("invalid_debt")
            reason = debt.get("last_failure") if debt else None
            if reason is not None and (not isinstance(reason, str) or not re.fullmatch("[a-z_]{1,64}", reason)):
                raise SchemaRepairPending("invalid_debt")
            entries = residuals.get("entries", {}) if residuals else {}
            if not isinstance(entries, dict):
                raise SchemaRepairPending("invalid_residuals")
            print(
                json.dumps(
                    {
                        "debt": debt is not None,
                        "automatic_attempts_remaining": remaining,
                        "last_failure": reason,
                        "residual_objects": sorted(entries),
                    }
                )
            )
        return 0
    finally:
        await engine.dispose()
        if source is not None:
            source.close()


async def retry(url):
    if url.get_backend_name() == "sqlite" and not Path(url.database).is_file():
        print("Database not found; no repair performed.", file=sys.stderr)
        return 2
    # Only the mutating, exclusively owned retry imports model registration.
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.core import case_folding, database
    from backend.app.core.config import settings
    from backend.app.core.database import Base, import_all_models
    from backend.app.core.schema_repair import claim, retry_residuals, run_claimed
    from backend.app.core.schema_repair_state import fail_attempt
    from backend.app.migrations import _run_pending

    import_all_models()
    # Keep production UTC, SQLite connection pragmas and listeners. Status
    # deliberately never imports this module or constructs this engine.
    engine = database.engine
    debt = None
    owned_pg = None
    try:
        if engine.url != url:
            raise SchemaRepairPending("connection_configuration_mismatch")
        if settings.embedded_postgres and not settings.embedded_pg_external_service:
            from backend.app.services import embedded_postgres

            # Recovery may restart this existing child cluster, never init a
            # guessed new one. A separately running server stays untouched.
            if not (settings.embedded_pg_data_dir / "PG_VERSION").is_file():
                raise SchemaRepairPending("existing_cluster_required")
            if not await embedded_postgres.is_running():
                owned_pg = embedded_postgres
                await owned_pg.start()
        if engine.dialect.name == "postgresql":
            await case_folding.probe_postgres_case_folding(engine)
        debt = await claim(engine, Base.metadata, automatic=False)
        if debt is not None:
            await _run_pending(engine, async_sessionmaker(engine, expire_on_commit=False))
            await run_claimed(engine, Base.metadata, debt)
        await retry_residuals(engine, Base.metadata)
        print("Schema repair verified. Restart BamDude normally.")
        return 0
    except Exception:
        if debt is not None:
            try:
                async with engine.begin() as conn:
                    await fail_attempt(conn, debt, "manual_retry_failed")
            except Exception:
                pass  # Never replace/clear a newer owner's debt to report ours.
        print("Schema repair remains pending. Correct the external cause, then retry.", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()
        if owned_pg is not None:
            await owned_pg.stop()
            if await owned_pg.is_running():
                raise SchemaRepairPending("owned_cluster_stop_unproven")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m backend.app.cli schema_repair")
    parser.add_argument("operation", choices=("status", "retry"))
    args = parser.parse_args(argv)
    try:
        root, url = configuration()
        with exclusive(root, readonly=args.operation == "status"):
            return asyncio.run(status(url) if args.operation == "status" else retry(url))
    except Exception:
        # Never print a URL, password, row value or driver's raw exception.
        print(
            "Schema repair unavailable. Stop BamDude and check the existing connection and DATA_DIR.", file=sys.stderr
        )
        return 2
