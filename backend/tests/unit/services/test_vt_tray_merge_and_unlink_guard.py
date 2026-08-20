"""A partial report about the external spool must not erase what we know.

Two halves of one failure, both reproduced from the live logs.

**The cache.** ``vt_tray`` was assigned wholesale from every report, so a push
carrying only ``remain`` erased ``tray_type``, ``tray_color`` and the RFID
identity. BambuStudio never showed this because it parses into a persistent
``DevAmsTray`` and writes only the keys a message contains — which is why the
user saw the slot correctly configured in BS while BamDude had forgotten it.

**The consequence.** ``main.on_ams_change`` compares the slot's filament against
the assignment fingerprint and unlinks on any difference; ``""`` differs from
every material name. So the erased field deleted the spool link — and nothing
automatic restores an ``ams_id=255`` link, because auto-assign runs inside the
``ams`` loop the external slot never enters. Live capture, two seconds after the
user assigned a spool mid-print:

    Auto-unlink: spool 145 AMS255-T0 - fingerprint mismatch
      (cur=50543DFF/  fp=000000FF/PETG  spool=50543DFF/PETG)

The colour had already come back as the NEW spool's. Only the type was missing.
"""

from __future__ import annotations

import pytest

from backend.app.main import slot_reported_no_filament
from backend.app.services.bambu_mqtt import _merge_vt_tray


def _full() -> list[dict]:
    return [
        {
            "id": 254,
            "tray_type": "PETG",
            "tray_color": "50543DFF",
            "tray_info_idx": "GFG99",
            "tag_uid": "0123456789ABCDEF",
            "tray_uuid": "ABC0123456789ABCDEF0123456789ABC",
            "remain": 80,
        }
    ]


class TestAbsentFieldsSurvive:
    def test_a_remain_only_push_keeps_the_filament_identity(self) -> None:
        """The exact shape that deleted the link: everything but ``remain`` gone."""
        merged = _merge_vt_tray(_full(), [{"id": 254, "remain": 45}])
        assert merged[0]["tray_type"] == "PETG"
        assert merged[0]["tray_color"] == "50543DFF"
        assert merged[0]["tray_info_idx"] == "GFG99"
        assert merged[0]["remain"] == 45, "what the push did carry still lands"

    def test_a_colour_only_push_keeps_the_type(self) -> None:
        """The live capture: colour arrived as the new spool's, type did not."""
        merged = _merge_vt_tray(_full(), [{"id": 254, "tray_color": "000000FF"}])
        assert merged[0]["tray_color"] == "000000FF"
        assert merged[0]["tray_type"] == "PETG"

    def test_the_id_may_arrive_as_a_string(self) -> None:
        """vir_slot's remap writes ``"254"``; the AMS payload uses ints. Matching
        by string keeps a type change from looking like a different slot."""
        merged = _merge_vt_tray(_full(), [{"id": "254", "remain": 10}])
        assert len(merged) == 1
        assert merged[0]["tray_type"] == "PETG"


class TestAnExplicitValueStillWins:
    def test_an_explicit_empty_type_clears_the_slot(self) -> None:
        """A report IS a report. Unloading must still be able to empty a slot —
        the guard against believing it too readily lives in the unlink decision,
        not here."""
        merged = _merge_vt_tray(_full(), [{"id": 254, "tray_type": "", "tray_color": ""}])
        assert merged[0]["tray_type"] == ""
        assert merged[0]["tray_color"] == ""

    def test_a_new_type_overwrites(self) -> None:
        merged = _merge_vt_tray(_full(), [{"id": 254, "tray_type": "PLA"}])
        assert merged[0]["tray_type"] == "PLA"


class TestIdentityIsNeverRevokedByABlank:
    @pytest.mark.parametrize("field_name", ["tag_uid", "tray_uuid"])
    def test_a_blank_rfid_field_does_not_wipe_a_known_one(self, field_name: str) -> None:
        """Mirrors the AMS merge's documented rule: routine pushes carry empty
        RFID fields that would otherwise erase what the connect-time pushall
        established."""
        before = _full()
        merged = _merge_vt_tray(before, [{"id": 254, field_name: ""}])
        assert merged[0][field_name] == before[0][field_name]

    def test_a_real_rfid_value_still_replaces(self) -> None:
        merged = _merge_vt_tray(_full(), [{"id": 254, "tag_uid": "FEDCBA9876543210"}])
        assert merged[0]["tag_uid"] == "FEDCBA9876543210"


class TestTheEdges:
    def test_nothing_cached_yet_passes_the_report_through(self) -> None:
        incoming = [{"id": 254, "tray_type": "PLA"}]
        assert _merge_vt_tray(None, incoming) == incoming
        assert _merge_vt_tray([], incoming) == incoming

    def test_an_unseen_slot_is_added_whole(self) -> None:
        merged = _merge_vt_tray(_full(), [{"id": 255, "tray_type": "ABS"}])
        assert merged == [{"id": 255, "tray_type": "ABS"}]

    def test_a_dual_slot_report_merges_each_slot_against_its_own(self) -> None:
        existing = _full() + [{"id": 255, "tray_type": "ABS", "tray_color": "FFFFFFFF"}]
        merged = _merge_vt_tray(existing, [{"id": 254, "remain": 5}, {"id": 255, "remain": 7}])
        by_id = {str(e["id"]): e for e in merged}
        assert by_id["254"]["tray_type"] == "PETG"
        assert by_id["255"]["tray_type"] == "ABS"
        assert by_id["255"]["tray_color"] == "FFFFFFFF"

    def test_malformed_entries_are_passed_along_untouched(self) -> None:
        """⚠️ Only entries that are not objects at all. An entry that *is* an
        object but carries no ``id`` used to be passed through here too — and
        that was the defect, not the contract: the printer sends exactly that
        shape for the external slot, and passing it through erased the slot.
        See ``TestADeltaWithNoSlotIdAtAll``."""
        assert _merge_vt_tray(_full(), ["nonsense"]) == ["nonsense"]

    def test_the_cached_list_is_not_mutated(self) -> None:
        before = _full()
        _merge_vt_tray(before, [{"id": 254, "tray_type": "PLA"}])
        assert before[0]["tray_type"] == "PETG"


class TestTheUnlinkGuard:
    """``slot_reported_no_filament`` — told nothing vs told it is empty."""

    @pytest.mark.parametrize("state", [None, 3, 11, 26])
    def test_a_blank_type_without_an_empty_state_is_silence(self, state) -> None:
        """26 is the code an X2D was seen reporting; 3 is what an A1 Mini BMCU
        and a P1S Standard AMS report for a *loaded* slot; None is an external
        slot, which has no state at all."""
        assert slot_reported_no_filament("", state) is True

    @pytest.mark.parametrize("state", [9, 10])
    def test_the_firmwares_own_empty_codes_are_believed(self, state: int) -> None:
        """A spool actually pulled out must still unlink, or the link outlives
        the filament."""
        assert slot_reported_no_filament("", state) is False

    @pytest.mark.parametrize("state", [None, 3, 9, 10, 11, 26])
    def test_a_real_material_is_never_silence(self, state) -> None:
        """Whatever the state says, a named filament is a reading — the caller
        compares it against the fingerprint as before."""
        assert slot_reported_no_filament("PETG", state) is False

    def test_whitespace_is_not_a_material(self) -> None:
        assert slot_reported_no_filament("   ", 3) is True

    def test_a_missing_type_field_is_handled(self) -> None:
        """``tray.get("tray_type")`` yields None when the key was never sent."""
        assert slot_reported_no_filament(None, 3) is True


class TestADeltaWithNoSlotIdAtAll:
    """⚠️ The shape every test above missed: the printer omits ``id`` entirely.

    Live capture from the MQTT log — the reply to our OWN ``extrusion_cali_sel``,
    which every slot configuration sends right after ``ams_filament_setting``::

        {"print":{"command":"extrusion_cali_sel","result":"success",
                  "vt_tray":[{"tray_color":"50543DFF"}], ...}}

    One key, no ``id``. The ``ams_filament_setting`` reply a heartbeat earlier
    carried the complete entry — id, ``tray_type``, filament id — and merged
    cleanly; this one threw all of it away. Keyed matching cannot place it, and taking it wholesale
    erases the type, the filament id **and the slot id** — after which nothing
    can ever be keyed again, so every later report replaces the cache wholesale
    too. That cascade is why only a manual reconfigure (which forces a full
    pushall) appeared to fix the slot.
    """

    def test_the_captured_delta_keeps_everything_it_did_not_carry(self) -> None:
        merged = _merge_vt_tray(_full(), [{"tray_color": "50543DFF"}])
        assert len(merged) == 1
        assert merged[0]["tray_color"] == "50543DFF", "what it did carry lands"
        assert merged[0]["tray_type"] == "PETG"
        assert merged[0]["tray_info_idx"] == "GFG99"

    def test_it_keeps_the_slot_id_so_the_next_merge_still_works(self) -> None:
        """The cascade, pinned: losing ``id`` is what poisons every later merge."""
        once = _merge_vt_tray(_full(), [{"tray_color": "50543DFF"}])
        assert once[0].get("id") == 254

        twice = _merge_vt_tray(once, [{"remain": 42}])
        assert twice[0]["tray_type"] == "PETG", "the second delta erased what the first preserved"
        assert twice[0]["tray_color"] == "50543DFF"
        assert twice[0]["remain"] == 42

    def test_a_cache_already_missing_its_id_still_heals(self) -> None:
        """Installs that already hold a poisoned cache must recover on the next
        delta rather than waiting for a full pushall."""
        poisoned = [{"tray_type": "PETG", "tray_color": "000000FF"}]
        merged = _merge_vt_tray(poisoned, [{"tray_color": "50543DFF"}])
        assert merged[0]["tray_type"] == "PETG"
        assert merged[0]["tray_color"] == "50543DFF"

    def test_it_refuses_to_guess_when_two_slots_are_cached(self) -> None:
        """⚠️ H2D has two external slots. An id-less delta cannot say which one
        it describes, and attaching it to the wrong one — or replacing the list
        and dropping the other slot — is worse than ignoring a colour update."""
        two = [
            {"id": 254, "tray_type": "PETG", "tray_color": "000000FF"},
            {"id": 255, "tray_type": "PLA", "tray_color": "FFFFFFFF"},
        ]
        merged = _merge_vt_tray(two, [{"tray_color": "50543DFF"}])
        assert merged == two, "a delta we cannot place must change nothing"

    def test_an_identified_delta_is_unaffected_by_any_of_this(self) -> None:
        merged = _merge_vt_tray(_full(), [{"id": 254, "tray_color": "50543DFF"}])
        assert merged[0]["tray_type"] == "PETG"
        assert merged[0]["tray_color"] == "50543DFF"
