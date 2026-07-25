"""An unwritable backup directory must be diagnosed, not quoted as an errno.

``ProtectSystem=strict`` in our systemd unit makes every path outside the
install / data / log dirs read-only *for the service*, so a NAS share the
operator writes to from their own shell fails with EROFS. That reads like a
permission problem and is not one — every check the operator can think to run
says the directory is fine (upstream #2544).
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from backend.app.services.backup_path import (
    classify_backup_dir_error,
    probe_backup_dir,
    systemd_unit_name,
)

BACKUP_DIR = Path("/mnt/nas/backups")


def _oserror(err: int) -> OSError:
    return OSError(err, "boom", str(BACKUP_DIR))


class TestClassify:
    def test_erofs_under_systemd_names_the_sandbox_and_hands_over_the_fix(self, monkeypatch):
        monkeypatch.setattr("backend.app.services.backup_path.systemd_unit_name", lambda: "bamdude.service")
        result = classify_backup_dir_error(_oserror(errno.EROFS), BACKUP_DIR)
        assert result["writable"] is False
        assert result["code"] == "sandboxed"
        assert "ProtectSystem=strict" in result["message"]
        # The remedy must carry the operator's own path, ready to paste.
        assert f"ReadWritePaths={BACKUP_DIR}" in result["remedy"]
        assert "systemctl edit bamdude.service" in result["remedy"]

    def test_erofs_outside_systemd_is_a_plain_read_only_filesystem(self, monkeypatch):
        monkeypatch.setattr("backend.app.services.backup_path.systemd_unit_name", lambda: None)
        result = classify_backup_dir_error(_oserror(errno.EROFS), BACKUP_DIR)
        assert result["code"] == "read_only"
        assert result["remedy"] is None

    @pytest.mark.parametrize(
        ("err", "code"),
        [
            (errno.EACCES, "permission_denied"),
            (errno.EPERM, "permission_denied"),
            (errno.ENOSPC, "no_space"),
            (errno.ENOTDIR, "not_a_directory"),
            (errno.EEXIST, "not_a_directory"),
            (errno.ENOENT, "missing"),
            (errno.EIO, "error"),
        ],
    )
    def test_each_errno_gets_its_own_code(self, err, code):
        assert classify_backup_dir_error(_oserror(err), BACKUP_DIR)["code"] == code

    def test_detail_carries_the_raw_error_for_the_log(self):
        result = classify_backup_dir_error(_oserror(errno.EACCES), BACKUP_DIR)
        assert "boom" in result["detail"]


class TestSystemdUnitName:
    def test_none_when_not_a_unit(self, monkeypatch):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        assert systemd_unit_name() is None

    def test_read_from_cgroup(self, monkeypatch, tmp_path):
        monkeypatch.setenv("INVOCATION_ID", "abc")
        cgroup = tmp_path / "cgroup"
        cgroup.write_text("0::/system.slice/bamdude.service\n")
        monkeypatch.setattr("backend.app.services.backup_path.Path", lambda _p: cgroup)
        assert systemd_unit_name() == "bamdude.service"

    def test_falls_back_to_our_unit_name_when_cgroup_is_unreadable(self, monkeypatch):
        monkeypatch.setenv("INVOCATION_ID", "abc")

        class _Unreadable:
            def read_text(self):
                raise OSError("nope")

        monkeypatch.setattr("backend.app.services.backup_path.Path", lambda _p: _Unreadable())
        assert systemd_unit_name() == "bamdude.service"


class TestProbe:
    def test_writable_directory_passes_and_leaves_nothing_behind(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.app.services.backup_path.is_running_in_docker", lambda: False)
        target = tmp_path / "backups"
        result = probe_backup_dir(target)
        assert result["writable"] is True
        assert result["code"] == "ok"
        assert result["warning"] is None
        # The probe file must not pollute the backup listing.
        assert list(target.iterdir()) == []

    def test_unwritable_directory_reports_the_diagnosis(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.app.services.backup_path.systemd_unit_name", lambda: "bamdude.service")

        def _boom(*_a, **_kw):
            raise OSError(errno.EROFS, "Read-only file system", str(tmp_path))

        monkeypatch.setattr(Path, "mkdir", _boom)
        result = probe_backup_dir(tmp_path / "nas")
        assert result["writable"] is False
        assert result["code"] == "sandboxed"
        assert result["warning"] is None

    def test_docker_path_on_the_container_root_device_warns(self, tmp_path, monkeypatch):
        """Writable but not bind-mounted: the backup lands in the container's
        ephemeral layer and is gone on the next compose up."""
        monkeypatch.setattr("backend.app.services.backup_path.is_running_in_docker", lambda: True)
        monkeypatch.setattr("backend.app.services.backup_path._is_container_ephemeral", lambda _p: True)
        result = probe_backup_dir(tmp_path / "backups")
        assert result["writable"] is True
        assert result["warning"] == "container_ephemeral"
        assert "bamdude:" in result["remedy"] and "volumes:" in result["remedy"]

    def test_docker_bind_mounted_path_does_not_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.app.services.backup_path.is_running_in_docker", lambda: True)
        monkeypatch.setattr("backend.app.services.backup_path._is_container_ephemeral", lambda _p: False)
        result = probe_backup_dir(tmp_path / "backups")
        assert result["warning"] is None
        assert result["remedy"] is None
