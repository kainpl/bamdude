"""The short-lived copies notifications link to: the name is the credential,
and the age limit is the link's expiry."""

import os
import re
import time

import pytest

from backend.app.utils import notification_photos as photos


@pytest.fixture(autouse=True)
def _dir(tmp_path, monkeypatch):
    monkeypatch.setattr(photos, "notification_photos_dir", lambda: tmp_path / "notification-photos")


def test_a_saved_copy_is_found_by_its_name():
    name = photos.save_notification_photo(b"jpeg-bytes", "print_complete")

    assert photos.find_notification_photo(name).read_bytes() == b"jpeg-bytes"


def test_the_name_carries_the_event_and_192_random_bits():
    name = photos.save_notification_photo(b"x", "First Layer!")

    assert re.fullmatch(r"firstlayer_\d{8}_\d{6}_[A-Za-z0-9_-]{32}\.jpg", name)
    assert name != photos.save_notification_photo(b"x", "First Layer!")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "photo.jpg",
        "../secret.jpg",
        "print_20260101_000000_short.jpg",
        "print_20260101_000000_" + "a" * 32 + ".png",
        "print_20260101_000000_" + "a" * 32 + ".jpg/../x",
    ],
)
def test_a_name_of_another_shape_opens_nothing(name):
    assert photos.find_notification_photo(name) is None


def test_a_well_shaped_name_that_was_never_written_opens_nothing():
    assert photos.find_notification_photo("print_20260101_000000_" + "a" * 32 + ".jpg") is None


def test_a_copy_past_its_age_is_refused_before_any_sweep():
    name = photos.save_notification_photo(b"x", "print_complete")
    path = photos.notification_photos_dir() / name
    old = time.time() - photos.MAX_AGE_SECONDS - 60
    os.utime(path, (old, old))

    assert path.exists()
    assert photos.find_notification_photo(name) is None


def test_writing_a_new_copy_removes_the_expired_ones():
    name = photos.save_notification_photo(b"x", "print_complete")
    path = photos.notification_photos_dir() / name
    old = time.time() - photos.MAX_AGE_SECONDS - 60
    os.utime(path, (old, old))

    kept = photos.save_notification_photo(b"y", "print_complete")

    assert not path.exists()
    assert photos.find_notification_photo(kept) is not None


def test_the_copies_live_three_days():
    assert photos.MAX_AGE_SECONDS == 3 * 24 * 60 * 60
