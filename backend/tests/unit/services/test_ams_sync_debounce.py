"""The two-push debounce that guards downward AMS corrections."""

from backend.app.services.ams_sync_debounce import DecreaseDebounce

KEY = (1, 0, 2)


def test_single_offer_is_not_confirmed():
    d = DecreaseDebounce(window_seconds=60)
    assert d.offer(KEY, 850.0, "UUID-A", now=100.0) is False


def test_same_value_too_soon_is_not_confirmed():
    d = DecreaseDebounce(window_seconds=60)
    d.offer(KEY, 850.0, "UUID-A", now=100.0)
    assert d.offer(KEY, 850.0, "UUID-A", now=130.0) is False


def test_same_value_after_window_confirms_and_clears():
    d = DecreaseDebounce(window_seconds=60)
    d.offer(KEY, 850.0, "UUID-A", now=100.0)
    assert d.offer(KEY, 850.0, "UUID-A", now=161.0) is True
    # Cleared: the next offer starts a fresh candidate.
    assert d.offer(KEY, 850.0, "UUID-A", now=162.0) is False


def test_different_value_resets_the_clock():
    d = DecreaseDebounce(window_seconds=60)
    d.offer(KEY, 850.0, "UUID-A", now=100.0)
    d.offer(KEY, 840.0, "UUID-A", now=130.0)  # value moved — new candidate
    assert d.offer(KEY, 840.0, "UUID-A", now=161.0) is False  # only 31 s on the new clock
    assert d.offer(KEY, 840.0, "UUID-A", now=191.0) is True


def test_uuid_change_resets_the_candidate():
    d = DecreaseDebounce(window_seconds=60)
    d.offer(KEY, 850.0, "UUID-A", now=100.0)
    assert d.offer(KEY, 850.0, "UUID-B", now=200.0) is False  # replacement spool
    assert d.offer(KEY, 850.0, "UUID-B", now=261.0) is True


def test_keys_are_independent():
    d = DecreaseDebounce(window_seconds=60)
    d.offer((1, 0, 0), 850.0, "U", now=100.0)
    assert d.offer((1, 0, 1), 850.0, "U", now=161.0) is False
