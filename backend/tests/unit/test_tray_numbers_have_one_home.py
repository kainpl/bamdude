"""Tray numbers are computed, split, compared and labelled in one place: ``utils/tray_ids``.

The scan (``tray_number_scan``) finds hand-written tray arithmetic in
``backend/app``: an expression that names a tray-ish value
(``ams|tray|unit|slot|global``) in a numbering form — ``* 4 +``, ``<< 8 |``,
``254 +``, ``// 4``, ``% 4``, ``& 3``, ``>> 2``, ``>> 8``, ``& 0xFF``,
``- 254``, ``- 128``, ``>= 254``, ``< 254``, ``>= 128``, ``128 <= x <= 135``,
``chr(65 + …)``. Each finding is moved to the helper or named below with the
stage that owns it (N01 C: load / unload, RFID, K-profile, drying; D: runout /
HMS) and why. An exemption that no longer matches a finding fails too: a
landed stage removes its rows (stage B — dispatch and the slot setting — has).

The frontend twin is ``frontend/src/__tests__/utils/trayNumbersHaveOneHome.test.ts``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tray_number_scan import scan  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
HOME = "backend/app/utils/tray_ids.py"

#: ``(file, function)`` → ``(stage, reason)``.
EXEMPT: dict[tuple[str, str], tuple[str, str]] = {
    # --- stage C: load / unload, RFID, K-profile, drying -----------------------
    ("backend/app/services/bambu_mqtt.py", "change_filament_wire_ids"): ("C", "load command wire ids"),
    ("backend/app/services/bambu_mqtt.py", "a2l_lite_wire_ids"): ("C", "A2L outbound wire ids"),
    ("backend/app/services/bambu_mqtt.py", "BambuMQTTClient._rfid_refresh_refusal"): ("C", "RFID refresh"),
    ("backend/app/services/bambu_mqtt.py", "BambuMQTTClient._publish_rfid_gcode"): ("C", "RFID M620 R<n>"),
    ("backend/app/services/bambu_mqtt.py", "BambuMQTTClient.extrusion_cali_sel"): ("C", "K-profile select"),
    ("backend/app/services/bambu_mqtt.py", "BambuMQTTClient.extrusion_cali_set"): ("C", "K value set"),
    ("backend/app/api/routes/printers.py", "configure_ams_slot"): ("C", "extrusion_cali_set tray id"),
    ("backend/app/services/drying_preflight.py", "max_temp_for"): ("C", "drying ceiling per unit"),
    ("backend/app/services/scheduled_drying.py", "_ams_label"): ("C", "drying schedule unit label"),
    ("backend/app/services/print_scheduler.py", "PrintScheduler._notify_auto_drying_suspended"): (
        "C",
        "auto-drying notice unit label",
    ),
    # --- stage D: runout / HMS ----------------------------------------------
    ("backend/app/services/bambu_mqtt.py", "BambuMQTTClient._resolve_runout_tray"): ("D", "runout tray from HMS"),
    # --- not a tray number --------------------------------------------------
    ("backend/app/services/bambu_mqtt.py", "BambuMQTTClient._process_message"): (
        "-",
        "ams_status main / sub bytes, not a tray",
    ),
    ("backend/app/services/bambu_mqtt.py", "BambuMQTTClient._handle_ams_data"): (
        "-",
        "ams_status main / sub bytes, not a tray",
    ),
}


def _findings():
    return scan(REPO / "backend/app", REPO, skip=frozenset({HOME}))


def test_no_hand_written_tray_arithmetic_outside_the_helper():
    unexpected = sorted(
        f"{f.file}:{f.line} {f.function} — {f.form}" for f in _findings() if (f.file, f.function) not in EXEMPT
    )
    assert unexpected == []


def test_every_exemption_still_names_a_finding():
    found = {(f.file, f.function) for f in _findings()}
    assert sorted(key for key in EXEMPT if key not in found) == []


def test_the_scan_sees_the_forms():
    """The rule itself: a hand copy of each form is a finding (so an empty list proves something)."""
    import tempfile

    sample = (
        "def f(ams_id, tray_id, global_tray, unit, slot):\n"
        "    a = ams_id * 4 + tray_id\n"
        "    b = (ams_id << 8) | slot\n"
        "    c = 254 + tray_id\n"
        "    d = global_tray // 4\n"
        "    e = global_tray % 4\n"
        "    g = slot & 3\n"
        "    h = global_tray >> 2\n"
        "    i = slot >> 8\n"
        "    j = slot & 0xFF\n"
        "    k = global_tray - 254\n"
        "    m = unit - 128\n"
        "    n = global_tray >= 254\n"
        "    o = global_tray < 254\n"
        "    q = unit >= 128\n"
        "    r = 128 <= unit <= 135\n"
        "    s = chr(65 + ams_id)\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "pkg").mkdir()
        (root / "pkg" / "m.py").write_text(sample, encoding="utf-8")
        forms = {f.form for f in scan(root / "pkg", root, skip=frozenset())}
    assert forms == {
        "* 4 +",
        "<< 8 |",
        "254 +",
        "// 4",
        "% 4",
        "& 3",
        ">> 2",
        ">> 8",
        "& 0xFF",
        "- 254",
        "- 128",
        ">= 254",
        "< 254",
        ">= 128",
        "128 <= x <= 135",
        "chr(65 + …)",
    }
