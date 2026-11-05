import hashlib
import json
import os
import uuid

import pytest
from orca_bamdude.journal import Journal
from orca_bamdude.staging import StagingError, stage


def test_the_staged_copy_is_hashed_as_it_is_copied(tmp_path):
    source = tmp_path / "part.gcode.3mf"
    source.write_bytes(b"PK" + os.urandom(3 * 1024 * 1024))

    staged = stage(str(source), str(tmp_path / "staging"), 10 * 1024 * 1024)

    assert staged.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    with open(staged.path, "rb") as handle:
        assert handle.read() == source.read_bytes()
    assert staged.filename == "part.gcode.3mf"
    assert [n for n in os.listdir(tmp_path / "staging") if n.endswith(".part")] == []


@pytest.mark.parametrize(
    "setup,code",
    [
        (lambda d: (d / "big.3mf").write_bytes(b"x" * 2048) and d / "big.3mf", "too_large"),
        (lambda d: (d / "empty.3mf").write_bytes(b"") or d / "empty.3mf", "empty"),
        (lambda d: (d / "folder").mkdir() or d / "folder", "not_a_file"),
        (lambda d: d / "missing.3mf", "source_missing"),
    ],
)
def test_a_source_outside_the_rules_is_refused(tmp_path, setup, code):
    source = setup(tmp_path)

    with pytest.raises(StagingError) as refused:
        stage(str(source), str(tmp_path / "staging"), 1024)
    assert refused.value.code == code


def test_a_source_that_changes_while_copied_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    source = tmp_path / "part.3mf"
    source.write_bytes(b"x" * (3 * 1024 * 1024))
    calls = {"n": 0}
    original_open = open

    def opening(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if str(path) == str(source) and "r" in mode:
            calls["n"] += 1
            os.utime(source, ns=(1, 1))  # the slicer saved over it mid-copy
        return handle

    monkeypatch.setattr("builtins.open", opening)
    with pytest.raises(StagingError) as refused:
        stage(str(source), str(tmp_path / "staging"), 10 * 1024 * 1024)
    monkeypatch.undo()
    assert refused.value.code == "source_changed" and calls["n"] == 1
    assert os.listdir(tmp_path / "staging") == []


def test_a_cancelled_copy_leaves_nothing(tmp_path):
    source = tmp_path / "part.3mf"
    source.write_bytes(b"x" * 1024)

    with pytest.raises(StagingError) as refused:
        stage(str(source), str(tmp_path / "staging"), 4096, cancelled=lambda: True)
    assert refused.value.code == "cancelled"
    assert os.listdir(tmp_path / "staging") == []


def test_the_journal_keeps_each_namespace_apart(tmp_path):
    ours = Journal(str(tmp_path), ("server-a", "epoch-1", "key-1"))
    restored = Journal(str(tmp_path), ("server-a", "epoch-2", "key-1"))
    other_key = Journal(str(tmp_path), ("server-a", "epoch-1", "key-2"))

    record = ours.create("part.gcode.3mf", "a" * 64)

    assert ours.load(record.operation_id).phase == "prepared"
    assert restored.load(record.operation_id) is None
    assert other_key.records() == []


def test_a_saved_record_survives_and_a_damaged_one_is_skipped(tmp_path):
    journal = Journal(str(tmp_path), ("s", "e", "k"))
    record = journal.create("part.gcode.3mf", "b" * 64)
    record.phase, record.library_file_id = "uploaded", 12
    journal.save(record)
    with open(os.path.join(journal.folder, f"{uuid.uuid4()}.json"), "w") as handle:
        handle.write("{not json")

    again = Journal(str(tmp_path), ("s", "e", "k"))
    [loaded] = again.records()
    assert (loaded.phase, loaded.library_file_id) == ("uploaded", 12)
    assert [n for n in os.listdir(journal.folder) if n.endswith(".tmp")] == []
    with open(os.path.join(journal.folder, f"{record.operation_id}.json")) as handle:
        assert json.load(handle)["phase"] == "uploaded"


def test_a_record_needing_reconciliation_is_listed(tmp_path):
    journal = Journal(str(tmp_path), ("s", "e", "k"))
    done = journal.create("a.3mf", "c" * 64)
    lost = journal.create("b.3mf", "d" * 64)
    done.phase, lost.phase = "finished", "unknown"
    journal.save(done)
    journal.save(lost)

    assert [r.operation_id for r in journal.pending()] == [lost.operation_id]


def test_only_a_uuid_names_a_record_file(tmp_path):
    journal = Journal(str(tmp_path), ("s", "e", "k"))

    with pytest.raises(ValueError):
        journal.load("../../escape")
