"""zigpy's database carries the Zigbee network key. It has to ride the backup.

Same reasoning as the MFA encryption key, which the ZIP already carries: both
are unrecoverable, and losing either makes a restore quietly useless — encrypted
secrets in one case, every paired device in the other.

The severities differ, and so does the handling. A missing MFA key corrupts data
that already exists, so that copy failure raises. A missing Zigbee database
costs re-pairing, which is recoverable by walking to each plug — so it warns and
lets the backup finish. Failing an operator's whole backup, prints and archives
included, over an optional radio would be the worse trade.
"""

import pytest

from backend.app.api.routes.settings import _restore_zigbee_db, _stage_zigbee_db


def test_staged_when_present(tmp_path):
    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    (data_dir / "zigbee").mkdir(parents=True)
    (data_dir / "zigbee" / "zigbee.db").write_bytes(b"network-key-lives-here")
    staging.mkdir()

    _stage_zigbee_db(data_dir, staging)

    assert (staging / "zigbee" / "zigbee.db").read_bytes() == b"network-key-lives-here"


def test_absent_is_not_an_error(tmp_path):
    """Most installs have no dongle; that is the normal case, not a failure."""
    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    data_dir.mkdir()
    staging.mkdir()

    _stage_zigbee_db(data_dir, staging)

    assert not (staging / "zigbee").exists()


def test_unreadable_source_warns_but_does_not_raise(tmp_path, monkeypatch):
    """An optional radio must not be able to fail the whole backup."""
    import shutil as _shutil

    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    (data_dir / "zigbee").mkdir(parents=True)
    (data_dir / "zigbee" / "zigbee.db").write_bytes(b"x")
    staging.mkdir()

    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(_shutil, "copy2", _boom)

    _stage_zigbee_db(data_dir, staging)  # must not raise

    assert not (staging / "zigbee" / "zigbee.db").exists()


def test_restore_puts_it_back(tmp_path):
    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    data_dir.mkdir()
    (staging / "zigbee").mkdir(parents=True)
    (staging / "zigbee" / "zigbee.db").write_bytes(b"restored-network")

    _restore_zigbee_db(staging, data_dir)

    assert (data_dir / "zigbee" / "zigbee.db").read_bytes() == b"restored-network"


def test_restore_creates_the_directory_on_a_fresh_host(tmp_path):
    """The whole point of this: restoring onto a machine that never had Zigbee."""
    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    data_dir.mkdir()
    (staging / "zigbee").mkdir(parents=True)
    (staging / "zigbee" / "zigbee.db").write_bytes(b"net")

    _restore_zigbee_db(staging, data_dir)

    assert (data_dir / "zigbee").is_dir()


def test_restore_without_the_entry_leaves_an_existing_database_alone(tmp_path):
    """A ZIP from before this feature must not wipe a working network."""
    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    (data_dir / "zigbee").mkdir(parents=True)
    (data_dir / "zigbee" / "zigbee.db").write_bytes(b"live-network")
    staging.mkdir()

    _restore_zigbee_db(staging, data_dir)

    assert (data_dir / "zigbee" / "zigbee.db").read_bytes() == b"live-network"


@pytest.mark.parametrize("helper", [_stage_zigbee_db, _restore_zigbee_db])
def test_helpers_tolerate_a_missing_destination_tree(tmp_path, helper):
    helper(tmp_path / "nope", tmp_path / "also-nope")
