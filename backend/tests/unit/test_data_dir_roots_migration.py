"""m177, the filesystem half: one rename per directory, merge into an existing
root one entry at a time, and stop on anything it cannot decide about.
Nothing is ever copied or deleted here - a conflict leaves both sides as they
were and names them, and the next start picks up where this one stopped."""

import errno

import pytest

from backend.app.migrations import m177_data_dir_roots as m177


def _tree(root, *files):
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(rel.encode())


def test_a_whole_directory_moves_with_one_rename(tmp_path):
    _tree(tmp_path, "archive/library/files/a.3mf", "archive/library/thumbnails/a.png", "archive/library/TestFolder/x")
    assert m177.move_root(tmp_path, "library") == 1
    assert (tmp_path / "library/files/a.3mf").read_bytes() == b"archive/library/files/a.3mf"
    assert (tmp_path / "library/TestFolder/x").exists()
    assert not (tmp_path / "archive/library").exists()


def test_a_missing_source_is_nothing_to_do(tmp_path):
    (tmp_path / "archive").mkdir()
    assert m177.move_root(tmp_path, "library") == 0
    assert not (tmp_path / "library").exists()


def test_an_existing_target_is_merged_entry_by_entry(tmp_path):
    _tree(
        tmp_path,
        "archive/projects/1/attachments/a.jpg",
        "archive/projects/2/attachments/b.jpg",
        "projects/3/attachments/c.jpg",
    )
    assert m177.move_root(tmp_path, "projects") == 2
    for rel in ("projects/1/attachments/a.jpg", "projects/2/attachments/b.jpg", "projects/3/attachments/c.jpg"):
        assert (tmp_path / rel).exists(), rel
    assert not (tmp_path / "archive/projects").exists()


def test_an_entry_present_on_both_sides_stops_the_move_and_touches_nothing(tmp_path):
    _tree(
        tmp_path,
        "archive/products/1/attachments/a.jpg",
        "archive/products/2/attachments/b.jpg",
        "products/2/attachments/z.jpg",
    )
    with pytest.raises(m177.RootMoveConflict) as err:
        m177.move_root(tmp_path, "products")
    assert "products" in str(err.value) and "2" in str(err.value)
    # Entry 1 may already have moved (renames are one at a time); entry 2 is untouched on both sides.
    assert (tmp_path / "archive/products/2/attachments/b.jpg").exists()
    assert (tmp_path / "products/2/attachments/z.jpg").exists()


def test_a_target_that_is_a_file_stops_the_move(tmp_path):
    _tree(tmp_path, "archive/library/files/a.3mf")
    (tmp_path / "library").write_bytes(b"not a directory")
    with pytest.raises(m177.RootMoveConflict):
        m177.move_root(tmp_path, "library")
    assert (tmp_path / "archive/library/files/a.3mf").exists()


def test_a_cross_device_rename_stops_the_move_with_instructions(tmp_path, monkeypatch):
    _tree(tmp_path, "archive/library/files/a.3mf")

    def refuse(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(m177.os, "rename", refuse)
    with pytest.raises(m177.RootMoveConflict) as err:
        m177.move_root(tmp_path, "library")
    assert "by hand" in str(err.value)
    assert (tmp_path / "archive/library/files/a.3mf").exists()


def test_a_second_run_is_a_no_op(tmp_path):
    _tree(tmp_path, "archive/library/files/a.3mf")
    assert m177.move_root(tmp_path, "library") == 1
    assert m177.move_root(tmp_path, "library") == 0
    assert (tmp_path / "library/files/a.3mf").exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("archive/library/files/a.3mf", "library/files/a.3mf"),
        ("archive\\library\\thumbnails\\a.png", "library\\thumbnails\\a.png"),
        ("archive/library/makerworld-covers/7-cover.png", "library/makerworld-covers/7-cover.png"),
        (
            "archive/1/20260914_171521_Untitled/Untitled.gcode.3mf",
            "archive/1/20260914_171521_Untitled/Untitled.gcode.3mf",
        ),
        ("\\\\nas\\share\\external.3mf", "\\\\nas\\share\\external.3mf"),
        ("library/files/already.3mf", "library/files/already.3mf"),
        (None, None),
        ("", ""),
    ],
)
def test_rewrite_drops_the_archive_prefix_of_library_paths_and_nothing_else(value, expected):
    assert m177.rewrite(value) == expected
