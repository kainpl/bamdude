"""Which HMS faults are announced, and how often.

Two defects this pins (upstream #2728, #3226): a fault of the ``hms[]`` array
was identified by its ``attr`` alone — the module and the part — so two faults
on one part shared a key and the second was never announced; and the memory was
one set per printer, forgotten only when the whole stack emptied, so a fault
flickering beside one that stays was announced on every return.
"""

from types import SimpleNamespace

import pytest

from backend.app import main


def fault(full_code: str, severity: int = 2):
    """An ``hms[]`` entry (16 characters) or a ``print_error`` one (8)."""
    if len(full_code) == 16:
        attr, code = int(full_code[:8], 16), int(full_code[8:], 16)
    else:
        attr, code = int(full_code, 16), int(full_code[4:], 16)
    return SimpleNamespace(attr=attr, code=f"0x{code:x}", severity=severity, full_code=full_code)


@pytest.fixture(autouse=True)
def _fresh_memory():
    for store in (main._notified_hms_errors, main._hms_print_state, main._hms_forget_when_gone):
        store.clear()
    yield
    for store in (main._notified_hms_errors, main._hms_print_state, main._hms_forget_when_gone):
        store.clear()


def new(errors, *, state="RUNNING", now=1000.0, printer_id=1):
    return main._take_new_hms_fault_keys(printer_id, errors, state, now=now)


CAMERA_A = "0500060000020005"
CAMERA_B = "0500060000020006"
LID_OPEN = "0300960000030001"


def test_two_faults_on_one_part_are_both_announced():
    assert new([fault(CAMERA_A)]) == {CAMERA_A}
    assert new([fault(CAMERA_A), fault(CAMERA_B)], now=1001.0) == {CAMERA_B}


def test_a_quiet_fault_does_not_silence_a_serious_one_on_the_same_part():
    new([fault(CAMERA_A, severity=4)])
    assert new([fault(CAMERA_A, severity=4), fault(CAMERA_B, severity=1)], now=1001.0) == {CAMERA_B}


def test_a_fault_flickering_beside_a_standing_one_is_announced_once():
    assert new([fault(LID_OPEN), fault(CAMERA_A)]) == {LID_OPEN, CAMERA_A}
    for step in range(1, 6):
        gone, back = 1000.0 + step * 60, 1000.0 + step * 60 + 30
        assert new([fault(LID_OPEN)], now=gone) == set()
        assert new([fault(LID_OPEN), fault(CAMERA_A)], now=back) == set()


def test_a_fault_flickering_alone_is_announced_once():
    assert new([fault(CAMERA_A)]) == {CAMERA_A}
    assert new([], now=1064.0) == set()
    assert new([fault(CAMERA_A)], now=1065.0) == set()


def test_a_fault_gone_longer_than_the_window_is_announced_again():
    new([fault(CAMERA_A)])
    new([], now=1001.0)
    later = 1000.0 + main._HMS_CLEAR_GRACE_SECONDS + 1
    assert new([fault(CAMERA_A)], now=later) == {CAMERA_A}


def test_a_fault_fixed_before_the_print_resumed_is_announced_when_it_recurs():
    runout = "07008011"
    assert new([fault(runout)], state="PAUSE") == {runout}
    # The frame that flips the state may still carry the old fault.
    assert new([fault(runout)], state="RUNNING", now=1010.0) == set()
    assert new([], state="RUNNING", now=1011.0) == set()
    assert new([fault(runout)], state="PAUSE", now=1100.0) == {runout}


def test_the_state_change_forgets_only_what_was_recorded_before_it():
    new([fault(LID_OPEN)], state="RUNNING")
    new([fault(LID_OPEN)], state="PAUSE", now=1010.0)
    assert new([fault(LID_OPEN), fault(CAMERA_A)], state="PAUSE", now=1020.0) == {CAMERA_A}
    assert new([fault(LID_OPEN)], state="PAUSE", now=1030.0) == set()
    assert new([fault(LID_OPEN), fault(CAMERA_A)], state="PAUSE", now=1040.0) == set()


def test_printers_do_not_share_memory():
    assert new([fault(CAMERA_A)], printer_id=1) == {CAMERA_A}
    assert new([fault(CAMERA_A)], printer_id=2) == {CAMERA_A}


def test_an_entry_without_a_full_code_is_keyed_by_attr_and_code():
    bare = SimpleNamespace(attr=0x05000600, code="0x20005", severity=2, full_code="")
    other = SimpleNamespace(attr=0x05000600, code="0x20006", severity=2, full_code="")
    assert len(new([bare, other])) == 2


def test_an_empty_memory_leaves_nothing_behind():
    new([fault(CAMERA_A)])
    new([], now=1000.0 + main._HMS_CLEAR_GRACE_SECONDS + 1)
    assert 1 not in main._notified_hms_errors
    assert 1 not in main._hms_forget_when_gone


def test_an_array_fault_is_labelled_with_its_full_code():
    assert main._hms_label("0500060000020070", "0500_0070") == "0500-0600-0002-0070"
    assert main._hms_label("0500060000020070".lower(), "0500_0070") == "0500-0600-0002-0070"


def test_a_print_error_keeps_its_short_label():
    assert main._hms_label("07008011", "0700_8011") == "0700_8011"
    assert main._hms_label("", "0700_8011") == "0700_8011"
    assert main._hms_label(None, "0700_8011") == "0700_8011"


def test_the_queue_reason_names_an_array_fault_by_its_full_code():
    summary = main._format_hms_error_summary(
        [{"code": "0x20070", "attr": 0x05000600, "module": 5, "severity": 2, "full_code": "0500060000020070"}],
        "22E",
    )
    assert summary is not None
    assert summary.startswith("[0500-0600-0002-0070]")
