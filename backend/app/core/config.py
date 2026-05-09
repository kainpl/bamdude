import os
from pathlib import Path

from pydantic_settings import BaseSettings

# Application version - single source of truth
APP_VERSION = "0.4.4b1"
GITHUB_REPO = "kainpl/bamdude"

# Bug-report relay endpoint. The relay holds the GitHub PAT and creates issues
# against ``GITHUB_REPO`` on behalf of users. Default points at the bamdude.top
# landing-site relay; self-hosters can override to run their own (~50 LOC FastAPI
# forwarder) or set to empty string to disable the in-app bug-report UI.
BUG_REPORT_RELAY_URL = os.environ.get("BUG_REPORT_RELAY_URL", "https://bamdude.top/api/bug-report")

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


# External DATABASE_URL takes priority (PostgreSQL support)
_external_db_url = os.environ.get("DATABASE_URL")

# Determine database path - only used for SQLite
_db_path = _get_database_path() if not _external_db_url else None


class Settings(BaseSettings):
    app_name: str = "BamDude"
    debug: bool = False  # Default to production mode

    # Paths - these accept env vars DATA_DIR, LOG_DIR etc.
    data_dir: Path = _data_dir
    log_dir: Path = _log_dir
    base_dir: Path = _data_dir  # For backwards compatibility (alias for data_dir)
    archive_dir: Path = _data_dir / "archive"
    plate_calibration_dir: Path = _plate_cal_dir  # Plate detection references
    static_dir: Path = _app_dir / "static"  # Static files are part of app, not data
    database_url: str = _external_db_url or f"sqlite+aiosqlite:///{_db_path}"

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
        # Recalculate database_url only for SQLite (don't overwrite external DATABASE_URL)
        if not _external_db_url:
            db_path = self.data_dir / "bamdude.db"
            object.__setattr__(self, "database_url", f"sqlite+aiosqlite:///{db_path}")


settings = Settings()

# Ensure directories exist
settings.archive_dir.mkdir(parents=True, exist_ok=True)
settings.plate_calibration_dir.mkdir(parents=True, exist_ok=True)
settings.static_dir.mkdir(exist_ok=True)
if settings.log_to_file:
    settings.log_dir.mkdir(exist_ok=True)
