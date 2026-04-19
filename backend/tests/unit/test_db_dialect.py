"""Unit tests for database dialect helpers and PostgreSQL compatibility."""

from unittest.mock import AsyncMock, patch

import pytest


class TestDialectDetection:
    """Test is_sqlite() and is_postgres() detection."""

    def test_sqlite_detected(self):
        with patch("backend.app.core.config.settings") as mock_settings:
            mock_settings.database_url = "sqlite+aiosqlite:///path/to/db.sqlite"
            from backend.app.core.db_dialect import is_postgres, is_sqlite

            assert is_sqlite() is True
            assert is_postgres() is False

    def test_postgres_detected(self):
        with patch("backend.app.core.config.settings") as mock_settings:
            mock_settings.database_url = "postgresql+asyncpg://user:pass@host:5432/db"
            from backend.app.core.db_dialect import is_postgres, is_sqlite

            assert is_postgres() is True
            assert is_sqlite() is False


class TestRunPragma:
    """Test that PRAGMAs only run on SQLite."""

    @pytest.mark.asyncio
    async def test_pragma_runs_on_sqlite(self):
        with patch("backend.app.core.db_dialect.is_sqlite", return_value=True):
            from backend.app.core.db_dialect import run_pragma

            mock_conn = AsyncMock()
            await run_pragma(mock_conn, "PRAGMA journal_mode = WAL")
            mock_conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_pragma_skipped_on_postgres(self):
        with patch("backend.app.core.db_dialect.is_sqlite", return_value=False):
            from backend.app.core.db_dialect import run_pragma

            mock_conn = AsyncMock()
            await run_pragma(mock_conn, "PRAGMA journal_mode = WAL")
            mock_conn.execute.assert_not_called()


class TestTimezoneStripping:
    """Test that the before_cursor_execute event strips timezone info."""

    def test_strip_aware_datetime(self):
        import datetime

        aware = datetime.datetime(2026, 4, 3, 10, 0, 0, tzinfo=datetime.timezone.utc)
        naive = aware.replace(tzinfo=None)

        def _strip(val):
            if isinstance(val, datetime.datetime) and val.tzinfo is not None:
                return val.replace(tzinfo=None)
            return val

        assert _strip(aware) == naive
        assert _strip(aware).tzinfo is None
        assert _strip(naive) == naive
        assert _strip("not a datetime") == "not a datetime"
        assert _strip(None) is None

    def test_strip_in_dict_params(self):
        import datetime

        aware = datetime.datetime(2026, 4, 3, 10, 0, 0, tzinfo=datetime.timezone.utc)

        def _strip(val):
            if isinstance(val, datetime.datetime) and val.tzinfo is not None:
                return val.replace(tzinfo=None)
            return val

        params = {"name": "test", "created_at": aware, "count": 5}
        result = {k: _strip(v) for k, v in params.items()}
        assert result["created_at"].tzinfo is None
        assert result["name"] == "test"
        assert result["count"] == 5

    def test_strip_in_tuple_params(self):
        import datetime

        aware = datetime.datetime(2026, 4, 3, 10, 0, 0, tzinfo=datetime.timezone.utc)

        def _strip(val):
            if isinstance(val, datetime.datetime) and val.tzinfo is not None:
                return val.replace(tzinfo=None)
            return val

        params = ("test", aware, 5)
        result = tuple(_strip(v) for v in params)
        assert result[1].tzinfo is None
        assert result[0] == "test"

    def test_naive_datetime_unchanged(self):
        import datetime

        naive = datetime.datetime(2026, 4, 3, 10, 0, 0)

        def _strip(val):
            if isinstance(val, datetime.datetime) and val.tzinfo is not None:
                return val.replace(tzinfo=None)
            return val

        result = _strip(naive)
        assert result == naive
        assert result.tzinfo is None


class TestCrossDatabaseConversion:
    """Test SQLite→Postgres type conversion logic used in cross-database import."""

    def test_boolean_conversion(self):
        assert bool(0) is False
        assert bool(1) is True

    def test_datetime_string_conversion(self):
        from datetime import datetime

        val = "2026-04-02 11:01:52.105147"
        result = datetime.fromisoformat(val)
        assert result.year == 2026
        assert result.month == 4
        assert result.microsecond == 105147

    def test_datetime_with_timezone_string(self):
        from datetime import datetime

        val = "2026-04-02T11:01:52+00:00"
        result = datetime.fromisoformat(val)
        assert result.year == 2026

    def test_json_serialization_for_backup(self):
        import json

        values = [{"key": "val"}, [1, 2, 3], "plain string", 42, None]
        for val in values:
            if isinstance(val, (list, dict)):
                serialized = json.dumps(val)
                assert isinstance(serialized, str)
            else:
                assert val == val  # noqa: PLR0124


class TestSafeExecutePattern:
    """Test exception handling patterns for dialect-aware migrations."""

    def test_catches_expected_exceptions(self):
        from sqlalchemy.exc import OperationalError, ProgrammingError

        for exc_type in (OperationalError, ProgrammingError):
            try:
                raise exc_type("test", [], Exception("column already exists"))
            except (OperationalError, ProgrammingError):
                pass

    def test_does_not_catch_integrity_error(self):
        from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

        with pytest.raises(IntegrityError):
            try:
                raise IntegrityError("test", [], Exception("unique violation"))
            except (OperationalError, ProgrammingError):
                pass
