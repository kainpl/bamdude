"""Soft-limit policy and best-effort, path-free diagnostics (G02 A2-A4)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.app.core import process_resources as resources


@pytest.mark.parametrize(
    "soft,hard,infinity,failures,expected",
    [
        (1024, 1048576, -1, 0, [(65536, 1048576)]),
        (1024, 4096, -1, 0, [(4096, 4096)]),
        (4096, 4096, -1, 0, []),
        (70000, 1048576, -1, 0, []),
        (-1, -1, -1, 0, []),
        (1024, -1, -1, 0, [(65536, -1)]),
        (1024, 2**63 - 1, 2**63 - 1, 0, [(65536, 2**63 - 1)]),
        (2**63 - 1, 2**63 - 1, 2**63 - 1, 0, []),
        (1024, 10**12, -1, 0, [(65536, 10**12)]),
        (1024, 1048576, -1, 1, [(65536, 1048576), (10240, 1048576)]),
        (1024, 1048576, -1, 2, [(65536, 1048576), (10240, 1048576)]),
        (1024, 4096, -1, 1, [(4096, 4096)]),
        (20000, 1048576, -1, 1, [(65536, 1048576)]),
    ],
)
def test_soft_limit_never_changes_hard_or_lowers_soft(monkeypatch, soft, hard, infinity, failures, expected):
    calls = []

    def set_limit(kind, values):
        calls.append(values)
        assert kind == 7 and values[1] == hard
        assert values[0] > soft
        if len(calls) <= failures:
            raise PermissionError("sentinel-private-path")

    fake = SimpleNamespace(
        RLIMIT_NOFILE=7, RLIM_INFINITY=infinity, getrlimit=lambda _: (soft, hard), setrlimit=set_limit
    )
    monkeypatch.setattr(resources, "resource", fake)
    resources.raise_nofile_limit()
    assert calls == expected


def test_unsupported_and_unavailable_limits_are_noop(monkeypatch):
    monkeypatch.setattr(resources, "resource", None)
    resources.raise_nofile_limit()
    fake = SimpleNamespace(RLIMIT_NOFILE=7, getrlimit=Mock(side_effect=OSError("denied")), setrlimit=Mock())
    monkeypatch.setattr(resources, "resource", fake)
    resources.raise_nofile_limit()
    fake.setrlimit.assert_not_called()


def test_limit_log_does_not_include_exception_path(monkeypatch, caplog):
    fake = SimpleNamespace(
        RLIMIT_NOFILE=7,
        RLIM_INFINITY=-1,
        getrlimit=lambda _: (1024, 4096),
        setrlimit=Mock(side_effect=PermissionError("SECRET_PATH")),
    )
    monkeypatch.setattr(resources, "resource", fake)
    with caplog.at_level("INFO", logger=resources.logger.name):
        resources.raise_nofile_limit()
    assert "PermissionError" in caplog.text
    assert "SECRET_PATH" not in caplog.text


def _diagnostic_sources(monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "linux")
    monkeypatch.setattr(resources.psutil, "Process", lambda: SimpleNamespace(num_fds=lambda: 10))
    monkeypatch.setattr(
        resources,
        "resource",
        SimpleNamespace(RLIMIT_NOFILE=7, RLIM_INFINITY=2**63 - 1, getrlimit=lambda _: (4096, 2**63 - 1)),
    )


def test_classification_uses_only_link_text_and_never_exports_it(monkeypatch):
    _diagnostic_sources(monkeypatch)
    targets = [
        "/NAS/SECRET/farm.db (deleted)",
        "/NAS/SECRET/farm.db-wal",
        "/NAS/SECRET/farm.db-shm",
        "/NAS/SECRET/directory",
        "socket:[1]",
        "pipe:[2]",
        "anon_inode:[eventpoll]",
        "/dev/null",
    ]
    monkeypatch.setattr(resources.os, "listdir", lambda _: [str(n) for n in range(9)])

    def readlink(path):
        n = int(path.rsplit("/", 1)[1])
        if n == 8:
            raise FileNotFoundError("closed listing descriptor")
        return targets[n]

    monkeypatch.setattr(resources.os, "readlink", readlink)
    # Limit the prohibition to the call; pytest itself needs these functions.
    with monkeypatch.context() as guard:
        for name in ("stat", "fstat", "lstat"):
            guard.setattr(resources.os, name, Mock(side_effect=AssertionError("target filesystem accessed")))
        info = resources.collect_fd_info()
    assert info == {
        "count": 10,
        "soft_limit": 4096,
        "hard_limit": -1,
        "kinds_truncated": False,
        "kinds": {
            "db_file": 1,
            "wal_file": 1,
            "shm_file": 1,
            "other_regular": 1,
            "socket": 1,
            "pipe": 1,
            "other": 2,
            "disappeared": 1,
        },
    }
    assert "SECRET" not in repr(info) and "NAS" not in repr(info)


def test_fd_cap_does_not_truncate_total_count(monkeypatch):
    _diagnostic_sources(monkeypatch)
    monkeypatch.setattr(resources.psutil, "Process", lambda: SimpleNamespace(num_fds=lambda: 5000))
    monkeypatch.setattr(resources.os, "listdir", lambda _: list(map(str, range(5000))))
    link = Mock(return_value="socket:[1]")
    monkeypatch.setattr(resources.os, "readlink", link)
    info = resources.collect_fd_info()
    assert info["count"] == 5000 and info["kinds_truncated"]
    assert info["kinds"] == {"socket": 4096} and link.call_count == 4096


def test_exhaustion_keeps_limits_and_unknown_is_not_zero(monkeypatch):
    _diagnostic_sources(monkeypatch)
    monkeypatch.setattr(resources.psutil, "Process", Mock(side_effect=OSError("EMFILE")))
    monkeypatch.setattr(resources.os, "listdir", Mock(side_effect=OSError("EMFILE")))
    info = resources.collect_fd_info()
    assert info == {"count": None, "kinds": None, "soft_limit": 4096, "hard_limit": -1, "kinds_truncated": False}


def test_windows_does_not_call_handles_a_descriptor_count(monkeypatch):
    monkeypatch.setattr(resources, "resource", None)
    monkeypatch.setattr(resources.sys, "platform", "win32")
    handles = Mock(return_value=123)
    monkeypatch.setattr(resources.psutil, "Process", lambda: SimpleNamespace(num_handles=handles))
    info = resources.collect_fd_info()
    assert info["count"] is None and info["kinds"] is None and info["soft_limit"] is None
    handles.assert_not_called()


def test_collector_does_not_import_code_on_first_call(monkeypatch):
    import builtins

    _diagnostic_sources(monkeypatch)
    monkeypatch.setattr(resources.os, "listdir", lambda _: [])
    original_import = builtins.__import__
    imports = []

    def tracked_import(*args, **kwargs):
        imports.append(args[0])
        return original_import(*args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "__import__", tracked_import)
        info = resources.collect_fd_info()
    assert not imports and info["count"] == 10
