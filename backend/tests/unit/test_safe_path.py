"""Tests for ``backend.app.utils.safe_path`` (path-traversal hardening, GHSA-r2qv).

``safe_join_under`` is the single source of truth for joining an
attacker-controlled component under a trusted parent. Every escape vector the
original arbitrary-file-write report exercised is pinned here.
"""

from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app.utils.safe_path import PathTraversalError, assert_under, safe_join_under


class TestSafeJoinUnder:
    def test_simple_join_round_trips(self, tmp_path: Path):
        result = safe_join_under(tmp_path, "model.3mf")
        assert result == (tmp_path / "model.3mf").resolve()

    def test_nested_join_round_trips(self, tmp_path: Path):
        result = safe_join_under(tmp_path, "myfolder", "sub", "file.3mf")
        assert result == (tmp_path / "myfolder" / "sub" / "file.3mf").resolve()

    def test_rejects_posix_absolute_path(self, tmp_path: Path):
        with pytest.raises(HTTPException) as ei:
            safe_join_under(tmp_path, "/etc/passwd")
        assert ei.value.status_code == 400

    def test_rejects_windows_style_absolute(self, tmp_path: Path):
        with pytest.raises(HTTPException):
            safe_join_under(tmp_path, "\\\\evil\\share\\x")

    def test_rejects_parent_traversal_segments(self, tmp_path: Path):
        with pytest.raises(HTTPException):
            safe_join_under(tmp_path, "..", "etc", "passwd")

    def test_rejects_embedded_traversal(self, tmp_path: Path):
        with pytest.raises(HTTPException):
            safe_join_under(tmp_path, "a/../../../etc/passwd")

    def test_rejects_null_byte(self, tmp_path: Path):
        with pytest.raises(HTTPException):
            safe_join_under(tmp_path, "evil\x00.3mf")

    def test_rejects_empty_part(self, tmp_path: Path):
        with pytest.raises(HTTPException):
            safe_join_under(tmp_path, "")

    def test_rejects_no_parts(self, tmp_path: Path):
        with pytest.raises(HTTPException):
            safe_join_under(tmp_path)

    def test_rejects_non_str_part(self, tmp_path: Path):
        with pytest.raises(HTTPException):
            safe_join_under(tmp_path, 123)  # type: ignore[arg-type]

    def test_http_false_raises_path_traversal_error(self, tmp_path: Path):
        with pytest.raises(PathTraversalError):
            safe_join_under(tmp_path, "../escape", http=False)


class TestAssertUnder:
    def test_accepts_child(self, tmp_path: Path):
        child = tmp_path / "a" / "b.txt"
        assert assert_under(tmp_path, child) == child.resolve()

    def test_rejects_escape(self, tmp_path: Path):
        with pytest.raises(HTTPException):
            assert_under(tmp_path, tmp_path / ".." / "outside.txt")

    def test_http_false_raises_path_traversal_error(self, tmp_path: Path):
        with pytest.raises(PathTraversalError):
            assert_under(tmp_path, tmp_path / ".." / "x", http=False)
