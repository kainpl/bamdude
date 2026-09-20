"""A backup carries BamDude's own data and refuses to reach outside it.

An external library folder is a path somebody else manages — a NAS share, a
laptop's folder over SMB. BamDude indexes what is in it; it does not own it,
cannot promise it is reachable at 03:00, and must not quietly fold gigabytes of
it into a backup the operator sized for their own data. Two things keep it out,
and both are easy to lose by accident, so both are pinned here.
"""

import os

import pytest

from backend.app.core.config import settings
from backend.app.services.backup_files import _stat, copy_tree, directories

# Every root the backup copies, by the name it takes in the ZIP. A new entry
# here means a new thing in everybody's backup: deliberate, never incidental.
OUR_ROOTS = {
    "archive",
    "library",
    "virtual_printer",
    "plate_calibration",
    "icons",
    "projects",
    "products",
    "certs",
}


def test_the_backup_copies_our_roots_and_only_ours():
    assert set(directories(settings)) == OUR_ROOTS


def test_an_external_library_folder_is_not_one_of_them(tmp_path, monkeypatch):
    """External folders live at their own absolute path, outside library_dir.

    `LibraryFolder.external_path` is the row's own field; nothing in the backup
    reads it, and the directory map cannot reach it because the map is fixed.
    """
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library", raising=False)
    elsewhere = tmp_path / "nas" / "models"
    elsewhere.mkdir(parents=True)

    roots = [p.resolve() for p in directories(settings).values()]
    assert not any(elsewhere.resolve() == r or r in elsewhere.resolve().parents for r in roots)


def test_a_link_into_someone_elses_folder_fails_the_backup(tmp_path):
    """Not followed, and not silently skipped either — the backup stops.

    A skipped link would hand back an archive missing files it never mentioned;
    following one would copy a share BamDude does not own. Refusing is the only
    answer that cannot mislead, and it is what `_stat` does for every entry.
    """
    library = tmp_path / "library"
    library.mkdir()
    (library / "own.3mf").write_bytes(b"ours")
    foreign = tmp_path / "nas"
    foreign.mkdir()
    (foreign / "theirs.3mf").write_bytes(b"not ours")

    try:
        os.symlink(foreign, library / "linked", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege.
        pytest.skip(f"symlinks unavailable here: {exc}")

    with pytest.raises(ValueError, match="refuses links"):
        copy_tree(library, tmp_path / "staging")


def test_stat_refuses_a_linked_file_too(tmp_path):
    target = tmp_path / "real.3mf"
    target.write_bytes(b"ours")
    link = tmp_path / "link.3mf"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable here: {exc}")

    with pytest.raises(ValueError, match="refuses links"):
        _stat(link)
