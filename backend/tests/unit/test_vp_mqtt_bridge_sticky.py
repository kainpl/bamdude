"""Regression tests for G.1 + G.2 / upstream Bambuddy #1371 + #1387.

The VP MQTT bridge caches ``push_status`` snapshots from the real
printer and replays them to slicers via the VP's 1 Hz status push.
Bambu firmware sends three shapes for the same field:

1. Full pushall (after reconnect / explicit pushall): every key
   populated with full state.
2. Status-only incremental: key omitted entirely from the push
   (``{ams_status: 1}``).
3. P1S / A1 incremental: key present but stripped inner content
   (``ams: {ams_status: 1}`` without the ``ams.ams`` array).

Before the fix the bridge replaced ``_latest_print_state`` wholesale on
every push, so shape (2) lost the field and shape (3) overwrote the
cached full blob with a stripped one — slicers reading the cache
between pushalls saw a printer with no AMS, no vt_tray, no net, etc.

The fix:

* For shape (2), preserve the cached value across the sticky-keys list
  (``ams``, ``vt_tray``, ``ams_extruder_map``, ``mapping``, ``net``,
  ``ipcam``, ``lights_report``).
* For shape (3) on ``ams`` specifically, deep-merge: units / trays
  matched by ``id``, prev fields surviving when the incremental
  doesn't mention them. Mirrors what
  ``bambu_mqtt._handle_ams_data`` already does for BamDude's internal
  ``raw_data``.
"""

from __future__ import annotations

from backend.app.services.virtual_printer.mqtt_bridge import _merge_ams_dict


class TestMergeAmsDict:
    """Direct exercise of the deep-merge helper — separate from the
    bridge integration so we don't need a full VP fixture."""

    def test_full_pushall_then_status_only_incremental_keeps_units(self):
        """Shape (2) on ``ams.ams``: incremental's ``ams`` dict has no
        ``ams`` array — keep prev's units list intact."""
        prev = {
            "ams": [
                {"id": 0, "tray": [{"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF"}]},
                {"id": 1, "tray": [{"id": 0, "tray_type": "PETG", "tray_color": "00FF00FF"}]},
            ],
            "ams_status": 1,
            "humidity": 30,
        }
        new = {"ams_status": 2, "humidity": 35}  # no `ams` key at all

        merged = _merge_ams_dict(prev, new)
        assert merged["ams_status"] == 2, "scalar fields take new value"
        assert merged["humidity"] == 35
        # Units survived from prev.
        assert len(merged["ams"]) == 2
        assert merged["ams"][0]["tray"][0]["tray_type"] == "PLA"
        assert merged["ams"][1]["tray"][0]["tray_type"] == "PETG"

    def test_tray_targeted_incremental_preserves_other_trays(self):
        """Shape (3) on a print: incremental updates one tray's state
        without dropping the other trays' tray_type / tray_color."""
        prev = {
            "ams": [
                {
                    "id": 0,
                    "tray": [
                        {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF"},
                        {"id": 1, "tray_type": "PETG", "tray_color": "00FF00FF"},
                        {"id": 2, "tray_type": "ABS", "tray_color": "0000FFFF"},
                    ],
                }
            ]
        }
        new = {
            "ams": [{"id": 0, "tray": [{"id": 1, "state": 11}]}],
        }

        merged = _merge_ams_dict(prev, new)
        trays = merged["ams"][0]["tray"]
        assert len(trays) == 3, "all 3 trays survive"
        # Tray 1 got the new state, kept its type + color.
        tray1 = next(t for t in trays if t["id"] == 1)
        assert tray1["state"] == 11
        assert tray1["tray_type"] == "PETG"
        assert tray1["tray_color"] == "00FF00FF"
        # Trays 0 and 2 untouched.
        tray0 = next(t for t in trays if t["id"] == 0)
        assert tray0["tray_type"] == "PLA"
        tray2 = next(t for t in trays if t["id"] == 2)
        assert tray2["tray_type"] == "ABS"

    def test_unit_only_incremental_without_tray_array(self):
        """Shape (3) on a unit: only the unit's scalar fields change,
        no ``tray`` array — prev's trays must survive intact."""
        prev = {
            "ams": [{"id": 0, "humidity": 30, "tray": [{"id": 0, "tray_type": "PLA"}]}],
        }
        new = {"ams": [{"id": 0, "humidity": 40}]}

        merged = _merge_ams_dict(prev, new)
        unit = merged["ams"][0]
        assert unit["humidity"] == 40
        assert len(unit["tray"]) == 1
        assert unit["tray"][0]["tray_type"] == "PLA"

    def test_p1s_partial_ams_dict_strips_inner_array(self):
        """Reporter's exact P1S 01.09.01.00 shape: the incremental has
        ``ams`` as a dict (not a list) with ``ams_status`` / ``humidity``
        but no inner ``ams`` array. The merge must keep the prev units
        intact while still picking up the new status."""
        prev = {
            "ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}],
            "ams_status": 1,
        }
        new = {"ams_status": 2, "humidity": 30}

        merged = _merge_ams_dict(prev, new)
        assert merged["ams_status"] == 2
        assert merged["humidity"] == 30
        assert len(merged["ams"]) == 1
        assert merged["ams"][0]["tray"][0]["tray_type"] == "PLA"

    def test_new_unit_added(self):
        """Edge case: incremental introduces a unit not in prev."""
        prev = {"ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}]}
        new = {"ams": [{"id": 1, "tray": [{"id": 0, "tray_type": "PETG"}]}]}

        merged = _merge_ams_dict(prev, new)
        assert len(merged["ams"]) == 2
        ids = sorted(u["id"] for u in merged["ams"])
        assert ids == [0, 1]

    def test_new_tray_added_to_existing_unit(self):
        """Incremental introduces a tray id the prev unit didn't have."""
        prev = {"ams": [{"id": 0, "tray": [{"id": 0, "tray_type": "PLA"}]}]}
        new = {"ams": [{"id": 0, "tray": [{"id": 1, "tray_type": "PETG"}]}]}

        merged = _merge_ams_dict(prev, new)
        trays = merged["ams"][0]["tray"]
        assert len(trays) == 2
        ids = sorted(t["id"] for t in trays)
        assert ids == [0, 1]
