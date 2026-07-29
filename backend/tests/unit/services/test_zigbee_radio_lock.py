"""One process owns the radio. Two corrupt the stream in ways that surface later.

The failures this prevents do not look like a lock problem — they look like
random pairing failures and dropped devices, weeks later. The failures it must
NOT cause are the reason stale locks are reclaimed rather than honoured.
"""

import os

from backend.app.services.zigbee.radio_lock import RadioLock


def test_second_acquire_is_refused(tmp_path):
    path = tmp_path / "radio.lock"
    first, second = RadioLock(path), RadioLock(path)

    assert first.acquire() is True
    assert second.acquire() is False

    first.release()
    assert second.acquire() is True
    second.release()


def test_stale_lock_is_reclaimed(tmp_path):
    """An unclean shutdown must not brick Zigbee until someone finds a file.

    Refusing on a dead PID would be a worse failure than the one being
    prevented: the operator has no way to know the file exists, let alone that
    deleting it is the fix.
    """
    lock_path = tmp_path / "radio.lock"
    lock_path.write_text("999999999", encoding="utf-8")  # cannot be a live PID

    assert RadioLock(lock_path).acquire() is True


def test_garbage_lock_file_is_reclaimed(tmp_path):
    """Half-written by a crash mid-write — stale, not fatal."""
    lock_path = tmp_path / "radio.lock"
    lock_path.write_text("not-a-pid", encoding="utf-8")

    assert RadioLock(lock_path).acquire() is True


def test_empty_lock_file_is_reclaimed(tmp_path):
    lock_path = tmp_path / "radio.lock"
    lock_path.write_text("", encoding="utf-8")

    assert RadioLock(lock_path).acquire() is True


def test_lock_records_the_owning_pid(tmp_path):
    """The PID is the whole diagnostic when someone asks who holds the radio."""
    lock_path = tmp_path / "radio.lock"
    lock = RadioLock(lock_path)
    lock.acquire()

    assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())
    lock.release()


def test_reacquire_by_the_same_process_succeeds(tmp_path):
    """Our own leftover lock is not a reason to refuse ourselves."""
    lock_path = tmp_path / "radio.lock"
    lock_path.write_text(str(os.getpid()), encoding="utf-8")

    assert RadioLock(lock_path).acquire() is True


def test_release_without_acquire_is_safe(tmp_path):
    RadioLock(tmp_path / "radio.lock").release()


def test_release_does_not_delete_a_lock_it_never_held(tmp_path):
    """Releasing must not evict the process that actually owns the radio."""
    lock_path = tmp_path / "radio.lock"
    owner = RadioLock(lock_path)
    owner.acquire()

    RadioLock(lock_path).release()  # a different instance that never acquired

    assert lock_path.exists()
    owner.release()


def test_parent_directory_is_created(tmp_path):
    """DATA_DIR/zigbee/ will not exist on a first run."""
    lock = RadioLock(tmp_path / "zigbee" / "radio.lock")

    assert lock.acquire() is True
    lock.release()
