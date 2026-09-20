"""Bundled PostgreSQL server for ``DATABASE_URL=embedded``.

The binaries come from the ``embedded-postgres`` wheel (our own build of
PostgreSQL + contrib + pgvector, one wheel per platform). This module is the
thin lifecycle layer around them: initialise the cluster once, start it before
the application opens its first connection, stop it after the engine is
disposed. It deliberately owns nothing else — the URL the engine uses is fixed
in ``core/config.py`` (127.0.0.1 + a configurable port + a generated password
file), so the engine can be created at import time as it always was, and the
server only has to be up before the first query.

Lifecycle rules (vault: «Вбудований PostgreSQL, який запускає сам BamDude»):

- **start**: if a postmaster already serves this data directory (an orphan of
  a BamDude that died), reuse it instead of starting a second one;
- **stop**: ``pg_ctl stop -m fast`` — a clean shutdown with a checkpoint,
  never ``immediate``;
- the data directory is versioned by the bundled PostgreSQL major
  (``DATA_DIR/postgres/18``): a wheel of another major must not touch it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from backend.app.core.config import settings

logger = logging.getLogger(__name__)

PG_USER = "bamdude"
PG_DATABASE = "bamdude"
PG_HOST = "127.0.0.1"

# Windows: every PostgreSQL tool runs on a console of its own, without a window.
# A console delivers Ctrl+C to EVERY process attached to it, so a server that
# shared BamDude's console took the operator's keypress at the same instant
# uvicorn did: the postmaster began a fast shutdown and the checkpointer, hit
# too, wrote the final checkpoint while backends were still being killed ("WAL
# was shut down unexpectedly", "abnormal database system shutdown") — all before
# the lifespan ever reached stop(). pg_ctl on Unix setsid()s the server away
# from the terminal for exactly this reason. Reproduced 2026-09-07 with
# GenerateConsoleCtrlEvent on a copy of the farm's data; the integration test
# with the console probe keeps it that way.
_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Written into the data directory and included from postgresql.conf, so our
# settings survive PostgreSQL's own file being regenerated and are re-applied on
# every start (a changed EMBEDDED_PG_PORT takes effect at the next start).
_CONF_NAME = "bamdude.conf"


class EmbeddedPostgresError(RuntimeError):
    """The bundled server could not be initialised, started or stopped."""


def _bin(name: str) -> Path:
    """Absolute path to one of the wheel's own binaries.

    name is never user input: every call site in this module passes a
    literal (``pg_ctl``, ``initdb``, ``pg_isready``, ``psql``, ``createdb``),
    and the parent comes from the installed package rather than from settings.
    """
    from embedded_postgres._commands import POSTGRES_BIN_PATH

    exe = f"{name}.exe" if sys.platform == "win32" else name
    path = Path(POSTGRES_BIN_PATH) / exe  # SEC-PATH-OK: literal name; parent is the wheel's own bin dir
    if not path.exists():
        raise EmbeddedPostgresError(f"embedded-postgres wheel has no {exe} at {path}")
    return path


def bundled_major() -> str:
    """Major version of the bundled PostgreSQL, from the wheel's own version."""
    from importlib.metadata import version

    return version("embedded-postgres").split(".")[0]


def _password() -> str:
    return settings.embedded_pg_password_file.read_text(encoding="utf-8").strip()


async def _run(*args: str, env: dict[str, str] | None = None, timeout: float = 120.0) -> tuple[int, str]:
    """Run one PostgreSQL tool, return (exit code, combined output).

    Output goes to a temporary file, never a pipe: ``pg_ctl start`` hands its
    standard streams to the postmaster it spawns, so a pipe only reaches EOF
    when the *server* exits — on Windows that turned a successful start into a
    90-second timeout. Waiting on the tool's exit and reading the file afterwards
    is immune to that (pgserver learned the same lesson).
    """
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    with tempfile.TemporaryFile(mode="w+b") as out_file:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out_file,
            stderr=asyncio.subprocess.STDOUT,
            env=full_env,
            creationflags=_CREATIONFLAGS,
        )
        try:
            code = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            raise EmbeddedPostgresError(f"timed out after {timeout:.0f}s: {' '.join(map(str, args))}") from None
        out_file.seek(0)
        out = out_file.read().decode("utf-8", errors="replace")
    return code or 0, out


def _pgdata() -> Path:
    return settings.embedded_pg_data_dir


def _log_file() -> Path:
    return _pgdata().parent / "postgres.log"


async def is_running() -> bool:
    """True when a postmaster serves our data directory (pg_ctl status)."""
    if not (_pgdata() / "PG_VERSION").exists():
        return False
    code, _ = await _run(str(_bin("pg_ctl")), "-D", str(_pgdata()), "status", timeout=30)
    return code == 0


async def _initdb() -> None:
    pgdata = _pgdata()
    pgdata.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Embedded PostgreSQL: initialising a new cluster in %s", pgdata)
    code, out = await _run(
        str(_bin("initdb")),
        "-D",
        str(pgdata),
        "-U",
        PG_USER,
        "--pwfile",
        str(settings.embedded_pg_password_file),
        "--auth=scram-sha-256",
        "--auth-local=scram-sha-256",
        "--encoding=UTF8",
        # Unicode-aware case mapping (Ukrainian ILIKE works) with code-point
        # ordering, no ICU, identical on every platform.
        "--locale-provider=builtin",
        "--builtin-locale=C.UTF-8",
        "--data-checksums",
        timeout=300,
    )
    if code != 0:
        raise EmbeddedPostgresError(f"initdb failed (exit {code}):\n{out}")
    # Our settings ride on an include so that they survive PostgreSQL's own
    # postgresql.conf being regenerated and can be rewritten on every start.
    with (pgdata / "postgresql.conf").open("a", encoding="utf-8") as conf:
        conf.write(f"\n# BamDude embedded server settings\ninclude = '{_CONF_NAME}'\n")


def _write_conf() -> None:
    pgdata = _pgdata()
    max_connections = max(120, settings.embedded_pg_max_connections)
    (pgdata / _CONF_NAME).write_text(
        "\n".join(
            [
                "# Written by BamDude on every start — do not edit, it is overwritten.",
                f"listen_addresses = '{PG_HOST}'",
                f"port = {settings.embedded_pg_port}",
                # TCP only, on every platform: the URL in core/config.py is the
                # same everywhere and nothing depends on a socket directory.
                "unix_socket_directories = ''",
                f"max_connections = {max_connections}",
                "shared_preload_libraries = 'pg_stat_statements'",
                # BamDude stores naive UTC; the server's own clock functions
                # (now(), log stamps) should agree. Needs the timezone database
                # the wheel ships since 18.6.2 (the Windows 18.6.0/18.6.1 wheels
                # had none and refused this line).
                "timezone = 'UTC'",
                "log_timezone = 'UTC'",
                "log_min_messages = warning",
                "",
            ]
        ),
        encoding="utf-8",
    )


async def _wait_ready() -> None:
    pg_isready = str(_bin("pg_isready"))
    for _ in range(60):
        code, _ = await _run(pg_isready, "-h", PG_HOST, "-p", str(settings.embedded_pg_port), timeout=15)
        if code == 0:
            return
        await asyncio.sleep(0.5)
    raise EmbeddedPostgresError(f"server did not accept connections on {PG_HOST}:{settings.embedded_pg_port}")


async def _psql(sql: str, database: str = "postgres") -> str:
    code, out = await _run(
        str(_bin("psql")),
        "-h",
        PG_HOST,
        "-p",
        str(settings.embedded_pg_port),
        "-U",
        PG_USER,
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-tAc",
        sql,
        env={"PGPASSWORD": _password()},
        timeout=60,
    )
    if code != 0:
        raise EmbeddedPostgresError(f"psql failed (exit {code}) for {sql!r}:\n{out}")
    return out.strip()


async def _ensure_database() -> None:
    if await _psql(f"SELECT 1 FROM pg_database WHERE datname = '{PG_DATABASE}'"):
        return
    logger.info("Embedded PostgreSQL: creating database %s", PG_DATABASE)
    code, out = await _run(
        str(_bin("createdb")),
        "-h",
        PG_HOST,
        "-p",
        str(settings.embedded_pg_port),
        "-U",
        PG_USER,
        "-E",
        "UTF8",
        PG_DATABASE,
        env={"PGPASSWORD": _password()},
    )
    if code != 0:
        raise EmbeddedPostgresError(f"createdb failed (exit {code}):\n{out}")


def _refuse_other_major() -> None:
    """A data directory of another PostgreSQL major must never be opened."""
    version_file = _pgdata() / "PG_VERSION"
    if not version_file.exists():
        return
    on_disk = version_file.read_text(encoding="utf-8").strip()
    bundled = bundled_major()
    if on_disk != bundled:
        raise EmbeddedPostgresError(
            f"the data directory {_pgdata()} was created by PostgreSQL {on_disk}, "
            f"but the bundled server is PostgreSQL {bundled}. Refusing to start: "
            "a major upgrade needs a migration step, not a silent open."
        )


async def _ensure_db_and_extension() -> None:
    await _ensure_database()
    await _psql("CREATE EXTENSION IF NOT EXISTS pg_stat_statements", database=PG_DATABASE)


async def start() -> None:
    """Initialise if needed, start unless already running, make sure the database exists."""
    if not settings.embedded_postgres:
        return
    pgdata = _pgdata()
    _refuse_other_major()

    if settings.embedded_pg_external_service:
        # The server runs as its own OS service (the Windows installer's
        # BamDudePostgres, ordered before us by the SCM through DependOnService).
        # We do not own its lifecycle: no initdb, no conf, no start, no stop —
        # only wait for it and make sure our database and extension exist.
        await _wait_ready()
        await _ensure_db_and_extension()
        logger.info(
            "Embedded PostgreSQL: using the externally-managed service on %s:%s (db %s)",
            PG_HOST,
            settings.embedded_pg_port,
            PG_DATABASE,
        )
        return

    fresh = not (pgdata / "PG_VERSION").exists()
    if fresh:
        await _initdb()
    _write_conf()

    if await is_running():
        listening = _running_port()
        if listening is not None and listening != settings.embedded_pg_port:
            # Started before the port changed (env var set, or the port file
            # moved with the data directory): a clean restart applies the conf
            # written a moment ago.
            logger.info(
                "Embedded PostgreSQL: the running server listens on %s but %s is configured — restarting it",
                listening,
                settings.embedded_pg_port,
            )
            await stop()
            await _start_server(pgdata)
        else:
            logger.info("Embedded PostgreSQL: a server already serves %s — reusing it", pgdata)
    else:
        await _start_server(pgdata)
    await _wait_ready()
    await _ensure_db_and_extension()
    logger.info(
        "Embedded PostgreSQL: ready (%s). Other clients: psql -h %s -p %s -U %s -d %s, password in %s",
        platform.machine(),
        PG_HOST,
        settings.embedded_pg_port,
        PG_USER,
        PG_DATABASE,
        settings.embedded_pg_password_file,
    )


def _running_port() -> int | None:
    """Port of the postmaster serving our data directory (line 4 of postmaster.pid)."""
    try:
        return int((_pgdata() / "postmaster.pid").read_text(encoding="utf-8").splitlines()[3])
    except (OSError, IndexError, ValueError):
        return None


async def _start_server(pgdata: Path) -> None:
    logger.info(
        "Embedded PostgreSQL %s: starting on %s:%s (data %s)",
        bundled_major(),
        PG_HOST,
        settings.embedded_pg_port,
        pgdata,
    )
    code, out = await _run(
        str(_bin("pg_ctl")),
        "-D",
        str(pgdata),
        "-l",
        str(_log_file()),
        "-w",
        "-t",
        "60",
        "start",
        timeout=90,
    )
    if code != 0:
        tail = ""
        if _log_file().exists():
            tail = "\n".join(_log_file().read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
        raise EmbeddedPostgresError(f"pg_ctl start failed (exit {code}):\n{out}\n--- {_log_file()} ---\n{tail}")


async def stop() -> None:
    """Clean shutdown with a checkpoint. Never ``immediate``."""
    if not settings.embedded_postgres:
        return
    if settings.embedded_pg_external_service:
        # The SCM owns the BamDudePostgres service; stopping it here would fight
        # the service manager and leave it in a confused state.
        return
    if not await is_running():
        return
    logger.info("Embedded PostgreSQL: stopping (fast)")
    code, out = await _run(
        str(_bin("pg_ctl")), "-D", str(_pgdata()), "-m", "fast", "-t", "60", "-w", "stop", timeout=90
    )
    if code != 0:
        logger.error("Embedded PostgreSQL: pg_ctl stop failed (exit %s):\n%s", code, out)
