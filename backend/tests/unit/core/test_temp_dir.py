"""Scratch lives on the data volume, not on whatever the system calls temp.

The backup stages a COPY OF THE WHOLE DATA TREE before it writes a byte of ZIP.
On Docker the system temp is the container's own layer and on some NAS hosts a
tmpfs — RAM. Neither is where an operator expects a copy of their whole library
to land, and neither was sized for it. These tests pin where scratch goes and
the one place it may not.
"""

import tempfile
from pathlib import Path

from backend.app.core.paths import install_process_temp_dir, resolve_temp_dir


def test_defaults_under_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("TEMP_DIR", raising=False)

    assert resolve_temp_dir() == tmp_path / "tmp"


def test_env_override_wins(tmp_path, monkeypatch):
    # An operator whose DATA_DIR is a slow network volume must be able to put
    # scratch on a local disk.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "fast"))

    assert resolve_temp_dir() == tmp_path / "fast"


def test_env_is_read_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "first"))
    assert resolve_temp_dir() == tmp_path / "first"

    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "second"))
    assert resolve_temp_dir() == tmp_path / "second"


def test_install_points_tempfile_at_it(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "scratch"))
    monkeypatch.setattr(tempfile, "tempdir", None, raising=False)

    chosen = install_process_temp_dir()

    assert chosen == tmp_path / "scratch"
    assert chosen.is_dir()
    assert tempfile.gettempdir() == str(chosen)
    # Not a claim about this module: the point is that every tempfile user in
    # the app lands there without knowing about any of this.
    with tempfile.TemporaryDirectory() as staging:
        assert Path(staging).parent == chosen


def test_a_scratch_dir_inside_a_backed_up_root_is_refused(tmp_path, monkeypatch, caplog):
    """`copy_tree` re-inventories what it copied and refuses if it changed.

    Staging inside the very tree being copied would therefore fail every
    backup — and on a pass where it did not, it would copy the backup into
    itself. Better a loud fallback than either.
    """
    from backend.app.core.config import settings

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive", raising=False)
    monkeypatch.setenv("TEMP_DIR", str(tmp_path / "archive" / "scratch"))
    monkeypatch.setattr(tempfile, "tempdir", None, raising=False)

    chosen = install_process_temp_dir()

    assert chosen == tmp_path / "tmp"
    assert "which the backup copies whole" in caplog.text


def test_the_default_is_not_inside_anything_the_backup_copies(tmp_path, monkeypatch):
    """The roots are siblings of scratch, so the default needs no exclusion."""
    from backend.app.core.config import settings
    from backend.app.services.backup_files import directories

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name, root in directories(settings).items():
        assert (tmp_path / "tmp") not in root.parents, f"{name} contains the scratch directory"
