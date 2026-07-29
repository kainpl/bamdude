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
    """Staged as a real database snapshot, not a byte-for-byte file copy."""
    import sqlite3

    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    (data_dir / "zigbee").mkdir(parents=True)
    staging.mkdir()

    conn = sqlite3.connect(data_dir / "zigbee" / "zigbee.db")
    conn.execute("CREATE TABLE devices_v15 (ieee TEXT)")
    conn.execute("INSERT INTO devices_v15 VALUES ('34:8d:13:ff:fe:11:e4:6f')")
    conn.commit()
    conn.close()

    _stage_zigbee_db(data_dir, staging)

    copied = sqlite3.connect(staging / "zigbee" / "zigbee.db")
    rows = copied.execute("SELECT ieee FROM devices_v15").fetchall()
    copied.close()
    assert rows == [("34:8d:13:ff:fe:11:e4:6f",)]


def test_absent_is_not_an_error(tmp_path):
    """Most installs have no dongle; that is the normal case, not a failure."""
    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    data_dir.mkdir()
    staging.mkdir()

    _stage_zigbee_db(data_dir, staging)

    assert not (staging / "zigbee").exists()


def test_a_corrupt_source_warns_but_does_not_raise(tmp_path):
    """An optional radio must not be able to fail the whole backup.

    A file that is not a database at all is the cheapest way to make the
    snapshot fail for real, rather than patching the call out.
    """
    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    (data_dir / "zigbee").mkdir(parents=True)
    (data_dir / "zigbee" / "zigbee.db").write_bytes(b"definitely not sqlite")
    staging.mkdir()

    _stage_zigbee_db(data_dir, staging)  # must not raise


def test_wal_contents_are_included(tmp_path):
    """The bug this whole helper exists to avoid.

    zigpy runs WAL, so a freshly-formed network sits in zigbee.db-wal while the
    main file is still an empty header. Measured on the first dongle run: a
    plain copy of zigbee.db yielded ZERO tables while the live database had 13,
    including the network key. The backup would have looked fine and restored to
    nothing.
    """
    import sqlite3

    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    (data_dir / "zigbee").mkdir(parents=True)
    staging.mkdir()
    live = data_dir / "zigbee" / "zigbee.db"

    conn = sqlite3.connect(live)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE network_backups_v15 (key TEXT)")
    conn.execute("INSERT INTO network_backups_v15 VALUES ('the-network-key')")
    conn.commit()
    # Deliberately NOT closed and NOT checkpointed: this is the live-database
    # state a backup actually runs against.
    try:
        _stage_zigbee_db(data_dir, staging)

        copied = sqlite3.connect(staging / "zigbee" / "zigbee.db")
        rows = copied.execute("SELECT key FROM network_backups_v15").fetchall()
        copied.close()
    finally:
        conn.close()

    assert rows == [("the-network-key",)]


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


def test_restore_clears_the_previous_networks_wal(tmp_path):
    """Sidecars belong to the database being replaced, not the restored one.

    Leaving them would mean trusting SQLite's salt check to reject a WAL from a
    different database. It does reject it — but the thing at stake is the
    network key, and the staged file is already a complete snapshot that needs
    no sidecar.
    """
    data_dir, staging = tmp_path / "data", tmp_path / "stage"
    (data_dir / "zigbee").mkdir(parents=True)
    (data_dir / "zigbee" / "zigbee.db").write_bytes(b"old")
    (data_dir / "zigbee" / "zigbee.db-wal").write_bytes(b"old-wal")
    (data_dir / "zigbee" / "zigbee.db-shm").write_bytes(b"old-shm")
    (staging / "zigbee").mkdir(parents=True)
    (staging / "zigbee" / "zigbee.db").write_bytes(b"restored")

    _restore_zigbee_db(staging, data_dir)

    assert (data_dir / "zigbee" / "zigbee.db").read_bytes() == b"restored"
    assert not (data_dir / "zigbee" / "zigbee.db-wal").exists()
    assert not (data_dir / "zigbee" / "zigbee.db-shm").exists()
