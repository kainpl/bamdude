import os
from pathlib import Path

from pydantic_settings import BaseSettings

# Application version - single source of truth
APP_VERSION = "0.5.6a1"
GITHUB_REPO = "kainpl/bamdude"

# Bug-report relay endpoint. The relay holds the GitHub PAT and creates issues
# against ``GITHUB_REPO`` on behalf of users. Default points at the BamDude Cloud
# portal relay (cloud.bamdude.top); the old ``bamdude.top`` URL still answers via
# an nginx bridge for installs that predate this default, so nothing breaks on
# upgrade. Self-hosters can override to run their own (~50 LOC FastAPI forwarder)
# or set to empty string to disable the in-app bug-report UI.
BUG_REPORT_RELAY_URL = os.environ.get("BUG_REPORT_RELAY_URL", "https://cloud.bamdude.top/api/bug-report")

# Anonymized telemetry endpoint (opt-out). Sends a daily anonymized snapshot
# (version / platform / aggregate counts / feature flags) keyed by a random
# install id. Default points at the BamDude Cloud portal (cloud.bamdude.top); the
# old ``bamdude.top`` URL still answers via the nginx bridge for older installs.
# Override the URL to point at your own collector, or set
# ``TELEMETRY_DISABLED=true`` (or the in-app Settings toggle) to turn it off.
TELEMETRY_RELAY_URL = os.environ.get("TELEMETRY_RELAY_URL", "https://cloud.bamdude.top/api/telemetry")
TELEMETRY_DISABLED = os.environ.get("TELEMETRY_DISABLED", "").strip().lower() in ("1", "true", "yes")

# App directory - where the application is installed (for static files)
_app_dir = Path(__file__).resolve().parent.parent.parent.parent

# Data directory - for persistent data (database, archives)
# Use DATA_DIR env var if set (Docker/custom), otherwise use <project_root>/data
_data_dir_env = os.environ.get("DATA_DIR")
_data_dir = Path(_data_dir_env) if _data_dir_env else _app_dir / "data"

# Plate calibration directory - special handling to maintain backwards compatibility
# Docker: DATA_DIR/plate_calibration (e.g., /data/plate_calibration)
# Local dev: project_root/data/plate_calibration (original location)
_plate_cal_dir = Path(_data_dir_env) / "plate_calibration" if _data_dir_env else _app_dir / "data" / "plate_calibration"

# Log directory - use LOG_DIR env var if set, otherwise use app_dir/logs
_log_dir_env = os.environ.get("LOG_DIR")
_log_dir = Path(_log_dir_env) if _log_dir_env else _app_dir / "logs"


def _get_database_path() -> Path:
    """Return the path to bamdude.db (may not exist yet)."""
    return _data_dir / "bamdude.db"


# DATABASE_URL selects the storage backend — one variable, three states:
#   empty / unset            → SQLite at DATA_DIR/bamdude.db (the default)
#   "embedded"               → the bundled PostgreSQL from the embedded-postgres
#                              wheel, data under DATA_DIR/postgres/<major>
#   postgresql+asyncpg://…   → an external PostgreSQL server
# Anything else is refused here, at import, with a readable message — a bad
# value used to surface only as a connection error at first use.


def classify_database_url(raw: str | None) -> tuple[bool, str | None]:
    """Read DATABASE_URL into (embedded, external_url).

    ``(False, None)`` = SQLite, ``(True, None)`` = the bundled PostgreSQL,
    ``(False, url)`` = an external server. Anything else raises here so a typo
    is a startup error with a readable message, not a connection error later.
    """
    value = (raw or "").strip()
    if value.lower() in ("embedded", "embedded://"):
        return True, None
    if not value:
        return False, None
    if not value.startswith(("postgresql", "sqlite")):
        raise RuntimeError(
            "DATABASE_URL must be empty (SQLite), 'embedded' (bundled PostgreSQL) or a "
            f"postgresql+asyncpg:// URL; got a value starting with {value.split(':', 1)[0]!r}"
        )
    return False, value


_embedded_db, _external_db_url = classify_database_url(os.environ.get("DATABASE_URL"))

# Determine database path - only used for SQLite
_db_path = _get_database_path() if not (_external_db_url or _embedded_db) else None


def _embedded_pg_major() -> str:
    """Major of the bundled PostgreSQL — the data directory is versioned by it."""
    try:
        from importlib.metadata import version

        return version("embedded-postgres").split(".")[0]
    except Exception:  # noqa: BLE001 — the wheel is missing; start() reports it properly
        return "18"


def _embedded_pg_paths(data_dir: Path) -> tuple[Path, Path]:
    """(data directory of the cluster, password file) for the bundled server."""
    root = data_dir / "postgres"
    return root / _embedded_pg_major(), root / "password"


def _ensure_embedded_password(password_file: Path) -> str:
    """Create the server password once (URL-safe token, owner-only file)."""
    if password_file.exists():
        return password_file.read_text(encoding="utf-8").strip()
    import secrets

    password_file.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    return token


def _embedded_pg_port_for(data_dir: Path, pinned: int | None = None) -> int:
    """The bundled server's port: pinned by EMBEDDED_PG_PORT, else chosen once.

    Without the variable, the first start takes a free port and remembers it in
    ``DATA_DIR/postgres/port`` so it stays put across restarts — an advanced
    user finds it there next to the password file and connects with any client.
    Setting EMBEDDED_PG_PORT pins a known port (6432, say) for the same purpose;
    ``pinned`` is that value when it came through .env rather than the process.
    """
    if pinned:
        return int(pinned)
    from_env = (os.environ.get("EMBEDDED_PG_PORT") or "").strip()
    if from_env:
        return int(from_env)
    port_file = data_dir / "postgres" / "port"
    if port_file.exists():
        return int(port_file.read_text(encoding="utf-8").strip())
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    port_file.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(str(port) + chr(10), encoding="utf-8")
    return port


def _embedded_pg_url(data_dir: Path, port: int) -> str:
    _, password_file = _embedded_pg_paths(data_dir)
    password = _ensure_embedded_password(password_file)
    return f"postgresql+asyncpg://bamdude:{password}@127.0.0.1:{port}/bamdude"


_embedded_pg_port = _embedded_pg_port_for(_data_dir) if _embedded_db else 0


class Settings(BaseSettings):
    app_name: str = "BamDude"
    debug: bool = False  # Default to production mode

    # Paths - these accept env vars DATA_DIR, LOG_DIR etc.
    data_dir: Path = _data_dir
    log_dir: Path = _log_dir
    base_dir: Path = _data_dir  # For backwards compatibility (alias for data_dir)
    # Application install directory — where requirements.txt, the .git
    # tree, and frontend/ live. Distinct from data_dir on Docker (data is
    # a mounted volume, app is in the container's /app); on native
    # installs the two coincide as project_root + project_root/data.
    # Used by the in-app updater to run git / pip / npm against the
    # code tree rather than the data tree (#1240, etc.).
    app_dir: Path = _app_dir
    archive_dir: Path = _data_dir / "archive"
    plate_calibration_dir: Path = _plate_cal_dir  # Plate detection references
    static_dir: Path = _app_dir / "static"  # Static files are part of app, not data
    database_url: str = (
        _embedded_pg_url(_data_dir, _embedded_pg_port)
        if _embedded_db
        else (_external_db_url or f"sqlite+aiosqlite:///{_db_path}")
    )

    # Bundled PostgreSQL (DATABASE_URL=embedded) — see services/embedded_postgres.py.
    embedded_postgres: bool = _embedded_db
    embedded_pg_port: int = _embedded_pg_port
    embedded_pg_data_dir: Path | None = _embedded_pg_paths(_data_dir)[0] if _embedded_db else None
    embedded_pg_password_file: Path | None = _embedded_pg_paths(_data_dir)[1] if _embedded_db else None
    # max_connections of the bundled server; the pool (20 + 80) plus a margin.
    embedded_pg_max_connections: int = int(os.environ.get("EMBEDDED_PG_MAX_CONNECTIONS") or "120")
    # The bundled server is registered as its own OS service (the Windows
    # installer's BamDudePostgres, run by the SCM) rather than started as a child
    # of this process. BamDude then only connects — it never runs initdb, writes
    # the conf, starts or stops the server. Set by the installer in the service
    # environment; unset everywhere else (child-process lifecycle, the default).
    embedded_pg_external_service: bool = (os.environ.get("EMBEDDED_PG_EXTERNAL_SERVICE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    # Database connection-pool sizing. ``None`` = use the built-in, dialect-aware
    # default (PostgreSQL: pool_size 20 + max_overflow 80 + pre-ping + recycle
    # 1800 + LIFO; SQLite: 20 + 200). Large PostgreSQL printer farms can raise
    # these via the DB_POOL_SIZE / DB_MAX_OVERFLOW / DB_POOL_TIMEOUT /
    # DB_POOL_RECYCLE / DB_POOL_USE_LIFO env vars (issue #2572). Ensure PostgreSQL
    # ``max_connections`` comfortably exceeds (pool_size + max_overflow) x workers.
    db_pool_size: int | None = None
    db_max_overflow: int | None = None
    db_pool_timeout: int | None = None
    db_pool_recycle: int | None = None
    db_pool_use_lifo: bool | None = None

    # Explicit path to the ffmpeg executable, used for RTSP camera streaming on
    # the X1 / X2 / H2 / P2 series (the A1 / P1 chamber-image protocol needs no
    # ffmpeg). Optional — when unset, the app searches PATH + common install
    # locations. Set this (env FFMPEG_PATH) when ffmpeg IS installed but not on
    # the service's PATH — e.g. a fresh Windows winget install whose PATH change
    # hasn't reached an already-running shell/service yet.
    ffmpeg_path: str | None = None

    # Logging
    log_level: str = "INFO"  # Override with LOG_LEVEL env var (DEBUG, INFO, WARNING, ERROR)
    log_to_file: bool = True  # Set to false to disable file logging
    # log_retention_days lives in the DB-backed Settings (UI: Settings ->
    # Data Management). Module-load uses a hardcoded 7-day bootstrap;
    # lifespan startup reads the DB value and applies it via
    # ``logging_state.update_log_retention``.

    # API
    api_prefix: str = "/api/v1"

    # Slicer-API sidecar URLs (B.4 — server-side slicing). Per-install overrides
    # live in the settings table (``orcaslicer_api_url`` / ``bambu_studio_api_url``)
    # and take priority; these env defaults fire when the settings keys are
    # empty (the default state for fresh installs that haven't touched the
    # sidecar yet).
    slicer_api_url: str = "http://localhost:3003"
    bambu_studio_api_url: str = "http://localhost:3001"

    # Auth — sliding-session refresh cookie Secure attribute. ``None`` (default)
    # = auto-detect from request scheme / ``X-Forwarded-Proto``: if the user is
    # on HTTPS the cookie is Secure, if they're on plain HTTP (local LAN dev)
    # the cookie is not Secure so the browser actually stores it. Set True to
    # force Secure (paranoid; breaks LAN HTTP deployments). Set False to force
    # non-Secure (defeats MITM protection on real HTTPS deploys — dev only).
    auth_refresh_cookie_secure: bool | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    def model_post_init(self, __context):
        """Recalculate dependent paths after env vars are loaded."""
        # Resolve data_dir to absolute
        if not self.data_dir.is_absolute():
            object.__setattr__(self, "data_dir", Path.cwd() / self.data_dir)
        # Resolve log_dir to absolute
        if not self.log_dir.is_absolute():
            object.__setattr__(self, "log_dir", Path.cwd() / self.log_dir)
        # Recalculate paths derived from data_dir
        object.__setattr__(self, "base_dir", self.data_dir)
        object.__setattr__(self, "archive_dir", self.data_dir / "archive")
        # DATABASE_URL reaches us two ways: from the process environment, classified
        # at import above, or from .env, which pydantic pours into the field only
        # now — as the raw word "embedded" or a URL. Resolve from the field's final
        # value so both routes end in the same place.
        raw = (self.database_url or "").strip()
        embedded = self.embedded_postgres or classify_database_url(raw)[0]
        if embedded:
            pgdata, password_file = _embedded_pg_paths(self.data_dir)
            port = _embedded_pg_port_for(self.data_dir, pinned=self.embedded_pg_port or None)
            object.__setattr__(self, "embedded_postgres", True)
            object.__setattr__(self, "embedded_pg_data_dir", pgdata)
            object.__setattr__(self, "embedded_pg_password_file", password_file)
            object.__setattr__(self, "embedded_pg_port", port)
            object.__setattr__(self, "database_url", _embedded_pg_url(self.data_dir, port))
        elif not _external_db_url and (raw == "" or raw.startswith("sqlite")):
            # Our own SQLite default: follow data_dir now that it is absolute.
            # ``raw == ""`` covers an explicitly empty DATABASE_URL from the
            # environment (a bare ``DATABASE_URL=`` in .env / a systemd or Docker
            # env line) — pydantic pours that empty string into the field, and
            # without this it stayed empty and the engine failed to parse it.
            db_path = self.data_dir / "bamdude.db"
            object.__setattr__(self, "database_url", f"sqlite+aiosqlite:///{db_path}")


settings = Settings()

# Ensure directories exist
settings.archive_dir.mkdir(parents=True, exist_ok=True)
settings.plate_calibration_dir.mkdir(parents=True, exist_ok=True)
settings.static_dir.mkdir(exist_ok=True)
if settings.log_to_file:
    settings.log_dir.mkdir(exist_ok=True)
