"""Unit tests for :func:`backend.app.core.auth._get_jwt_secret`.

Specifically covers the length-enforcement floor on the ``JWT_SECRET_KEY``
env-var path — the app-side mitigation for PYSEC-2025-183 /
CVE-2025-45768 (HS256 signing with a sub-256-bit key).

The file-path is left to integration coverage; the generator already
produces ``secrets.token_urlsafe(64)`` which is far above the floor.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.app.core.auth import _JWT_SECRET_MIN_LEN, _get_jwt_secret


class TestGetJwtSecretEnvVar:
    def test_env_secret_at_minimum_is_accepted(self) -> None:
        secret = "a" * _JWT_SECRET_MIN_LEN
        with patch.dict("os.environ", {"JWT_SECRET_KEY": secret}, clear=False):
            assert _get_jwt_secret() == secret

    def test_env_secret_above_minimum_is_accepted(self) -> None:
        secret = "a" * (_JWT_SECRET_MIN_LEN + 50)
        with patch.dict("os.environ", {"JWT_SECRET_KEY": secret}, clear=False):
            assert _get_jwt_secret() == secret

    def test_env_secret_below_minimum_raises(self) -> None:
        short = "a" * (_JWT_SECRET_MIN_LEN - 1)
        with (
            patch.dict("os.environ", {"JWT_SECRET_KEY": short}, clear=False),
            pytest.raises(RuntimeError, match="JWT_SECRET_KEY is too short"),
        ):
            _get_jwt_secret()

    def test_empty_env_secret_falls_through_to_file_path(self) -> None:
        # Empty string is falsy → falls through to the file-based path.
        # We don't actually want to exercise the disk side here, so just
        # confirm the env-var branch doesn't pick it up.
        with (
            patch.dict("os.environ", {"JWT_SECRET_KEY": ""}, clear=False),
            patch("backend.app.core.paths.resolve_data_dir") as mock_resolve,
        ):
            # Point at a directory we know has no .jwt_secret — generator
            # branch will execute and produce a valid (long) secret.
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                from pathlib import Path

                mock_resolve.return_value = Path(tmp)
                secret = _get_jwt_secret()

            assert len(secret) >= _JWT_SECRET_MIN_LEN

    def test_error_message_mentions_actionable_command(self) -> None:
        with (
            patch.dict("os.environ", {"JWT_SECRET_KEY": "short"}, clear=False),
            pytest.raises(RuntimeError) as exc_info,
        ):
            _get_jwt_secret()
        # Operator gets a concrete how-to-fix in the error.
        assert "secrets.token_urlsafe" in str(exc_info.value)
        assert "CVE-2025-45768" in str(exc_info.value)
