"""External-spool identity changes must re-trigger inventory reconciliation (#2575).

The AMS change-hash in ``_handle_ams_data`` is built from AMS units only, so
swapping the filament on the *external* spool never re-fired ``on_ams_change`` —
and that callback is the only thing that unlinks a now-stale inventory
assignment on the ``ams_id=255`` slot. The detector added here hashes the
external spool's identity fields and fires the same callback on a change.

BamDude divergence pinned below: the FIRST observation seeds the hash without
firing. Upstream fires on the None -> first-value transition, but our
connect-time pushall carries ``print.ams`` and ``vt_tray`` in one message while
``_previous_ams_hash`` also starts at None — so upstream's shape dispatches
``on_ams_change`` twice, concurrently, on every printer on every reconnect, and
that handler holds a DB session across Spoolman HTTP I/O.
"""

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client():
    c = BambuMQTTClient(ip_address="192.0.2.10", access_code="x", serial_number="S1")
    c.state.raw_data = {}
    return c


def _vt(tray_type="PLA", color="FF0000FF", uuid="u1", idx="GFA00", tray_id=254):
    return [
        {
            "id": tray_id,
            "tray_type": tray_type,
            "tray_color": color,
            "tag_uid": "0000000000000000",
            "tray_uuid": uuid,
            "tray_info_idx": idx,
            "remain": 80,
        }
    ]


def test_first_observation_seeds_without_firing():
    c = _client()
    fired = []
    c.on_ams_change = fired.append
    c.state.raw_data["vt_tray"] = _vt()

    c._maybe_trigger_external_spool_change()

    assert fired == []
    assert c._previous_vt_tray_hash is not None


def test_identity_change_fires_the_callback():
    c = _client()
    fired = []
    c.on_ams_change = fired.append
    c.state.raw_data["vt_tray"] = _vt(tray_type="TPU")
    c._maybe_trigger_external_spool_change()  # seed

    c.state.raw_data["vt_tray"] = _vt(tray_type="ABS", uuid="u2")
    c._maybe_trigger_external_spool_change()

    assert len(fired) == 1


def test_unchanged_identity_does_not_fire():
    c = _client()
    fired = []
    c.on_ams_change = fired.append
    c.state.raw_data["vt_tray"] = _vt()
    c._maybe_trigger_external_spool_change()  # seed
    c._maybe_trigger_external_spool_change()
    c._maybe_trigger_external_spool_change()

    assert fired == []


def test_remain_drop_does_not_fire():
    """A print steadily consumes the external spool; `remain` is deliberately
    excluded from the fingerprint so it doesn't fire on every MQTT push."""
    c = _client()
    fired = []
    c.on_ams_change = fired.append
    c.state.raw_data["vt_tray"] = _vt()
    c._maybe_trigger_external_spool_change()  # seed

    for pct in (70, 60, 50, 12):
        vt = _vt()
        vt[0]["remain"] = pct
        c.state.raw_data["vt_tray"] = vt
        c._maybe_trigger_external_spool_change()

    assert fired == []


def test_colour_only_change_fires():
    """Same material, different colour — still a different spool physically."""
    c = _client()
    fired = []
    c.on_ams_change = fired.append
    c.state.raw_data["vt_tray"] = _vt(color="FF0000FF")
    c._maybe_trigger_external_spool_change()  # seed

    c.state.raw_data["vt_tray"] = _vt(color="00FF00FF", uuid="u2")
    c._maybe_trigger_external_spool_change()

    assert len(fired) == 1


def test_callback_receives_current_merged_ams_data():
    c = _client()
    seen = []
    c.on_ams_change = seen.append
    c.state.raw_data["ams"] = [{"id": "0", "tray": [{"id": "0", "tray_type": "PETG"}]}]
    c.state.raw_data["vt_tray"] = _vt()
    c._maybe_trigger_external_spool_change()  # seed

    c.state.raw_data["vt_tray"] = _vt(tray_type="ABS", uuid="u9")
    c._maybe_trigger_external_spool_change()

    assert seen and seen[0] == c.state.raw_data["ams"]


def test_missing_or_malformed_vt_tray_is_a_noop():
    c = _client()
    fired = []
    c.on_ams_change = fired.append

    c._maybe_trigger_external_spool_change()  # no vt_tray key at all
    c.state.raw_data["vt_tray"] = {"id": 254}  # dict, not the normalised list
    c._maybe_trigger_external_spool_change()

    assert fired == []
    assert c._previous_vt_tray_hash is None


def test_no_callback_registered_still_seeds():
    c = _client()
    c.on_ams_change = None
    c.state.raw_data["vt_tray"] = _vt()
    c._maybe_trigger_external_spool_change()
    assert c._previous_vt_tray_hash is not None
