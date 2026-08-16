"""Bambu Lab MQTT communication service.

IMPORTANT: Always use qos=1 for all MQTT publish calls!
The printer ignores qos=0 messages when busy broadcasting status updates.
Using qos=1 ensures the printer acknowledges and processes our commands immediately.
This was discovered when K-profile requests with qos=0 took 20-30 seconds,
but with qos=1 they respond instantly.
"""

import asyncio
import json
import logging
import os
import ssl
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from backend.app.services.hms_actions import HMSAction, get_actions_for_error_code
from backend.app.utils.timelapse import task_cfg

logger = logging.getLogger(__name__)


def _install_paho_suback_guard() -> None:
    """Guard paho's SUBACK handler against malformed (sub-2-byte) packets.

    A Bambu printer's broker occasionally delivers a truncated SUBACK whose
    body is shorter than the 2-byte packet identifier. paho-mqtt's
    ``_handle_suback`` then builds a ``struct`` format with a negative count
    (``f"!H{len-2}s"`` → ``"!H-1s"``) and ``struct.unpack`` raises
    ``struct.error: bad char in struct format`` on paho's network thread —
    killing that thread until BamDude's stale-connection watchdog reconnects
    ~70 s later. This wrapper drops the malformed SUBACK instead (equivalent
    to "no SUBACK arrived", which the subscribe path already tolerates) so
    the paho loop thread survives. Idempotent; best-effort — a paho API
    change just leaves the original behaviour in place.
    """
    try:
        original = mqtt.Client._handle_suback
    except AttributeError:
        return
    if getattr(original, "_bamdude_guarded", False):
        return

    def _guarded_handle_suback(self):  # type: ignore[no-untyped-def]
        packet = self._in_packet.get("packet", b"")
        if len(packet) < 2:
            logger.warning(
                "Dropping malformed SUBACK (%d-byte body, need >=2) — paho would "
                "otherwise crash its network thread on this packet",
                len(packet),
            )
            return None
        return original(self)

    _guarded_handle_suback._bamdude_guarded = True  # type: ignore[attr-defined]
    mqtt.Client._handle_suback = _guarded_handle_suback


def _install_paho_publish_guard() -> None:
    """Guard paho's PUBLISH handler against malformed (truncated) packets.

    The PUBLISH counterpart of :func:`_install_paho_suback_guard`. A Bambu
    printer's broker occasionally delivers a truncated PUBLISH whose declared
    topic length exceeds the bytes actually present. paho-mqtt's
    ``_handle_publish`` then builds a ``struct`` format with a negative count
    (``f"!{slen}s{len-slen}s"`` → ``"!120s-40s"``) and ``struct.unpack`` raises
    ``struct.error: bad char in struct format`` on paho's network thread —
    killing that thread until BamDude's stale-connection watchdog reconnects
    ~70 s later. This wrapper drops the malformed PUBLISH instead (the printer
    re-pushes its state within seconds) so the paho loop thread survives.
    Idempotent; best-effort — a paho API change just leaves the original
    behaviour in place.
    """
    try:
        original = mqtt.Client._handle_publish
    except AttributeError:
        return
    if getattr(original, "_bamdude_guarded", False):
        return

    def _guarded_handle_publish(self):  # type: ignore[no-untyped-def]
        packet = self._in_packet.get("packet", b"")
        # paho reads a 2-byte topic length, then unpacks slen topic bytes plus
        # the remainder. Either field going negative crashes struct.unpack.
        if len(packet) < 2:
            logger.warning(
                "Dropping malformed PUBLISH (%d-byte body, need >=2) — paho would "
                "otherwise crash its network thread on this packet",
                len(packet),
            )
            return None
        slen = (packet[0] << 8) | packet[1]
        if slen > len(packet) - 2:
            logger.warning(
                "Dropping malformed PUBLISH (topic len %d > %d remaining bytes) — "
                "paho would otherwise crash its network thread on this packet",
                slen,
                len(packet) - 2,
            )
            return None
        return original(self)

    _guarded_handle_publish._bamdude_guarded = True  # type: ignore[attr-defined]
    mqtt.Client._handle_publish = _guarded_handle_publish


_install_paho_suback_guard()
_install_paho_publish_guard()

# AMS module name prefixes used in get_version responses.
# The numeric suffix after '/' is the AMS unit ID as reported in push_status.
#   "ams/<id>"  – original AMS (X1C, X1E, P1S, …)
#   "n3f/<id>"  – AMS 2 Pro (H2D Pro and similar)
#   "n3s/<id>"  – AMS HT (H2D Pro and similar; IDs typically start at 128)
_AMS_MODULE_PREFIXES = ("ams/", "n3f/", "n3s/")

# The band of ``sequence_id`` values that means "this reply is to something WE
# sent". BS carves out the same one (``DevUtil.h``: ``STUDIO_START_SEQ_ID``
# 20000, ``STUDIO_END_SEQ_ID`` 30000) and tests membership with
# ``is_studio_cmd`` before acting on any reply — the topic is shared with the
# printer's own screen, the Bambu app and the cloud, so an untested reply is
# somebody else's command.
#
# We counted from 0, which is inside nobody's range and outside our own: a reply
# with ``sequence_id`` 5 could equally be ours or the screen's. The one place
# that needed the answer hardcoded a literal — ``project_file`` pins "20000" so
# a slicer-launched print can be told from ours — and 20000 is exactly
# ``STUDIO_START_SEQ_ID``. This generalises that single case into the rule it
# was always an instance of.
#
# 20000 itself stays reserved for that pin: the counter starts there and every
# command pre-increments, so no ordinary command can be mistaken for the
# project_file we sent.
#
# ⚠️ BS does NOT wrap — ``m_sequence_id`` is a static int incremented forever,
# so after 10 000 commands its own replies stop passing ``is_studio_cmd``. That
# is survivable in a desktop session and certain to happen in a server that
# stays up for weeks, so we wrap instead of copying it.
STUDIO_SEQ_START = 20000
STUDIO_SEQ_END = 30000

# BS ``DeviceManager.hpp``: the ``option`` bitmask on ``print_option`` has
# exactly one bit defined, and ``PRINT_OP_MAX`` follows it immediately.
PRINT_OP_AUTO_RECOVERY = 0

# gcode_state values that mean the printer is not idle and must not be handed a
# new start-print (#2598). Firmware rejects a project_file while busy with
# 0500_4004 "Device is busy and cannot start a new task", and on some models
# (A1 mini reported) that error cancels the RUNNING job. IDLE / FINISH / FAILED
# are valid start targets and are deliberately excluded. Mirrors
# printer_manager.ACTIVE_PRINT_STATES / print_scheduler._ACTIVE_PRINT_STATES /
# background_dispatch._ACTIVE_PRINT_STATES.
_ACTIVE_PRINT_STATES = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})


# ── paho's outgoing retry queue: the only two places we touch it ─────────────
# We publish with ``qos=1``, which is a promise — *at least once* — and paho
# keeps that promise by holding the message in ``Client._out_messages`` and
# re-sending it until the broker returns a PUBACK. The broker here is the
# printer's own firmware, and it loses PUBACKs (see the
# ``max_inflight_messages_set`` note below, #1164).
#
# ⚠️ An unacknowledged packet is NOT a lost one. Measured 2026-08-16: a plate
# sweep published at 02:06 executed on the printer, its receipt went missing,
# and paho re-delivered it at 16:42 — eleven hours into another print, which it
# swept off the bed. paho cannot tell "never arrived" from "arrived, receipt
# lost"; from the transport's side those are one case.
#
# ⚠️ There is no public API for this, and that is not an oversight: MQTT has no
# "withdraw", and QoS 1 exists to promise the opposite, so a public cancel
# would be a method for breaking the guarantee the API is for. Hence private
# state — kept behind these two functions so there is one place to fix when
# paho moves it, and pinned by a canary test.


def _drain_outgoing(client: object) -> int:
    """Forget every message paho is still trying to deliver. Returns the count."""
    pending = getattr(client, "_out_messages", None)
    if not isinstance(pending, dict) or not pending:
        return 0
    count = len(pending)
    pending.clear()
    return count


def _drop_queued_message(client: object, mid: int) -> bool:
    """Withdraw one message from the retry queue.

    ``False`` when it was already gone, which is the normal case: the PUBACK
    usually beats the printer's own acknowledgement.
    """
    pending = getattr(client, "_out_messages", None)
    if not isinstance(pending, dict):
        return False
    return pending.pop(mid, None) is not None


# ── A2L "AMS Lite" unit-id normalisation (upstream a2l-am-unit-16) ───────────
# The A2L reports its 4-slot AMS Lite as physical unit **id 16**, but the
# firmware is internally inconsistent about it:
#   - its tray bitmasks (tray_exist_bits etc.) sit at **bit base 24**, i.e. the
#     position for id 6 (6*4), NOT id 16 (which would be bit 64);
#   - it reports ``tray_now`` as a **local** 0-3 slot, not a global id;
#   - ``ams_mapping2`` and per-unit commands use the **physical** id 16.
# So we normalise 16 → 6 at the MQTT ingest boundary. Global tray ids then land
# at 24-27, which every ``ams_id*4+slot`` consumer handles unchanged, collides
# with nothing (regular AMS 0-15, AMS-HT 128-135, external 254/255) and passes
# the ``ams_id <= 7`` DB constraint. We translate 6 → 16 (and the local slot)
# back to the physical form ONLY on the outbound wire.
A2L_LITE_PHYSICAL_AMS_ID = 16
A2L_LITE_NORMALIZED_AMS_ID = 6
A2L_LITE_GLOBAL_BASE = A2L_LITE_NORMALIZED_AMS_ID * 4  # 24


def normalize_am_unit_id(ams_id: int) -> int:
    """Map the A2L AMS-Lite's physical unit id (16) to its normalised id (6).

    Self-scoping: only id 16 is remapped, and no other Bambu device reports an
    AMS unit at id 16 (regular AMS 0-3, AMS-HT 128-135). All other ids pass
    through untouched.
    """
    return A2L_LITE_NORMALIZED_AMS_ID if ams_id == A2L_LITE_PHYSICAL_AMS_ID else ams_id


def a2l_lite_wire_ids(ams_id: int, tray_id: int) -> tuple[int, int, int] | None:
    """Translate a normalised A2L slot back to the physical wire form.

    Returns ``(wire_ams_id, wire_slot_id, wire_global_tray)`` for the AMS-Lite
    (normalised id 6), else ``None`` for every other unit.

    CONFIRMED from the firmware's own ``ams_mapping2`` (``{ams_id:16,
    slot_id:0-3}``): the wire uses the physical unit id 16 with a **local** 0-3
    slot. NOT yet confirmed by capture: the physical **global** tray value some
    commands put on the wire (load ``target``, extrusion_cali ``tray_id``) — we
    extrapolate it as ``16*4+slot`` = 64-67 to stay consistent with the physical
    unit id. This is the single unverified encoding, and it lives only here.
    """
    if ams_id != A2L_LITE_NORMALIZED_AMS_ID:
        return None
    local_slot = tray_id % 4
    return (
        A2L_LITE_PHYSICAL_AMS_ID,
        local_slot,
        A2L_LITE_PHYSICAL_AMS_ID * 4 + local_slot,
    )


def apply_tray_exist_bits(
    units: list,
    tray_exist_bits_str: str | int | None,
    *,
    power_on_flag: bool = True,
    log_label: str | None = None,
    annotate_exists: bool = False,
) -> int:
    """Wipe stale per-tray filament fields on slots whose ``tray_exist_bits`` bit is 0.

    ``tray_exist_bits`` is firmware's canonical "which slots have a spool" bitmask
    (BambuStudio uses it too). For every slot whose bit is 0, promote the tray
    ``state`` to 9 (firmware's "no spool" code) and clear ``tray_type`` /
    ``tray_color`` / ``tray_info_idx`` / ``tag_uid`` / ``tray_uuid`` / ``remain``
    etc so downstream readers (BamDude's AMS card, the VP slicer-facing cache,
    inventory short-circuits keyed on ``state in {9, 10}``) all see one canonical
    empty-slot signal instead of guessing from payload shape (#1322, #147).

    Two callers share this helper to keep their views consistent:

    1. ``_handle_ams_data`` for BamDude's internal AMS state (printer card).
    2. ``virtual_printer.mqtt_bridge._on_printer_raw`` for the cached slicer-
       facing push_status (upstream Bambuddy #1726 — without this the VP would
       forward stale per-tray fields for empty slots, and BambuStudio's Sync
       would render phantom loaded slots).

    Skipped only on the printer-shutdown pattern: all-zero bits paired with
    ``power_on_flag=False`` (#765). Non-zero bits with ``power_on_flag=False``
    is valid idle-printer state (#1365 — X1C between prints) and MUST be applied
    so spool removal is detected without requiring a manual reconnect.

    AMS-HT units (``id`` 128-135) are single-tray dry boxes whose presence bit
    is packed as ONE consecutive bit starting at 16 (``16 + (ams_id - 128)``),
    not at ``ams_id * 4``. This bitmask is the ONLY reliable empty signal for an
    HT: it keeps echoing a stale ``tray_type`` after the spool is pulled, and its
    ``state`` is firmware-variant (loaded reports 11 on H2D, 9 on the firmware in
    upstream #2594). Addressing taken from OrcaSlicer's ``DevFilaSystem.cpp``
    (``is_exists = tray_exist_bits >> (16 + (ams_id - 128))``) and confirmed
    against a live H2D capture where HT-A is bit 16 — loaded ``0x10f7f``, empty
    ``0xf7f`` (upstream #2670). We have no AMS-HT here to re-derive it from.

    ``tray_exist_bits_str`` is expected as a hex string (firmware sends it that
    way). Ints are tolerated for defensive symmetry but typically not seen on
    the wire. ``None`` / empty / unparseable → no-op.

    ``annotate_exists`` writes a per-tray ``exists`` bool (from the bitmask) on
    every processed slot. This is firmware's authoritative "spool physically
    present" signal — the same one BambuStudio uses to draw a ``?`` for a
    non-RFID spool in an otherwise-unidentified slot. BamDude's AMS card keys
    empty-vs-unknown off it so a non-Bambu spool shows ``?`` instead of "Empty"
    (upstream #2527). Only the internal (printer-card) caller sets this; the VP
    bridge leaves it False so the ``exists`` key never reaches the slicer wire
    format.

    Mutates ``units`` in place. Returns the number of slots cleared.
    """
    if not tray_exist_bits_str:
        return 0
    try:
        if isinstance(tray_exist_bits_str, int):
            tray_exist_bits = tray_exist_bits_str
        else:
            tray_exist_bits = int(tray_exist_bits_str, 16)
    except (ValueError, TypeError):
        return 0
    if tray_exist_bits == 0 and not power_on_flag:
        return 0
    if not isinstance(units, list):
        return 0

    cleared = 0
    for ams_unit in units:
        if not isinstance(ams_unit, dict):
            continue
        ams_id_raw = ams_unit.get("id")
        if ams_id_raw is None:
            continue
        try:
            ams_id = int(ams_id_raw) if isinstance(ams_id_raw, str) else ams_id_raw
        except (ValueError, TypeError):
            continue
        if not isinstance(ams_id, int):
            continue
        # Fold the A2L AMS-Lite's physical id (16) onto its normalised one (6)
        # HERE rather than relying on the caller, because the two callers do not
        # agree: ``_handle_ams_data`` reads state normalised at ingest, while the
        # VP bridge does its own ``json.loads`` on the raw payload and still
        # holds 16. Without this the docstring's promise below — "the A2L-Lite,
        # normalised to id 6, lands at bits 24-27" — is simply untrue for one of
        # them, and that unit falls out of the range guard and gets no cleanup at
        # all: empty slots keep their stale filament and BambuStudio paints
        # phantom loaded spools through the VP (upstream Bambuddy #2699).
        ams_id = normalize_am_unit_id(ams_id)
        # AMS-HT (id 128-135) is a single-tray dry box whose presence bit is ONE
        # consecutive bit starting at 16 — ``16 + (ams_id - 128)`` — not
        # ``ams_id * 4``, which would overflow past bit 512. Regular AMS (and the
        # A2L-Lite, normalised to id 6, which lands at bits 24-27 through the
        # ordinary formula) keep ``ams_id * 4 + tray_id``.
        #
        # Anything outside those two ranges has no layout we know, so it is
        # skipped rather than guessed at.
        is_ht = 128 <= ams_id <= 135
        if not is_ht and not (0 <= ams_id <= 15):
            continue
        for tray in ams_unit.get("tray", []):
            if not isinstance(tray, dict):
                continue
            tray_id_raw = tray.get("id")
            if tray_id_raw is None:
                continue
            try:
                tray_id = int(tray_id_raw) if isinstance(tray_id_raw, str) else tray_id_raw
            except (ValueError, TypeError):
                continue
            if not isinstance(tray_id, int):
                continue
            global_bit = (16 + (ams_id - 128)) if is_ht else (ams_id * 4 + tray_id)
            slot_exists = (tray_exist_bits >> global_bit) & 1
            if annotate_exists:
                tray["exists"] = bool(slot_exists)
            if slot_exists:
                continue
            tray["state"] = 9
            if tray.get("tray_type"):
                if log_label:
                    logger.debug(
                        f"[{log_label}] Clearing empty slot: AMS {ams_id} slot {tray_id} "
                        f"(tray_exist_bits bit {global_bit} = 0)"
                    )
                tray["tray_type"] = ""
                tray["tray_sub_brands"] = ""
                tray["tray_color"] = ""
                tray["tray_id_name"] = ""
                tray["tag_uid"] = "0000000000000000"
                tray["tray_uuid"] = "00000000000000000000000000000000"
                tray["tray_info_idx"] = ""
                tray["remain"] = 0
                cleared += 1
    return cleared


@dataclass
class MQTTLogEntry:
    """Log entry for MQTT message debugging."""

    timestamp: str
    topic: str
    direction: str  # "in" or "out"
    payload: dict


# BS ``HMSMessageLevel`` (DeviceCore/DevHMS.h). **Lower is worse**, and 0 means
# "the printer sent a level we do not recognise" — not "harmless".
HMS_LEVEL_UNKNOWN = 0
HMS_LEVEL_FATAL = 1
HMS_LEVEL_SERIOUS = 2
HMS_LEVEL_COMMON = 3
HMS_LEVEL_INFO = 4
_HMS_LEVEL_MAX = 5  # BS HMS_MSG_LEVEL_MAX — the exclusive bound on a valid level

# Faults at or below this rank are worth waking somebody for. Used by the
# notification filter; the frontend applies the same ``<= 2`` for its red pip.
HMS_SEVERITY_NOTIFY_THRESHOLD = HMS_LEVEL_SERIOUS

# Actions that send the printer nothing — the printer's own screen owns them, and
# BS treats them the same way. Exported because the HTTP route must NOT run its
# "did the printer answer?" probe for these: with no publish there is no pushall,
# so the probe times out and reports a failure for something that was never a
# transmission.
#
# ``REMOVE_CLOSE_BTN`` is not even a button. BS's ``DeviceErrorDialog.hpp`` marks
# it *"special case, do not show close button"* — a dialog modifier that hides the
# close affordance, which is why it must never be rendered as one.
HMS_UI_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "CHECK_ASSISTANT",
        "JUMP_TO_LIVEVIEW",
        "OK_JUMP_RACK",
        "REMOVE_CLOSE_BTN",
        "LOAD_VIRTUAL_TRAY",
        "CANCLE",
        "DBL_CHECK_CANCEL",
    }
)


# BS ``DevStorage::SdcardState`` (DeviceCore/DevStorage.h).
SDCARD_NONE = 0
SDCARD_NORMAL = 1
SDCARD_ABNORMAL = 2
SDCARD_READONLY = 3


def parse_hex_bitfield(value: object) -> int | None:
    """The ``cfg`` / ``fun`` / ``aux`` / ``stat`` quartet arrive as hex STRINGS
    on new-protocol printers and as plain ints on some builds. ``None`` when the
    field is absent or unparseable — distinct from 0, which is "reported, all
    bits clear"."""
    if value is None:
        return None
    try:
        return value if isinstance(value, int) else int(str(value), 16)
    except (ValueError, TypeError):
        return None


def _hms_severity_from_code(code: int) -> int:
    """BS ``DevHMSItem::parse`` — ``msg_level_int = code >> 16``, clamped.

    BS falls back to ``HMS_UNKNOWN`` (0) for an out-of-range level. We fall back
    to **SERIOUS** instead, deliberately: 0 renders as the quietest colour in
    our own severity map, so an unrecognised level would make an unrankable
    fault the least visible thing on the page. A fault we cannot rank is not a
    fault we can afford to whisper.
    """
    level = (code >> 16) & 0xFFFF
    if 0 < level < _HMS_LEVEL_MAX:
        return level
    return HMS_LEVEL_SERIOUS


_SECRET_KEYS = frozenset({"password", "passwd", "access_code", "token", "bind_code", "secret", "key"})


def _loggable(payload: dict, _limit: int = 2000) -> str:
    """A command payload as text, with anything credential-shaped masked.

    ⚠️ A printer payload is not automatically safe to write down. ``url`` carries
    userinfo on some transfer commands, and an operator pasting a debug log into
    an issue would be handing over their printer with it. Masked by key name
    rather than by value shape: a heuristic that decides what *looks* like a
    secret fails silently on the one that does not.

    Truncated, because a single ``pushall`` response is tens of kilobytes and
    would bury the thing the log is being read for.
    """

    def clean(value: object) -> object:
        if isinstance(value, dict):
            return {k: ("***" if k.lower() in _SECRET_KEYS else clean(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str) and "://" in value and "@" in value:
            scheme, _, rest = value.partition("://")
            return f"{scheme}://***@{rest.rpartition('@')[2]}"
        return value

    try:
        text = json.dumps(clean(payload), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(payload)
    return text if len(text) <= _limit else f"{text[:_limit]}… ({len(text)} chars)"


def _print_error_severity(error: int) -> int:
    """Rank a ``print_error`` from the top nibble of its low half.

    ⚠️ **This is ours, not BambuStudio's.** BS assigns ``print_error`` no level
    at all — it looks the code up and shows a dialog
    (``DeviceErrorDialog.cpp``), so there is no parity to copy here. The mapping
    below is this repo's own long-standing reading of the code space,
    documented beside the ``< 0x4000`` filter in ``_update_state``:
    ``0x4xxx`` fatal, ``0x8xxx`` warning, ``0xCxxx`` prompt.

    It replaces a hardcoded COMMON, which ranked a fatal print error the same as
    a prompt. Anything outside the three known prefixes keeps that old constant,
    because an unfamiliar shape is exactly where a guessed rank would be wrong.
    """
    nibble = (error >> 12) & 0xF
    if nibble == 0x4:
        return HMS_LEVEL_FATAL
    if nibble == 0x8:
        return HMS_LEVEL_COMMON
    if nibble == 0xC:
        return HMS_LEVEL_INFO
    return HMS_LEVEL_COMMON


@dataclass
class HMSError:
    """Health Management System error from printer."""

    code: str
    attr: int  # Attribute value for constructing wiki URL
    module: int
    severity: int  # 1=fatal, 2=serious, 3=common, 4=info
    message: str = ""
    # User-facing remediation actions from the bundled HMS catalog (e.g. "RESUME_PRINTING",
    # "CHECK_ASSISTANT"). Defaults to an empty list rather than None so the field always
    # satisfies HMSErrorResponse.actions: list[str] — a code path that builds an HMSError
    # without explicitly passing actions can't silently land None on the schema boundary.
    actions: list[str] = field(default_factory=list)
    # The `subtask_id` snapshotted from PrinterState when this error surfaced; Bambu's
    # HMS-aware commands echo it back as `job_id`. None for idle errors with no job.
    job_id: str | None = None
    # Canonical hex identifier for the firmware's `err` matching: 16 chars for the
    # 64-bit `hms[]` array path (`f"{attr:08X}{code:08X}"`), 8 chars for the 32-bit
    # `print_error` path. The frontend echoes this back to execute_hms_action; the
    # truncated 8-char short code the firmware silently rejects on H2C and hms[]-sourced
    # faults (#1830).
    full_code: str = ""

    @property
    def short_code(self) -> str:
        """``MMMM_EEEE`` — the lossy form the catalogue and the printer's own
        screen are keyed by (e.g. ``0300_8004``).

        Reconstructed rather than stored because both producing branches already
        derive it the same way: the ``hms[]`` path puts the module in
        ``attr``'s high half, and the ``print_error`` path stores the whole
        32-bit value in ``attr`` — whose high half is, again, the module.

        It is a property because this formula had grown **three** copies (the
        parser, the duplicate check, the WebSocket payload) and was about to
        grow a fourth in the pause classifier. Three copies of a lossy
        conversion is how one of them ends up subtly different.
        """
        try:
            error = int(self.code.replace("0x", ""), 16) if self.code else 0
        except ValueError:
            error = 0
        return f"{(self.attr >> 16) & 0xFFFF:04X}_{error & 0xFFFF:04X}"


# HMS short codes the firmware emits during normal user-cancel sequences.
# These aren't faults — they're status echoes that confirm the cancel happened.
# Filtering them at parse-time keeps them out of state.hms_errors entirely,
# so they don't drive the printer card's "X problem" badge, the red pip, or
# any other consumer that treats hms_errors as the active-fault list.
_HMS_USER_ACTION_CODES: frozenset[str] = frozenset(
    {
        "0300_400C",  # "The task was canceled."
        "0500_400E",  # "Printing was cancelled."
    }
)

# "Device is busy and cannot start new task." The firmware's refusal of a
# start-print it will not accept — see _ACTIVE_PRINT_STATES above, which exists
# because of this very code. It is a rejected request, not a fault of the
# machine, and while a print is RUNNING it is doubly harmless: nothing of ours
# is asking (the scheduler refuses a busy printer first), so it is an echo of
# something the cloud, Handy or a stray reconnect asked for.
#
# ⚠️ NOT added to _HMS_USER_ACTION_CODES, which drops codes unconditionally.
# This one is dropped ONLY mid-print. Idle, the same code means a start-print
# was refused and the operator has every reason to see it.
#
# ⚠️ Left on the printer it is not merely noise: on some models (A1 mini
# reported) an uncleared 0500_4004 cancels the RUNNING job, which is why the
# guard also clears it rather than only hiding it.
_DEVICE_BUSY_CODE = "0500_4004"

# The printer repeats print_error in every push_status until it is cleared, so
# an unacknowledged clean_print_error would be re-sent about once a second.
_DEVICE_BUSY_CLEAR_INTERVAL = 30.0


# "MQTT command verification failed" — the printer's authorization check
# refusing a control command it could not verify (firmware >= 01.08.03.00beta /
# 01.08.05.00). Queries (get_version, extrusion_cali_get, pushall) still answer,
# so the connection looks perfectly healthy while project_file, gcode_line and
# ams_change_filament are all silently dropped — which is exactly how it
# presents: the upload succeeds, the printer echoes our subtask_id, then it sits
# at IDLE forever (upstream Bambuddy #2732).
#
# The 16-char form is load-bearing. This code's meaning lives in ``attr``'s low
# half (0500) and ``code``'s high half (0001); the MMMM_EEEE short code collapses
# it to "0500_0007", which matches nothing in any catalog.
HMS_MQTT_VERIFY_FAILED: str = "0500050000010007"


@dataclass
class KProfile:
    """Pressure advance (K) calibration profile from printer."""

    slot_id: int
    extruder_id: int
    nozzle_id: str
    nozzle_diameter: str
    filament_id: str
    name: str
    k_value: str
    n_coef: str = "0.000000"
    ams_id: int = 0
    tray_id: int = -1
    setting_id: str | None = None


# Mirror BS DevNozzleSystem.cpp:609-647 — canonical names from PrintConfig.hpp:303-319.
# Long-form names the firmware emits on P1/A1/A1mini for older protocol versions.
_NOZZLE_FULL_NAME_MAP: dict[str, str] = {
    "undefine": "undefine",
    "hardened_steel": "hardened_steel",
    "stainless_steel": "stainless_steel",
    "tungsten_carbide": "tungsten_carbide",
    "brass": "brass",
    "E3D": "E3D",
}

# 4-char-code material codes (positions 2-3).
_NOZZLE_MATERIAL_CODE_MAP: dict[str, str] = {
    "00": "stainless_steel",
    "01": "hardened_steel",
    "05": "tungsten_carbide",
}

# 4-char-code flow letters (position 1), copied from BS ``_str2_nozzle_flow_type``.
#
# ⚠️ **``E`` is plain High Flow and ``B`` is the E3D one** — not the other way
# round, which is what the letter shapes suggest. BS maps ``E -> H_FLOW`` and
# ``B -> E_FLOW``, and we were missing ``B`` entirely: an ``HB00`` nozzle fell
# through to the default and was labelled Standard, so touching the field in the
# UI wrote back a flow class the nozzle does not have.
_NOZZLE_FLOW_LETTER_MAP: dict[str, str] = {
    "S": "standard",
    "H": "high_flow",
    "A": "standard",
    "X": "standard",
    "E": "high_flow",
    "U": "tpu_high_flow",
    "B": "e3d_high_flow",
}


def _parse_nozzle_type(raw: str | None) -> tuple[str, str]:
    """Decode the printer's nozzle_type into (material, flow).

    Mirrors BS DevNozzleSystem.cpp::s_parse_nozzle_type. Three supported
    formats: canonical long names ("stainless_steel"), 4-char codes
    ("HS00"), and "N/A". Returns ("", "") when the value is empty or
    unrecognized so callers can leave NozzleInfo defaults intact.

    The device's ``nozzle_type`` is the short code; a calibration ``nozzle_id``
    is the long form ("HS00-0.4"). Both are read by the same slice positions,
    which is why the length check is ``>= 4`` and not ``== 4``.
    """
    if not raw:
        return "", ""
    s = str(raw).strip()
    if not s:
        return "", ""
    if s in _NOZZLE_FULL_NAME_MAP:
        return _NOZZLE_FULL_NAME_MAP[s], ""
    if s == "N/A":
        return "undefine", ""
    if len(s) >= 4:
        flow = _NOZZLE_FLOW_LETTER_MAP.get(s[1:2], "")
        material = _NOZZLE_MATERIAL_CODE_MAP.get(s[2:4], "")
        return material, flow
    return "", ""


@dataclass
class NozzleInfo:
    """Nozzle hardware configuration.

    Stores the *decoded* values per BS — ``nozzle_type`` is the canonical
    material name ("stainless_steel" / "hardened_steel" / ...), not the
    raw 4-char code. ``nozzle_flow`` is the parsed flow class
    ("standard" / "high_flow" / "tpu_high_flow" / "e3d_high_flow"). Diameter stays as the
    string the firmware reported.
    """

    nozzle_type: str = ""
    nozzle_flow: str = ""
    nozzle_diameter: str = ""


@dataclass
class ModuleInfo:
    """One entry from the get_version module list — the printer body or an
    accessory (AMS unit, filament buffer/hub, exhaust fan, …). Populated in
    ``_handle_version_info``; only modules with a display name (``product_name``)
    are kept. Surfaced read-only in the Printer Settings → Add-ons tab.
    """

    name: str = ""  # module name, e.g. "ota", "n3f/0", "ahb", "eef"
    product_name: str = ""  # e.g. "Bambu Lab X2D", "AMS 2 Pro (1)"
    hw_ver: str = ""
    sw_ver: str = ""
    serial: str = ""


@dataclass
class ExtrusionCaliResult:
    """One row from push ``extrusion_cali_get_result`` (X1 auto-cali path).

    PrinterState.extrusion_cali_results accumulates these between
    ``extrusion_cali_start`` and the matching done-event. UI drains the
    list when the user clicks Save on the auto-cali results page.
    """

    tray_id: int = 0
    ams_id: int = 0
    slot_id: int = 0
    extruder_id: int = 0
    nozzle_diameter: float = 0.4
    nozzle_volume_type: str = "standard"
    filament_id: str = ""
    setting_id: str = ""
    k_value: float = 0.0
    n_coef: float = 0.0
    confidence: int = -1
    nozzle_pos_id: int = -1
    nozzle_sn: str = ""


@dataclass
class PACalibHistoryEntry:
    """One row from push ``extrusion_cali_get`` (printer-side 16-slot history).

    Pulled on-demand by the History modal via ``extrusion_cali_query_history``
    so operators can see / pick / delete entries the printer firmware stored.
    """

    cali_idx: int = -1
    name: str = ""
    filament_id: str = ""
    setting_id: str = ""
    nozzle_diameter: float = 0.4
    nozzle_volume_type: str = "standard"
    extruder_id: int = 0
    k_value: float = 0.0
    n_coef: float = 0.0


@dataclass
class FilaSwitchState:
    """Filament Track Switch (FTS) accessory state.

    The FTS is an external accessory that mediates filament routing between an
    AMS and the printer's extruders. When installed, the AMS no longer has a
    fixed extruder assignment — any slot can be routed to any extruder via the
    track switch. Detected from print.device.fila_switch in MQTT. Upstream
    Bambuddy #1162.
    """

    installed: bool = False
    # in[track] = currently loaded slot for that track (-1 = empty). The slot
    # value is reported as observed in MQTT (treated as a global tray ID).
    in_slots: list[int] = field(default_factory=list)
    # out[track] = extruder this track terminates at (0 = right/main, 1 = left)
    out_extruders: list[int] = field(default_factory=list)
    stat: int = 0  # status flags (0 = idle)
    info: int = 0  # info flags


@dataclass
class PrintOptions:
    """AI detection and print options from xcam data."""

    # Core AI detectors
    spaghetti_detector: bool = False
    print_halt: bool = False
    halt_print_sensitivity: str = "medium"  # Spaghetti sensitivity
    first_layer_inspector: bool = False
    printing_monitor: bool = False  # AI print quality monitoring
    buildplate_marker_detector: bool = False
    allow_skip_parts: bool = False
    # Additional AI detectors - decoded from cfg bitmask
    nozzle_clumping_detector: bool = True
    nozzle_clumping_sensitivity: str = "medium"
    pileup_detector: bool = True
    pileup_sensitivity: str = "medium"
    airprint_detector: bool = True
    airprint_sensitivity: str = "medium"
    auto_recovery_step_loss: bool = True  # Uses print.print_option command
    filament_tangle_detect: bool = False
    # New flags added for the Printer Settings dialog. Each is bool|None
    # (None = "printer hasn't reported"); the rest of the dataclass uses
    # plain bool defaults — we keep the original defaults intact and only
    # surface None for the new ones via the API.
    nozzle_blob_detect: bool | None = None
    sound_enable: bool | None = None
    save_remote_to_storage: int | None = None
    air_purification: int | None = None  # 0 Off / 1 Inside / 2 Outside
    # Open-door detection setting (BS DoorOpenCheckState): 0 Disable / 1 Notification
    # (warning) / 2 Pause print. Read from cfg bits 20-21; written via system/set_door_stat.
    open_door_check: int | None = None
    # Idle heating protection (BS): 0 Off / 1 On / 2 Unavailable (read-only — the
    # printer's heating-maintenance function is active). Read from cfg bits 32-33.
    idle_heating_protect: int | None = None
    # Live capability bits for the Safety tab (fun bit 12 / bit 62).
    support_open_door: bool = False
    support_idle_heating: bool = False
    plate_type_detect: bool | None = None  # build_plate_marker_detect echo
    plate_align_check: bool | None = None
    snapshot_enabled: bool | None = None
    fod_check: bool | None = None
    displacement_detection: bool | None = None
    # Phase-3b BS-parity rows (unverified — no local hardware reports these)
    nozzle_blob_v2: int | None = None  # smart nozzle blob: 0 off / 1 on / 2 auto (cfg 43-44)
    air_print_nonvisual: bool | None = None  # sensor air-print (home_flag bit 28)
    ai_monitoring_sensitivity: str | None = None  # legacy AI monitoring (cfg 13-14)


@dataclass
class PrinterState:
    connected: bool = False
    state: str = "unknown"
    current_print: str | None = None
    subtask_name: str | None = None
    progress: float = 0.0
    remaining_time: int = 0
    layer_num: int = 0
    total_layers: int = 0
    temperatures: dict = field(default_factory=dict)
    raw_data: dict = field(default_factory=dict)
    gcode_file: str | None = None
    subtask_id: str | None = None
    hms_errors: list = field(default_factory=list)  # List of HMSError
    kprofiles: list = field(default_factory=list)  # List of KProfile
    # BS ``DevStorage::SdcardState`` — four states, not a bool:
    #   0 NO_SDCARD · 1 HAS_SDCARD_NORMAL · 2 HAS_SDCARD_ABNORMAL · 3 HAS_SDCARD_READONLY
    # New-protocol printers report it in ``aux`` bits 12-13; legacy ones send a
    # ``sdcard`` bool (BS maps that to NORMAL / NO_SDCARD).
    sdcard_state: int = 0
    # "A card we can actually write a print to", i.e. NORMAL only. ⚠️ This used
    # to be a substring test — ``"HAS_SDCARD" in value`` — so ABNORMAL and
    # READONLY both read as a healthy card, and the firmware-upload gate happily
    # sent a .bin to a card that could not take it.
    sdcard: bool = False
    store_to_sdcard: bool = False  # Store sent files on SD card (home_flag bit 11)
    timelapse: bool = False  # Timelapse recording active
    ipcam: bool = False  # Live view / camera streaming enabled
    wifi_signal: int | None = None  # WiFi signal strength in dBm
    wired_network: bool = False  # Ethernet connection detected (home_flag bit 18)
    door_open: bool = False  # Enclosure door open (home_flag bit 23 on X1 family, stat bit 23 on X2D)
    # Last classified pause reason — populated by main.on_printer_status_change
    # on the RUNNING→PAUSE edge using ``hms_errors.classify_pause_reason``,
    # cleared back to ``None`` on the PAUSE→RUNNING edge. Surfaces in the
    # status snapshot so frontend can render the cause inline without
    # re-running the HMS table lookup. ``str | None`` not enum since the set
    # of keys is owned by ``hms_errors.PAUSE_REASON_LABELS``.
    pause_reason: str | None = None
    pause_reason_label: str | None = None  # Human-readable text matching the key
    # Wall-clock (epoch seconds, ``time.time()``) at which the current pause
    # started. Populated by ``main._handle_pause_edge`` on the RUNNING→PAUSE
    # transition, cleared back to ``None`` on PAUSE→RUNNING. Surfaces in the
    # status snapshot so the frontend live-counter ("Paused 14 min") survives
    # an F5 / page navigation — without it, the counter would reset to zero
    # on every snapshot poll because there's no other authoritative source
    # for "when did this pause start".
    pause_started_at: float | None = None
    # Nozzle hardware info (for dual nozzle printers, index 0 = left, 1 = right)
    nozzles: list = field(default_factory=lambda: [NozzleInfo(), NozzleInfo()])
    # Module inventory (printer body + accessories) from get_version — Add-ons tab
    modules: list = field(default_factory=list)
    # Per-option print-option support, mirrored from BS DevPrintOptionsParser
    # (home_flag / xcam / cfg / fun / fun2 / named support bools). Populated as
    # messages arrive; compute_printer_supports gates each row on it.
    print_option_support: dict = field(default_factory=dict)
    # What the machine says its heaters will accept. Each is optional because
    # "did not report" is a real answer with its own fallback — see
    # ``utils.temperature_limits``, which owns the precedence.
    nozzle_temp_range: list | None = None
    bed_temp_range: list | None = None
    bed_temperature_limit: int | None = None
    # ⚠️ Mains voltage, ``home_flag`` bit 3. It LOWERS the bed ceiling on the X1
    # and O series (110 instead of 120), which reads backwards until you take it
    # as a fact about the heating element rather than about available power.
    is_220v: bool = False
    # Which axes the printer says are homed — ``home_flag`` bits 0/1/2 (BS
    # ``DevAxis::IsAxisAtHomeX/Y/Z``). Keyed "x"/"y"/"z".
    #
    # ⚠️ **A ``home_flag`` of exactly 0 means all three ARE home**, not none.
    # BS spells it ``m_home_flag == 0 ? true : (m_home_flag & 1) == 1`` — zero is
    # the "nothing reported" sentinel, and reading it as "not homed" would lock
    # every printer that omits the field out of jogging entirely.
    axis_at_home: dict = field(default_factory=lambda: {"x": True, "y": True, "z": True})
    # BS ``check_enable_np``: the print payload carries all four of ``cfg``,
    # ``fun``, ``aux`` and ``stat``. It is how BS decides a machine speaks the
    # new protocol, and it gates the per-extruder ``set_extrusion_length``.
    enable_np: bool = False
    # The two live halves of BS ``MachineObject::is_nozzle_flow_type_supported``
    # (``DeviceManager.hpp:336`` — ``is_enable_np | has_extra_flow_type``), which
    # decides whether a K-profile's Standard / High Flow choice means anything on
    # this machine. See ``printer_configs.supports_nozzle_flow_type``.
    #
    # STICKY: once observed they stay true. BS re-evaluates ``is_enable_np`` on
    # each full parse, but our pushes are frequently partial — a status frame
    # carrying only ``gcode_state`` would otherwise retract a capability the
    # printer has, and the field would flicker in the UI as frames arrive.
    # A capability is a property of the printer, not of one message.
    #
    # ``is_enable_np`` — the push carries the new-protocol quartet
    # (``check_enable_np``, ``DeviceManager.cpp:4280``).
    enable_np: bool = False
    # ``has_extra_flow_type`` — a nozzle frame that also carried ``flag3``
    # (``DeviceManager.cpp:3314-3321``).
    has_extra_flow_type: bool = False
    # AI detection and print options
    print_options: PrintOptions = field(default_factory=PrintOptions)
    # Calibration stage tracking (from stg_cur and stg fields)
    stg_cur: int = -1  # Current stage index (-1 = not calibrating)
    stg: list = field(default_factory=list)  # List of stages to execute
    # Air conditioning mode (0=cooling, 1=heating)
    airduct_mode: int = 0
    # ``device.airduct.subMode`` — BS ``m_sub_mode``. On the X2D the SAME part id
    # is a different fan depending on it (part 10 in cooling: 0 → Right(Aux),
    # 1 → Right(Filter)), so a label derived without it is a guess.
    airduct_sub_mode: int = -1
    # Print speed level (1=silent, 2=standard, 3=sport, 4=ludicrous)
    speed_level: int = 2
    # Chamber light on/off
    chamber_light: bool = False
    # Active extruder for dual nozzle (0=right, 1=left) - from device.extruder.info[X].hnow
    active_extruder: int = 0
    # Currently loaded tray (global ID): 254/255 = external spools, 255 = no filament on legacy printers
    tray_now: int = 255
    # Firmware's target/previous tray as reported in print.ams (RAW, not globalised):
    #   tray_tar = the slot the paused/loading print now expects
    #   tray_pre = the slot that was loaded before (e.g. the one that ran out)
    # For a single regular AMS these equal the global tray ID; for multi-AMS they
    # are local slot IDs (0-3) that must be resolved against the mapping field, and
    # for AMS-HT they are already global (128-135). 255 = none/idle, 254 = external.
    # Surfaced during a runout PAUSE so the UI can name the expected slot
    # (upstream #2587) — see printer_manager.resolve_expected_tray.
    tray_tar: int = 255
    tray_pre: int = 255
    # Last valid tray_now (0-253) - survives unload (255) for usage tracking after print completes
    last_loaded_tray: int = -1
    # Pending load target - used to track what tray we're loading for H2D disambiguation
    pending_tray_target: int | None = None
    # AMS status for filament change tracking (from print.ams.ams_status field)
    # ams_status is a combined value: lower 8 bits = sub status, bits 8-15 = main status
    # Main status: 0=idle, 1=filament_change, 2=rfid_identifying, 3=assist, 4=calibration, etc.
    ams_status: int = 0
    ams_status_main: int = 0  # (ams_status >> 8) & 0xFF
    ams_status_sub: int = 0  # ams_status & 0xFF
    # mc_print_sub_stage - filament change step indicator from print.mc_print_sub_stage
    # Used by OrcaSlicer/BambuStudio to track progress during filament load/unload
    mc_print_sub_stage: int = 0
    # AMS mapping for dual nozzle: which slot is active (from ams.ams_exist_bits/tray_exist_bits)
    ams_mapping: list = field(default_factory=list)
    # Per-AMS extruder map: {ams_id: extruder_id} where 0=right/main, 1=left/deputy
    ams_extruder_map: dict = field(default_factory=dict)
    # ---------- AMS system-level user settings (BS "AMS Settings" dialog) ----------
    # Each flag mirrors the corresponding push field from print.ams (insert_flag,
    # power_on_flag, calibrate_remain_flag) and the cfg bitfield (auto_switch
    # bit 10 X1 / bit 18 P1+A1, air-print echo). None means "printer hasn't
    # reported it yet" — distinct from False ("printer says off").
    ams_insertion_update: bool | None = None
    ams_power_on_update: bool | None = None
    ams_remain_capacity: bool | None = None
    ams_auto_switch_filament: bool | None = None
    ams_air_print_detect: bool | None = None
    # ---------- AMS firmware switch (BS DevAmsSystemFirmwareSwitch) ----------
    # The A1's AMS carries two firmware "personalities" and can reflash between
    # them. Everything here comes from ``print.upgrade_state.mc_for_ams_firmware``
    # — BS never hardcodes the names, and neither do we: the device reports its
    # own list, and an id whose meaning we invented is how you reflash an AMS
    # into the wrong personality.
    #
    # ``ams_firmwares`` is BS's ``m_firmwares`` map flattened to a list, ordered
    # by id: [{"id": int, "name": str, "version": str}, …]. Empty means the
    # printer has not offered a switch, which is BS's whole support test
    # (``SupportSwitchFirmware() = !m_firmwares.empty()``).
    ams_firmwares: list = field(default_factory=list)
    # ``current_run_firmware_id`` — what the AMS is running now. BS's IDX_DC
    # (-1) means "not reported"; we use None for the same thing.
    ams_firmware_idx_run: int | None = None
    # ``current_firmware_id`` — what is selected, i.e. what runs after a switch
    # completes. Differs from _run only mid-switch.
    ams_firmware_idx_sel: int | None = None
    # Raw ``status`` string. BS treats exactly "SWITCHING" as in-progress
    # (``IsSwitching()``) and hides the picker while it holds.
    ams_firmware_status: str | None = None
    # ``print.upgrade_state.status`` — the PRINTER's own firmware flash, not the
    # AMS one above. BS's ``is_in_upgrading()`` is this string being one of five
    # values (DevUpgrade.cpp): DOWNLOADING, FLASHING, UPGRADE_REQUEST,
    # PRE_FLASH_START, PRE_FLASH_SUCCESS. Used to refuse an AMS reflash while
    # the printer is already flashing something.
    firmware_upgrade_status: str | None = None
    # ⚠️ Two print-blocking firmware states, both from ``print.upgrade_state``
    # and both arriving over plain LAN MQTT. ``consistency_request`` is a module
    # version mismatch — the printer refuses to print until it is repaired, and
    # an SD-card update is a way to end up there. Neither was read at all, so a
    # printer in either state looked ordinary and simply took no work.
    firmware_consistency_request: bool = False
    firmware_force_upgrade: bool = False
    # ``device.extruder.info[].info`` bit 1 — filament present in that extruder
    # (BS ``DevExtruderSystem.cpp``: ``m_ext_has_filament = get_flag_bits(info, 1)``).
    # Keyed by extruder id. BS refuses an AMS firmware switch while any is loaded.
    ext_has_filament: dict = field(default_factory=dict)
    # Same word, bit 3 — a hotend is physically fitted (BS ``m_has_nozzle``).
    # ⚠️ An absent entry means "this machine cannot tell", not "no hotend": BS
    # initialises the field to true precisely because the A and P series have no
    # such detection. Only an entry that exists and says False may refuse a
    # heat request.
    ext_has_nozzle: dict = field(default_factory=dict)
    # BS ``m_has_timelapse_kit`` (``aux`` bit 26) — the add-on that gives a
    # machine somewhere to write a timelapse without an SD card.
    has_timelapse_kit: bool = False
    # ``device.cam.tl_*_kb`` — free and total space on each timelapse target.
    # Absent keys mean the camera never reported, which is NOT the same as zero
    # free: BS only warns on a value it actually has (``free_kb >= 0``).
    timelapse_storage: dict = field(default_factory=dict)
    # Absolute path of the last finished recording, as the printer reports it
    # (``device.cam.timelapse_path``). Empty while a print is running.
    timelapse_path: str = ""
    # Hold-timer: when we publish an AMS setting command we stamp the flag
    # name here; the push parser skips overwriting the corresponding field
    # while ``time.time() - hold < 3.0``. Avoids the toggle visually flipping
    # back during the half-second printer-confirms-the-change round-trip.
    ams_settings_hold: dict = field(default_factory=dict)  # flag_name -> epoch_seconds
    # Hold-timer for Printer Settings dialog. Same 3 s TTL pattern as
    # ams_settings_hold — keys are flag names ("auto_recovery",
    # "sound_enable", "purify_air", "open_door", "spaghetti_detector", …)
    # mapped to epoch_seconds. Push parser ignores echoes for a key while
    # the hold is active.
    printer_settings_hold: dict = field(default_factory=dict)
    # ---------- Filament Calibration (m062 / Plan 1) ----------
    # Push parser of `print.command=extrusion_cali_get_result` accumulates one
    # ExtrusionCaliResult per filament here. Drained by CalibrationService
    # when the operator clicks Save on the auto-cali results page.
    extrusion_cali_results: list = field(default_factory=list)
    # MQTT sequence_id from the active extrusion_cali_start publish. Helps
    # the parser correlate `print.gcode_file=auto_cali_for_user*` events
    # with our session.
    extrusion_cali_session_id: str | None = None
    # idle | running | completed | failed — drives wizard step transitions
    extrusion_cali_status: str = "idle"
    # 16-slot PA history pulled from `print.command=extrusion_cali_get`.
    # Refreshed on demand from the History modal; consumed read-only.
    extrusion_cali_history: list = field(default_factory=list)
    # Capability flags from printer push.func bitfield + cfg overrides
    is_support_pa_calibration: bool = False
    is_support_auto_flow_calibration: bool = False
    # Live device-calibration support flags (Device page → Calibration dialog),
    # keyed by the MQTT field name (support_lidar_calibration,
    # support_nozzle_offset_calibration, support_high_tempbed_calibration,
    # support_clump_position_calibration, support_motor_noise_cali,
    # support_ai_monitoring, support_bed_leveling). Only the keys the printer
    # actually reported are present; the API resolver merges these OVER the
    # per-model base matrix (hybrid gating, mirrors BambuStudio).
    device_cali_support: dict = field(default_factory=dict)
    # Filament Track Switch (FTS) accessory — when installed, AMS info reports
    # bits 8-11 = 0xE (uninitialized) because routing is dynamic. Upstream #1162.
    fila_switch: "FilaSwitchState" = field(default_factory=lambda: FilaSwitchState())
    # Plate dispatched by BamDude for the current print (#1166). Some firmware
    # versions (P1S 01.10.00.00) only put the .3mf filename in
    # ``print.gcode_file``, so the regex used to derive the plate number from
    # the path always falls back to plate 1 — and the printer card shows the
    # wrong thumbnail. When BamDude dispatches the print itself we know the
    # plate authoritatively; we record it here and prefer it over the
    # ``gcode_file`` regex. The subtask field guards against staleness: if the
    # printer is currently running a different subtask (e.g. a Studio-direct
    # dispatch on the same machine), these values are ignored. Cleared on
    # disconnect.
    dispatched_plate_id: int | None = None
    dispatched_subtask: str | None = None
    # H2D per-extruder tray_now from snow field: {extruder_id: normalized_global_tray_id}
    # snow encodes AMS ID in high byte: ams_id = snow >> 8, slot = snow & 0xFF
    h2d_extruder_snow: dict = field(default_factory=dict)
    # H2C nozzle rack: full device.nozzle.info array for tool-changer printers (>2 nozzles)
    nozzle_rack: list = field(default_factory=list)
    # Timestamp of last AMS data update (for RFID refresh detection)
    last_ams_update: float = 0.0
    # Printable objects for skip object functionality: {identify_id: object_name}
    printable_objects: dict = field(default_factory=dict)
    # Objects that have been skipped during the current print
    skipped_objects: list = field(default_factory=list)
    # The most recent command the printer refused, from the one inbound router
    # (``_handle_command_error_reply``). Last verdict, not a list: a command error
    # is one-shot and nothing ever un-reports it, so accumulating them would build
    # a fault log that only grows. ``None`` until a command actually fails.
    last_command_error: dict | None = None
    # Whether the active print's source 3MF supports per-object skipping.
    # Computed from archive.extra_data: requires both ``gcode_label_objects``
    # AND ``exclude_object`` to be True. Used to gate the skip-objects
    # button in the UI — even with N>=2 objects in the metadata, the firmware
    # can only skip them when the gcode carries label_object markers AND
    # the slicer profile enables exclude_object.
    skip_objects_supported: bool = False
    # Fan speeds (0-100 percentage, None if not available for this model)
    cooling_fan_speed: int | None = None  # Part cooling fan
    big_fan1_speed: int | None = None  # Auxiliary fan
    big_fan2_speed: int | None = None  # Chamber/exhaust fan
    heatbreak_fan_speed: int | None = None  # Hotend heatbreak fan
    # ``device.airduct.parts``, keyed by BS part id (AIR_FUN). The ONLY source
    # for fans that are never mirrored into a flat ``big_fanX_speed`` field —
    # notably the second auxiliary fan, which is an add-on kit on the P2S and
    # factory-fitted on the X2D. The list contains only fans that physically
    # exist, so presence here IS the hardware check; there is no model table.
    # Value: {"state", "range_start", "range_end", "func", "type"}.
    airduct_parts: dict[int, dict] = field(default_factory=dict)
    # ``device.airduct.modeList``, keyed by mode id: {"ctrl": [...], "off": [...]}
    # of part ids. A fan listed in ``off`` for the current mode is forced off by
    # the mode and cannot be driven — the printer accepts the command and
    # ignores it, which looks like a broken control.
    airduct_modes: dict[int, dict] = field(default_factory=dict)
    # Tray change history during current print: [(global_tray_id, layer_num), ...]
    # Used by usage tracker to split filament weight on mid-print tray switch
    tray_change_log: list = field(default_factory=list)
    # Firmware version info (from info.module[name="ota"].sw_ver)
    firmware_version: str | None = None
    # Developer LAN mode: parsed from MQTT "fun" field bit 0x20000000
    # True = dev mode ON (no encryption), False = dev mode OFF (encryption required), None = unknown
    developer_mode: bool | None = None
    # Currently executing macro name (set by macro execute endpoint, cleared on stg_cur 11→0)
    macro_executing: str | None = None


# Stage name mapping from BambuStudio DeviceManager.cpp
STAGE_NAMES = {
    0: "Printing",
    1: "Auto bed leveling",
    2: "Heatbed preheating",
    3: "Vibration compensation",
    4: "Changing filament",
    5: "M400 pause",
    6: "Paused (filament ran out)",
    7: "Heating nozzle",
    8: "Calibrating dynamic flow",
    9: "Scanning bed surface",
    10: "Inspecting first layer",
    11: "Identifying build plate type",
    12: "Calibrating Micro Lidar",
    13: "Homing toolhead",
    14: "Cleaning nozzle tip",
    15: "Checking extruder temperature",
    16: "Paused by the user",
    17: "Pause (front cover fall off)",
    18: "Calibrating the micro lidar",
    19: "Calibrating flow ratio",
    20: "Pause (nozzle temperature malfunction)",
    21: "Pause (heatbed temperature malfunction)",
    22: "Filament unloading",
    23: "Pause (step loss)",
    24: "Filament loading",
    25: "Motor noise cancellation",
    26: "Pause (AMS offline)",
    27: "Pause (low speed of the heatbreak fan)",
    28: "Pause (chamber temperature control problem)",
    29: "Cooling chamber",
    30: "Pause (Gcode inserted by user)",
    31: "Motor noise showoff",
    32: "Pause (nozzle clumping)",
    33: "Pause (cutter error)",
    34: "Pause (first layer error)",
    35: "Pause (nozzle clog)",
    36: "Measuring motion precision",
    37: "Enhancing motion precision",
    38: "Measure motion accuracy",
    39: "Nozzle offset calibration",
    40: "High temperature auto bed leveling",
    41: "Auto Check: Quick Release Lever",
    42: "Auto Check: Door and Upper Cover",
    43: "Laser Calibration",
    44: "Auto Check: Platform",
    45: "Confirming BirdsEye Camera location",
    46: "Calibrating BirdsEye Camera",
    47: "Auto bed leveling - phase 1",
    48: "Auto bed leveling - phase 2",
    49: "Heating chamber",
    50: "Cooling heatbed",
    51: "Printing calibration lines",
    52: "Auto Check: Material",
    53: "Live View Camera Calibration",
    54: "Waiting for heatbed temperature",
    55: "Auto Check: Material Position",
    56: "Cutting Module Offset Calibration",
    57: "Measuring Surface",
    58: "Thermal Preconditioning",
    59: "Homing Blade Holder",
    60: "Calibrating Camera Offset",
    61: "Calibrating Blade Holder Position",
    62: "Hotend Pick and Place Test",
    63: "Waiting for Chamber temperature",
    64: "Preparing Hotend",
    65: "Calibrating nozzle clumping detection",
    66: "Purifying the chamber air",
    74: "Preparing",  # Seen on H2D during print preparation
    77: "Preparing AMS",
}


def get_stage_name(stage: int) -> str:
    """Get human-readable stage name from stage number."""
    return STAGE_NAMES.get(stage, f"Unknown stage ({stage})")


# What the active airduct mode does with one fan. BS's own three outcomes
# (``FanControlNew::update_mode``) — and the middle one is the reason a boolean
# was never enough.
FAN_OFF = "off"  # the mode forces it off; BS writes "Off" where the slider was
FAN_AUTO = "auto"  # firmware drives it; BS writes "Auto" — a reading, not a control
FAN_CTRL = "ctrl"  # the user may set a speed


# BS ``enum AIR_FUN`` — the three fans the old protocol knows about.
FAN_PART_ID_COOLING = 1  # FAN_COOLING_0_AIRDOOR
FAN_PART_ID_AUX = 2  # FAN_REMOTE_COOLING_0_IDX
FAN_PART_ID_CHAMBER = 3  # FAN_CHAMBER_0_IDX

# Jog feedrates, taken from the values BS passes to ``Ctrl_Axis`` at each of its
# arrow buttons (``StatusPanel``). They are not interchangeable — the toolhead
# moves more than three times faster than the bed, and the extruder shares the
# bed's rate rather than the toolhead's.
AXIS_SPEED_XY = 3000
AXIS_SPEED_Z = 900
AXIS_SPEED_E = 900

# ⚠️ Below this, BS refuses to move the extruder at all
# (``TEMP_THRESHOLD_ALLOW_E_CTRL``) and shows a hint instead. Cold extrusion
# grinds a flat onto the filament and packs the gear teeth with the shavings.
EXTRUDER_MIN_TEMP_C = 170.0


def _synthesised_part(speed: int | None) -> dict:
    return {"type": 0, "state": int(speed or 0), "range_start": 0, "range_end": 100}


def _fan_fitted(state, model: str | None, live_key: str, cfg_key: str) -> bool:
    """Whether the machine physically has this fan.

    The printer's own ``support_*`` bool first (BS ``DevFan::ParseV2_0``), the
    mirrored config second, and **False** when neither says — BS's own default
    for ``is_support_aux_fan``. Absent means absent here, unlike most flags in
    this file: inventing a fan gives it a control that silently does nothing.
    """
    live = (getattr(state, "print_option_support", None) or {}).get(live_key)
    if isinstance(live, bool):
        return live
    fw = getattr(state, "firmware_version", None)
    if model and isinstance(fw, str) and fw:
        from backend.app.utils.printer_configs import get_device_support_flags

        cfg = get_device_support_flags(model, fw).get(cfg_key)
        if isinstance(cfg, bool):
            return cfg
    if model:
        from backend.app.utils.printer_configs import get_device_support_flags

        cfg = get_device_support_flags(model).get(cfg_key)
        if isinstance(cfg, bool):
            return cfg
    return False


def _fan_gear_bytes(value) -> tuple[int, int, int] | None:
    """Unpack BS's ``fan_gear`` word into (cooling, aux, chamber) bytes.

    ``DevFan::ParseV1_0``: three fan speeds live in one 32-bit field — byte 0 is
    part cooling, byte 1 the auxiliary fan, byte 2 the chamber fan. Each byte is
    already 0-255, with no gear conversion.

    ⚠️ **This is why an A1 Mini appeared to have three fans.** The slots exist
    whatever the hardware does, and the Mini's firmware fills all three with the
    same number — so setting part cooling to 100 % lit three badges at 100 %.
    The values are real; they simply do not mean three fans.
    """
    try:
        packed = int(value)
    except (TypeError, ValueError):
        return None
    return packed & 0xFF, (packed >> 8) & 0xFF, (packed >> 16) & 0xFF


def _percent_from_byte(value: int) -> int:
    """A 0-255 fan byte as a percentage."""
    return round(max(0, min(255, int(value))) * 100 / 255)


def airduct_parts_effective(state, model: str | None = None) -> dict[int, dict]:
    """The fan inventory, with the old protocol converted to look like the new.

    BS parity: ``DevFan::converse_to_duct``, which ``StatusPanel`` calls whenever
    the printer reports no airduct modes. It builds parts 1/2/3 by hand and sets
    the mode to ``-1``, so one widget can drive both protocols. Without the same
    step here a P1S has an empty ``airduct_parts``, every consumer reads it, and
    the machine ends up with **no fan controls at all** — while the ``M106``
    branch in :meth:`set_fan_speed` sits there unreachable, because part ids only
    ever came from the parts list it is bypassing.

    ⚠️ **The aux and chamber fans are gated on support flags, NOT on whether the
    printer reported a speed for them.** A first attempt here used the reported
    speeds as evidence — ``big_fan1_speed`` is ``None`` until mentioned, so a
    mentioned fan must exist. **Measured false on an A1 Mini**, which has part
    cooling only, ignores g-code aimed at anything else, and still echoed the
    part-cooling percentage into all three fields: setting one fan to 100 % lit
    three badges at 100 %. The fields are published regardless of the hardware,
    so they cannot answer a hardware question. ``support_aux_fan`` /
    ``support_chamber_fan`` can, and both say False for the A1, A1 Mini and A2L.

    ⚠️ We do read them as **two** flags where BS reads one. BS parses both into
    separate fields and then ``GetSupportChamberFan()`` returns
    ``is_support_aux_fan`` — the chamber field it just parsed is never used by
    it. That is a slip in the getter, not a data model: following it would tie a
    chamber fan to an unrelated aux fan.

    Part cooling is added unconditionally, as in BS — every FDM printer has one,
    and a printer that has not reported a speed yet shows 0 %, which is what the
    card already displayed.

    ⚠️ Synthesised parts must NOT be written into ``state.airduct_parts``. That
    dict answers "what did the printer actually send", and
    :meth:`set_fan_speed` picks its wire command from exactly that question —
    invented entries there would make it publish ``set_fan`` to a machine that
    only speaks ``M106``.
    """
    if state.airduct_parts:
        return state.airduct_parts

    parts: dict[int, dict] = {FAN_PART_ID_COOLING: _synthesised_part(state.cooling_fan_speed)}
    if _fan_fitted(state, model, "aux_fan", "support_aux_fan"):
        parts[FAN_PART_ID_AUX] = _synthesised_part(state.big_fan1_speed)
    if _fan_fitted(state, model, "chamber_fan", "support_chamber_fan"):
        parts[FAN_PART_ID_CHAMBER] = _synthesised_part(state.big_fan2_speed)
    return parts


# BS ``AIR_DUCT_NONE``. ``converse_to_duct`` stamps this on a printer whose fans
# it had to synthesise, and everything downstream keys off it.
AIRDUCT_MODE_NONE = -1

# BS ``enum AIR_DUCT``. Naming and the sub-mode gate only — which of these a
# machine actually offers comes from its own ``modeList``, never from this list.
AIRDUCT_COOLING_FILT = 0
AIRDUCT_HEATING_INTERNAL_FILT = 1
AIRDUCT_EXHAUST = 2
AIRDUCT_FULL_COOLING = 3


def airduct_mode_effective(state) -> int | None:
    """The airduct mode, with the old protocol reporting the mode BS gives it.

    ``converse_to_duct`` does not only build the parts — it also sets
    ``curren_mode = -1``, and that value is load-bearing twice over:

    * the fan gate takes its "no lists to consult" branch on it, so a printer
      with no airduct keeps its controls. Without it the mode falls to the
      default ``0``, the lookup misses, and by BS's own rule that is **auto** —
      which would silently take back every control :func:`airduct_parts_effective`
      just restored;
    * the mirrored configs **key their fan names by mode**, and the old-protocol
      names live under ``"-1"``. The P1S config is literally
      ``{"-1": {"3": "Chamber"}}``, so asking with mode ``0`` finds nothing and
      the chamber fan goes unnamed.

    Both were measured, not assumed: the second is visible in
    ``backend/app/data/printers/C12.json``.

    ⚠️ The trigger is **no mode lists**, which is BS's own condition
    (``StatusPanel``: ``if (modes.empty()) converse_to_duct(...)``) — not "no
    parts", which is what a first version keyed on. The two differ exactly when a
    printer has reported its parts but not yet its modes, and there the wrong
    predicate leaves mode ``0``, misses the lookup, and hands back **auto** for
    every fan on the machine.

    Where we do stop short of BS: it *clears* the reported parts in that case and
    rebuilds 1/2/3. We keep them (see :func:`airduct_parts_effective`, which
    synthesises only when there are none), because our two lists arrive in
    separate diff frames and discarding real parts on the frame between them
    would make an X2D's fans flicker into three generic ones and back.
    """
    if state.airduct_modes:
        return state.airduct_mode
    return AIRDUCT_MODE_NONE


def airduct_fan_control(state, part_id: int) -> str:
    """Which of BS's three outcomes applies to this fan in the active mode.

    A mode carries two lists: the parts it forces **off**, and the parts it hands
    over to the user (``ctrl``). BS checks them in that order and treats
    everything left over as **auto** — its ``AirMode`` says so in a comment: *"If
    the fan is not off or ctrl, it will be displayed as auto"*.

    ⚠️ **The middle state is the one we were missing, and it is not cosmetic.**
    Reading only ``off`` meant every auto fan looked controllable: we offered a
    slider, the command went out, and the mode overrode it. On an X2D in Strong
    Cooling that is a control that visibly does nothing. "Forced off by the mode"
    and "driven by the firmware" are also different answers for whoever is
    looking at the card — one is a state, the other is a policy.

    A negative mode id is the old protocol, where BS shows the slider with no
    checks at all: there are no mode lists to consult, so nothing can forbid the
    fan. An unknown mode id is treated the same way — a mode we cannot look up
    must not silently retract a control for hardware the printer is reporting.

    Module-level rather than a client method because the status route needs the
    same answer from a bare ``PrinterState``, and two copies of a rule is how
    this codebase keeps producing half-fixes.
    """
    mode_id = airduct_mode_effective(state)
    if mode_id is None or mode_id < 0:
        return FAN_CTRL
    # ⚠️ A mode we cannot look up is **auto**, not controllable — and that is BS,
    # not a choice of ours. ``AirDuctData::modes`` is a ``std::map`` and BS
    # indexes it with ``operator[]``, which default-constructs a missing entry:
    # empty ``off``, empty ``ctrl``. The part is then in neither list, which is
    # exactly the auto branch. A first version of this returned "controllable"
    # here on the reasoning that BS had no answer. It has one; I had not worked
    # it out.
    mode = (state.airduct_modes or {}).get(mode_id) or {}
    if part_id in (mode.get("off") or []):
        return FAN_OFF
    if part_id not in (mode.get("ctrl") or []):
        return FAN_AUTO
    return FAN_CTRL


def airduct_fan_controllable(state, part_id: int) -> bool:
    """Whether a speed may be published for this fan right now.

    Thin on purpose: the write path needs a yes/no, and the only yes is
    :data:`FAN_CTRL`. ``auto`` used to answer yes here, which is exactly how a
    command reached a fan the mode owns.
    """
    return airduct_fan_control(state, part_id) == FAN_CTRL


class BambuMQTTClient:
    """MQTT client for Bambu Lab printer communication."""

    MQTT_PORT = 8883

    # Class-level cache: serial_number -> False when request topic is known unsupported.
    # Persists across client instances so reconnects don't re-trigger failed subscriptions.
    _request_topic_cache: dict[str, bool] = {}
    # Counter for generating unique MQTT client IDs across instances.
    _client_instance_counter: int = 0

    # Upstream #2582: how long to wait for the AMS telemetry to echo back an
    # assignment before declaring it un-confirmed. The printer re-broadcasts tray
    # state every few seconds (and register_assignment_verification nudges a
    # fresh pushall), so this only has to survive a couple of idle push intervals.
    ASSIGNMENT_VERIFY_TIMEOUT: float = 30.0

    def __init__(
        self,
        ip_address: str,
        serial_number: str,
        access_code: str,
        model: str | None = None,
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict], None] | None = None,
        on_print_complete: Callable[[dict], None] | None = None,
        on_ams_change: Callable[[list], None] | None = None,
        on_layer_change: Callable[[int, int], None] | None = None,
        on_macro_complete: Callable[[str, str], None] | None = None,
        on_kprofiles_changed: Callable[[], None] | None = None,
        on_first_status: Callable[[str, str, str, str], None] | None = None,
        on_drying_complete: Callable[[int], None] | None = None,
        on_print_running_observed: Callable[[dict], None] | None = None,
        on_finish_photo_moment: Callable[[dict], None] | None = None,
        on_assignment_verified: Callable[[int, int, bool, dict], None] | None = None,
        on_skipped_objects_changed: Callable[[list], None] | None = None,
    ):
        self.ip_address = ip_address
        self.serial_number = serial_number
        self.access_code = access_code
        self.model = model
        self.on_state_change = on_state_change
        self.on_print_start = on_print_start
        self.on_print_complete = on_print_complete
        self.on_ams_change = on_ams_change
        self.on_layer_change = on_layer_change
        self.on_macro_complete = on_macro_complete
        # Fires with the full skipped-object list whenever it grows, from either
        # source: the printer's own ``s_obj`` report (which covers skips made on
        # its screen or from Handy) or our ``skip_objects`` command. The list,
        # not a delta — the consumer records a count, and re-sending the same
        # list must not inflate it. printer_manager wires this to the archive's
        # defective-part counter.
        self.on_skipped_objects_changed = on_skipped_objects_changed
        # #1349: fired when an AMS unit's ``dry_time`` falls from >0 to 0
        # — i.e. the drying cycle just finished (queue-triggered, ambient,
        # or manual). Receives the AMS id of the unit that finished drying.
        self.on_drying_complete = on_drying_complete
        # #1485 follow-up: fired the first time we see RUNNING state in a
        # session WHEN on_print_start was suppressed (BamDude started mid-
        # print, the #1304 first-push guard skipped the start event). Lets
        # main.py capture a fresh timelapse baseline at restart-recovery time
        # so the completion-time snapshot-diff still works. Receives the same
        # payload shape as on_print_start.
        self.on_print_running_observed = on_print_running_observed
        # #1721: fired the moment the printer enters the end-of-print
        # "Filament unloading" phase (stg_cur=22 while progress>=99 or
        # we've hit the last layer / remaining_time<=0). This is the
        # framing #1397 was after — toolhead parked, bed not yet
        # dropped — but reached via a clean state signal instead of
        # the per-layer M622 J1 macros which caused per-layer nozzle
        # parks on slicer profiles with Timelapse Type = Smooth.
        # A FINISH-state fallback below fires this same callback if
        # stage 22 never arrives (cancel mid-print, external-spool-
        # only prints, HMS halt before unload, firmware variants).
        self.on_finish_photo_moment = on_finish_photo_moment
        # Upstream #2582: fired after a spool assignment (ams_filament_setting +
        # extrusion_cali_sel) once the tray's telemetry either confirms the push
        # landed or a timeout elapses without it. Receives
        # ``(ams_id, tray_id, verified: bool, detail: dict)``. Lets the frontend
        # tell the user "loaded" vs "assignment didn't take" instead of the
        # historic fire-and-forget silence that made the AMS/Studio hand-off feel
        # random. See ``_check_assignment_verifications``.
        self.on_assignment_verified = on_assignment_verified
        # Pending read-back verifications, keyed by ``(ams_id, tray_id)``. Each
        # value is the desired end-state we just pushed plus a monotonic deadline.
        # Populated by ``register_assignment_verification``, drained by
        # ``_check_assignment_verifications`` on every AMS push.
        self._pending_assignments: dict[tuple[int, int], dict] = {}
        # Per-AMS previous ``dry_time``, used to detect the falling edge.
        # Seeded lazily as we observe each AMS unit.
        self._previous_dry_times: dict[int, int] = {}
        # Per-AMS active-cycle target params (filament + temp) we sent on the
        # last drying start. Bambu does not echo these on the per-tick AMS push
        # — only the dry_time countdown — so we cache what we sent to drive the
        # UI badge. Cleared on stop (mode=0) or when dry_time returns to 0.
        self._drying_targets: dict[int, dict[str, object]] = {}
        # Fires when the printer's K-profile push contains content that
        # differs from the last seen hash — covers MQTT (re)connect (first
        # push fills empty hash), set/edit/delete from extrusion_cali_*
        # commands (printer re-pushes the updated list), and calibration
        # save. printer_manager wires it to a DB sync. Same broadcast
        # twice in a row is a no-op.
        self.on_kprofiles_changed = on_kprofiles_changed
        self._last_kprofiles_hash: str | None = None
        # Fires once, on the first full MQTT status after a fresh connect,
        # so the startup print-reconciliation sweep can close any archive
        # left at 'printing' by a print that finished while BamDude was
        # stopped. printer_manager wires it to reconcile_printer_prints.
        self.on_first_status = on_first_status

        self.state = PrinterState()
        self._client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._previous_gcode_state: str | None = None
        # Last time "device is busy" was cleared off this printer (see
        # _clear_device_busy). Per client, so a reconnect starts fresh.
        self._device_busy_cleared_at: float = 0.0
        self._previous_gcode_file: str | None = None
        self._was_running: bool = False  # Track if we've seen RUNNING state for current print
        self._completion_triggered: bool = False  # Prevent duplicate completion triggers
        self._timelapse_during_print: bool = False  # Track if timelapse was active during this print
        # #1721: one-shot guard so the end-of-print stage-22 detector
        # and the FINISH-state fallback don't both fire on the same
        # print. Reset to False on every print start.
        self._finish_photo_captured: bool = False
        self._last_valid_progress: float = 0.0  # Last non-zero progress (firmware resets on cancel)
        self._last_valid_layer_num: int = 0  # Last non-zero layer (firmware resets on cancel)
        self._startup_reconcile_done: bool = False  # one-shot guard for reconcile_printer_prints
        self._is_dual_nozzle: bool = False  # Set when device.extruder.info has >= 2 entries
        self._message_log: deque[MQTTLogEntry] = deque(maxlen=100)
        self._logging_enabled: bool = False
        self._last_message_time: float = 0.0  # Track when we last received a message
        # Count of report-topic messages received since the last (re)connect.
        # Lets check_staleness() distinguish "printer never sent a status
        # report" (typically a wrong / mis-cased serial) from a normal quiet
        # gap mid-session. _zero_report_hint_logged keeps the actionable hint
        # to once per client lifetime so the stale loop doesn't spam it (#1465).
        self._report_messages_since_connect: int = 0
        self._zero_report_hint_logged: bool = False
        # Raw-message fan-out for VP MQTT bridge (non-proxy modes republish the
        # printer's pushes verbatim to slicers connected to a virtual printer).
        # Handlers receive (topic, payload_bytes) before JSON parsing.
        self._raw_message_handlers: list[Callable[[str, bytes], None]] = []
        self._disconnection_event: threading.Event | None = None
        self._previous_ams_hash: str | None = None  # Track AMS changes
        # Track external-spool identity changes separately: the AMS hash above is
        # built from AMS units only, so an external-spool-only filament swap would
        # never re-trigger inventory reconciliation (#2575). Covers both wire
        # shapes — ``_process_message`` folds the H2-series ``vir_slot`` into
        # ``raw_data["vt_tray"]`` before the detector runs.
        self._previous_vt_tray_hash: str | None = None

        # Cache AMS firmware/SN from get_version in case it arrives before AMS status
        # Key: ams_id (int). Value: {'sw_ver': str, 'sn': str}
        self._ams_version_cache: dict[int, dict[str, str]] = {}

        # Track which (ams_id, field) warnings have already been emitted this connection
        # so that missing-serial / missing-firmware warnings fire only once per connection.
        self._ams_version_warned: set[tuple[int | str, str]] = set()

        # K-profile command tracking.
        #
        # One entry per in-flight request, keyed by the ``sequence_id`` we send —
        # NOT a single shared slot. Two concurrent requests used to collide: the
        # second overwrote the first's expectation and Event, the first's answer
        # arrived, failed the nozzle comparison, was discarded as a broadcast, and
        # the first caller timed out through all its retries. That is the
        # "Failed to get K-profiles after 3 attempts" seen in logs where the
        # printer had in fact answered correctly both times.
        #
        # Concurrency is not hypothetical: the spool PA-Profil picker fetches
        # every installed nozzle diameter in parallel, so a dual-nozzle printer
        # issues two requests on this client every time that dialog opens.
        #
        # value: (event, expected_nozzle, profiles_or_None). Same shape as
        # ``_ack_listeners`` below, deliberately — one idiom for "await a reply
        # correlated by sequence_id" in this class.
        self._sequence_id = STUDIO_SEQ_START
        self._kprofile_waiters: dict[str, tuple[asyncio.Event, str, list | None]] = {}
        # Verdicts on K-profile *writes* (``extrusion_cali_set`` /
        # ``extrusion_cali_del``), keyed by the sequence_id we sent — the printer
        # echoes it back. ``None`` = registered, not yet answered. Filled on the
        # MQTT thread, drained by ``await_cali_ack``. A separate registry from
        # ``_kprofile_waiters`` because a read waits for a payload and a write
        # waits for a verdict; sharing one would make "no profiles" and "no
        # answer" the same value.
        self._pending_cali_acks: dict[str, dict | None] = {}

        # GCode ACK listeners: sequence_id -> (threading.Event, result_dict)
        # Used by macro execute to wait for printer ACK before returning HTTP response
        self._ack_listeners: dict[str, tuple[threading.Event, dict]] = {}
        # Our gcode sequence_id -> paho's message id, so the printer's own
        # acknowledgement can retire that packet from paho's retry queue
        # (see _drop_queued_message). Emptied on connect with the queue itself.
        self._mid_by_sequence: dict[str, int] = {}

        # Xcam hold timers - OrcaSlicer pattern: ignore incoming data for 3 seconds after command
        # Key: module_name, Value: timestamp when command was sent
        self._xcam_hold_start: dict[str, float] = {}
        self._xcam_hold_time: float = 3.0  # Ignore incoming data for 3 seconds after command

        # Track last requested tray ID for H2D dual-nozzle printers
        # H2D only reports slot number (0-3) in tray_now, not global tray ID
        # We use our tracked value to resolve the correct global ID
        self._last_load_tray_id: int | None = None

        # Captured ams_mapping from print commands on the request topic
        # Intercepts slicer/BamDude print commands to get the slot-to-tray mapping
        self._captured_ams_mapping: list[int] | None = None

        # True once we've seen (and normalised 16→6) an A2L AMS-Lite unit in the
        # AMS telemetry. Used to globalise the Lite's local ``tray_now`` to
        # 24+slot. See normalize_am_unit_id / a2l_lite_wire_ids.
        self._has_a2l_am_unit: bool = False

        # Request topic subscription tracking
        # Some printer MQTT brokers (e.g. P1S, A1) reject subscriptions to the request
        # topic by killing the TCP connection. We detect this and gracefully degrade.
        # Check class-level cache first so new client instances don't retry known-bad subscriptions.
        self._request_topic_supported: bool = BambuMQTTClient._request_topic_cache.get(self.serial_number, True)
        self._request_topic_sub_mid: int | None = None
        self._request_topic_sub_time: float = 0.0
        self._request_topic_confirmed: bool = False

        # Developer mode probe: two-phase detection to avoid false negatives.
        # Phase 1: wait for a "large" status push (len > 30) to confirm printer is ready.
        # Phase 2: wait 5s after connect before sending the probe request.
        self._dev_mode_probed: bool = False
        self._dev_mode_needs_probe: bool = False
        self._dev_mode_probe_seq: str | None = None
        self._dev_mode_probe_time: float = 0.0
        self._dev_mode_probe_failures: int = 0
        # True while developer_mode=False came from HMS_MQTT_VERIFY_FAILED rather
        # than from the probe. The HMS is a latch, not a level: the printer keeps
        # reporting it until the fault clears, so when a later hms[] arrives
        # without it (user enabled Developer Mode and restarted) we drop back to
        # "unknown" and let the probe re-run, instead of leaving a permanently
        # wrong False behind (#2732).
        self._dev_mode_from_hms: bool = False
        self._connect_time: float = 0.0

        # Set when check_staleness() force-closes the socket to trigger reconnect.
        # Prevents _on_disconnect from redundantly broadcasting state (already done).
        self._stale_reconnecting: bool = False
        # Timestamp of last stale reconnect - prevents rapid-fire socket closes
        # when the frontend polls status faster than paho can reconnect.
        self._last_stale_reconnect: float = 0.0

        # Zombie session detection via ams_filament_setting response tracking (#887).
        # The dev-mode probe only runs on first connect; this catches zombie sessions
        # that develop later (telemetry flows but publishes silently fail).
        self._last_ams_cmd_time: float = 0.0  # monotonic time of last published command
        self._ams_cmd_unanswered: int = 0  # consecutive commands with no response

    def carry_print_lifecycle_from(self, prior: "BambuMQTTClient") -> None:
        """Inherit in-flight print-tracking state from a prior client instance.

        ``printer_manager.connect_printer`` destroys and recreates the client
        on every (re)connect — including the stale-watchdog reconnect, which on
        a P1S fires often because its firmware stops publishing while the TCP
        socket stays alive. A fresh client has ``_was_running=False`` and
        ``_previous_gcode_state=None``, so a print that finishes *during* the
        stale window arrives as ``FINISH`` with no tracked RUNNING history:
        ``should_trigger_completion`` cannot fire and the print is silently
        never completed (no archive close, no notification, the queue item
        stays stuck at "printing"). Copying the print-lifecycle flags onto the
        replacement client lets completion detection — and timelapse / usage
        accounting — survive a mid-print reconnect.
        """
        self._previous_gcode_state = prior._previous_gcode_state
        self._previous_gcode_file = prior._previous_gcode_file
        self._was_running = prior._was_running
        self._completion_triggered = prior._completion_triggered
        self._timelapse_during_print = prior._timelapse_during_print
        # #1721: carry the one-shot finish-photo guard across the swap so a
        # print whose stage-22 edge already fired on the prior client doesn't
        # re-fire (or double-capture) on the replacement.
        self._finish_photo_captured = prior._finish_photo_captured
        self._last_valid_progress = prior._last_valid_progress
        self._last_valid_layer_num = prior._last_valid_layer_num
        # Re-arm the connect-edge reconcile sweep on every client recreation
        # (#1542 follow-up): a print that finished during a disconnect window —
        # or a firmware ghost-replay that reran the file under a new subtask —
        # is only caught by re-running the sweep after the reconnect. Re-running
        # is idempotent: while the same print is genuinely still RUNNING the
        # sweep classifies it "running" and no-ops, so re-firing after a
        # frequent stale-watchdog reconnect is harmless.
        self._startup_reconcile_done = False

    @property
    def _sequence_id(self) -> int:
        return self.__sequence_id

    @_sequence_id.setter
    def _sequence_id(self, value: int) -> None:
        """Keep the counter inside the band that identifies our own commands.

        A property rather than a helper method on purpose: every publisher in
        this class already writes ``self._sequence_id += 1`` inline, and that is
        a read-then-write, so the wrap lands here without any of those twenty-odd
        call sites having to know about it. Turning them all into calls to a
        helper would be a wide edit whose only purpose was to reach one branch.

        Wraps to ``STUDIO_SEQ_START + 1``, not to ``STUDIO_SEQ_START`` — 20000 is
        the value ``project_file`` pins, and handing it to an ordinary command
        once every ten thousand publishes would make that print look
        slicer-launched.
        """
        self.__sequence_id = STUDIO_SEQ_START + 1 if value >= STUDIO_SEQ_END else value

    def _is_our_sequence_id(self, raw: object) -> bool:
        """BS ``DevUtil::is_studio_cmd`` — plus its cloud case.

        ``is_cloud_cmd`` is ``seq == 0``, which BS accepts because a cloud-issued
        command is still one the user asked for from Studio. We keep it for the
        same reason: our own ``stop`` publishes ``sequence_id`` "0".
        """
        try:
            seq = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return seq == 0 or STUDIO_SEQ_START <= seq < STUDIO_SEQ_END

    @property
    def topic_subscribe(self) -> str:
        return f"device/{self.serial_number}/report"

    @property
    def topic_publish(self) -> str:
        return f"device/{self.serial_number}/request"

    @property
    def report_messages_since_connect(self) -> int:
        """Count of report-topic messages received since the latest (re)connect.

        Exposed for the connection diagnostic so it can distinguish "MQTT
        broker accepted us but the printer never published" (typically a
        wrong / mis-cased serial — #1622) from a healthy bridge that happens
        to be idle right now. Zero immediately after a fresh connect is normal;
        zero after a full status-push cycle is the actionable symptom.
        """
        return self._report_messages_since_connect

    # Maximum time (seconds) without a message before considering connection stale
    STALE_TIMEOUT = 60.0

    def is_stale(self) -> bool:
        """Check if the connection is stale (no messages for too long)."""
        if self._last_message_time == 0:
            return False  # Never received a message yet
        time_since_last = time.time() - self._last_message_time
        return time_since_last > self.STALE_TIMEOUT

    # Minimum seconds between stale reconnect attempts.  Frontend polls
    # status every few seconds - without a cooldown, each poll would
    # force-close the socket before paho has time to reconnect.
    STALE_RECONNECT_COOLDOWN = 30.0

    def check_staleness(self) -> bool:
        """Check staleness and update connected state if stale. Returns True if connected."""
        if self.state.connected and self.is_stale():
            # Don't force-close again if we already did recently - give paho
            # time to reconnect and the printer time to send its first message.
            now = time.time()
            if now - self._last_stale_reconnect < self.STALE_RECONNECT_COOLDOWN:
                return self.state.connected

            logger.warning(
                f"[{self.serial_number}] Connection stale - no message for {now - self._last_message_time:.1f}s, forcing reconnect"
            )
            # A connection that keeps going stale without ever receiving a
            # status report is almost always a wrong or mis-cased serial
            # number — the broker accepts the connection and the subscription
            # regardless, but the printer publishes to device/<real-serial>/
            # report, which is case-sensitive. Surface that once so the user
            # has something actionable instead of an endless reconnect loop.
            # Only meaningful once the *current* session has had time to receive
            # something. ``_report_messages_since_connect`` is reset by
            # ``_on_connect``, so a reconnect landing microseconds before this
            # check leaves it at 0 for reasons that have nothing to do with the
            # serial — which is how a healthy printer gets told to go check its
            # serial number 1 ms after reconnecting (#2732). Requiring
            # STALE_TIMEOUT of silence on this session means the hint fires only
            # when the printer really has published nothing to the topic we
            # subscribed to. A ``_connect_time`` of 0 means we have no timestamp
            # to judge by (never went through ``_on_connect``); fall back to the
            # old unconditional behaviour rather than swallowing the hint.
            session_too_young = self._connect_time > 0 and (time.monotonic() - self._connect_time) < self.STALE_TIMEOUT
            if self._report_messages_since_connect == 0 and not session_too_young and not self._zero_report_hint_logged:
                self._zero_report_hint_logged = True
                logger.warning(
                    "[%s] Connected and subscribed, but the printer has sent zero "
                    "status reports. The most common cause is a wrong or mis-cased "
                    "serial number — the device/<serial>/report MQTT topic is "
                    "case-sensitive. Verify the serial number configured in BamDude "
                    "exactly matches the printer.",
                    self.serial_number,
                )
            self._last_stale_reconnect = now
            self.state.connected = False
            if self.on_state_change:
                self.on_state_change(self.state)
            # Set flag so _on_disconnect knows this was intentional and skips
            # redundant state broadcast (we already set connected=False above).
            # Route based on caller thread — see force_reconnect_stale_session.
            # check_staleness is normally called from FastAPI handlers (async,
            # gets the hard-reset path) but the router covers paho-thread
            # callers too via socket-close fallback.
            self._stale_reconnecting = True
            self._reset_client_for_reconnect()
        return self.state.connected

    def force_reconnect_stale_session(self, reason: str) -> None:
        """Heal #887/#936/#1136 half-broken session: telemetry keeps arriving
        but our publishes don't reach the printer.

        Two routing paths:

        Async-context callers (background_dispatch dispatch deadline, FastAPI
        handlers via check_staleness) → full client teardown + fresh
        client_id. Wipes paho's client-side QoS 1 queue, which is exactly the
        #1136 reproducer: an unacked ``project_file`` from the broken
        session would otherwise replay on reconnect, mixing stale commands
        into the next dispatch and triggering 0500_4003 SD R/W on the
        printer.

        Paho-network-thread callers (dev-mode probe + ams_filament_setting
        zombie detection inside ``_update_state``) → socket-close fallback.
        ``loop_stop()`` from inside the network thread would self-join and
        deadlock; the safe pattern there is to close the socket and let
        paho's own loop detect it and auto-reconnect on the same client.
        Queue replay is theoretically possible from those paths but #1136
        was specifically traced through the dispatch-deadline path which
        now hard-resets.
        """
        logger.warning("[%s] Forcing MQTT reconnect: %s", self.serial_number, reason)
        self._stale_reconnecting = True
        self.state.connected = False
        if self.on_state_change:
            self.on_state_change(self.state)
        self._reset_client_for_reconnect()

    def _reset_client_for_reconnect(self) -> None:
        """Route between hard-reset and socket-close based on caller thread.

        Hard-reset (preferred) requires we're not on paho's network thread,
        since ``loop_stop()`` on the same thread deadlocks. Detect via
        ``asyncio.get_running_loop()`` — paho's callback thread has no
        loop; every legitimate hard-reset caller (FastAPI handlers,
        background async tasks) does.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            self._loop = loop
            self._hard_reset_client()
        else:
            self._socket_close_for_reconnect()

    def _hard_reset_client(self) -> None:
        """Tear down the paho client entirely and rebuild it with a fresh
        client_id, so the broker drops the old session and paho's local
        QoS 1 queue is gone. Must NOT be called from paho's network thread.
        """
        old_client = self._client
        self._client = None
        if old_client is not None:
            try:
                old_client.disconnect()  # MQTT DISCONNECT — broker drops session
            except Exception:
                pass
            try:
                old_client.loop_stop()  # blocks briefly until network thread exits
            except Exception:
                pass
        # Skip reconnect if no asyncio loop is available (test/pre-init).
        if self._loop is None:
            return
        try:
            self.connect(loop=self._loop)
        except Exception as e:
            logger.error("[%s] Hard reset reconnect failed: %s", self.serial_number, e)

    def _socket_close_for_reconnect(self) -> None:
        """Close the underlying socket so paho's loop thread detects the
        broken connection and triggers auto-reconnect on the SAME client
        instance. Safe from paho's own network thread.
        """
        if self._client:
            try:
                sock = self._client.socket()
                if sock:
                    sock.close()
            except Exception:
                pass

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.state.connected = True

            # ⚠️ Anything paho is still retrying was published BEFORE this link
            # dropped, and a command is not state: "change the plate" was right
            # when the print ended and destroys a print eleven hours later.
            # Measured 2026-08-16 — a sweep published at 02:06 was re-delivered
            # at 16:42 and swept the bed mid-print.
            #
            # Nothing depends on redelivery. The requests below are re-issued on
            # every connect; macro sends wait on the printer's own ACK and fail
            # loudly rather than hoping a retry saves them; the reconcile sweep
            # re-arms itself. This sits beside _pending_assignments.clear() for
            # the same reason: state that must not outlive a reconnect.
            #
            # ⚠️ Only on rc == 0. A failed connect is not a connection, and
            # discarding there would throw away commands that never had their
            # chance on a client that may still succeed.
            discarded = _drain_outgoing(client)
            self._mid_by_sequence.clear()
            if discarded:
                logger.warning(
                    "[%s] Discarded %d unacknowledged command(s) on connect — a command that missed "
                    "its moment is cancelled, not delivered late",
                    self.serial_number,
                    discarded,
                )

            self._stale_reconnecting = False  # Clear stale-reconnect flag on successful connect
            # Reset per-connection warning state so warnings fire once per (re)connection
            self._ams_version_warned = set()
            # Reset developer mode probe tracking (don't clear developer_mode itself -
            # it may still be valid from a previous connection, avoids reprobe loop #887)
            self._dev_mode_probed = False
            self._dev_mode_needs_probe = False
            self._dev_mode_probe_seq = None
            self._dev_mode_probe_time = 0.0
            self._dev_mode_probe_failures = 0
            self._connect_time = time.monotonic()
            # NOT reset: _dev_mode_from_hms. The HMS is a property of the printer,
            # not of our session — it survives a reconnect and is cleared only by
            # the printer no longer reporting it (see _apply_mqtt_verify_state).
            self._report_messages_since_connect = 0
            # Reset zombie session detection — fresh session means no commands pending
            self._last_ams_cmd_time = 0.0
            self._ams_cmd_unanswered = 0
            # Drop any assignment verifications that were mid-flight before the
            # reconnect — their deadlines are stale and the tray state we would
            # compare against is about to be re-pushed from scratch (upstream
            # #2582). Dropping is silent (no failure event) on purpose.
            self._pending_assignments.clear()
            client.subscribe(self.topic_subscribe)
            # Subscribe to request topic for ams_mapping capture (if supported by broker)
            if self._request_topic_supported:
                result, mid = client.subscribe(self.topic_publish)
                if result == mqtt.MQTT_ERR_SUCCESS:
                    self._request_topic_sub_mid = mid
                    self._request_topic_sub_time = time.time()
                    self._request_topic_confirmed = False
                else:
                    logger.warning(
                        "[%s] Failed to send request topic subscription",
                        self.serial_number,
                    )
                    self._request_topic_supported = False
                    BambuMQTTClient._request_topic_cache[self.serial_number] = False
            # Request full status update (includes nozzle info in push_status response)
            self._request_push_all()
            # Request firmware version info
            self._request_version()
            # Note: get_accessories returns stale nozzle data on H2D, so we don't use it.
            # The correct nozzle data comes from push_status.
            # Prime K-profile request (Bambu printers often ignore first request)
            self._prime_kprofile_request()
            # Immediately broadcast connection state change
            if self.on_state_change:
                self.on_state_change(self.state)
        else:
            self.state.connected = False

    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties=None):
        """Handle SUBACK responses to detect request topic subscription rejection."""
        if mid == self._request_topic_sub_mid:
            for rc in reason_code_list:
                if rc.is_failure:
                    logger.warning(
                        "[%s] Request topic subscription rejected (code=%d: %s). "
                        "ams_mapping capture from slicer-initiated prints unavailable.",
                        self.serial_number,
                        rc.value,
                        rc.getName(),
                    )
                    self._request_topic_supported = False
                    BambuMQTTClient._request_topic_cache[self.serial_number] = False
                else:
                    logger.info(
                        "[%s] Request topic subscription accepted. "
                        "ams_mapping capture enabled for slicer-initiated prints.",
                        self.serial_number,
                    )
                    self._request_topic_confirmed = True
                    BambuMQTTClient._request_topic_cache[self.serial_number] = True
            self._request_topic_sub_mid = None
            self._request_topic_sub_time = 0.0

    def _on_disconnect(self, client, userdata, disconnect_flags=None, rc=None, properties=None):
        # Always unblock disconnect() callers, regardless of whether we suppress
        # the state broadcast below.  disconnect() sets _disconnection_event and
        # waits on it - every callback path must fire it.
        if self._disconnection_event:
            self._disconnection_event.set()

        # If we intentionally closed the socket for stale reconnect, don't broadcast
        # another state change - check_staleness() already set connected=False and
        # notified the UI.  Just log and let paho auto-reconnect.
        if self._stale_reconnecting:
            logger.info(
                "[%s] Disconnect callback after stale reconnect (expected), rc=%s",
                self.serial_number,
                rc,
            )
            return

        # Ignore spurious disconnect callbacks if we've received a message recently
        # Paho-mqtt sometimes fires disconnect callbacks while the connection is still active.
        # BUT: never suppress error disconnects (keepalive timeout, connection lost, etc.)
        # - only suppress when rc indicates a clean/normal disconnect.
        is_error_disconnect = rc is not None and hasattr(rc, "is_failure") and rc.is_failure
        time_since_last_message = time.time() - self._last_message_time
        if not is_error_disconnect and time_since_last_message < 10.0 and self._last_message_time > 0:
            logger.debug(
                f"[{self.serial_number}] Ignoring spurious disconnect (last message {time_since_last_message:.1f}s ago)"
            )
            return

        logger.warning("[%s] MQTT disconnected: rc=%s, flags=%s", self.serial_number, rc, disconnect_flags)

        # Detect if request topic subscription caused the disconnect.
        # If we just subscribed and got disconnected before any SUBACK confirmation,
        # the broker likely killed the connection due to the unauthorized subscription.
        if (
            self._request_topic_sub_time > 0
            and not self._request_topic_confirmed
            and time.time() - self._request_topic_sub_time < 10.0
        ):
            logger.warning(
                "[%s] Disconnected shortly after request topic subscription. Disabling request topic for this printer.",
                self.serial_number,
            )
            self._request_topic_supported = False
            BambuMQTTClient._request_topic_cache[self.serial_number] = False
        self._request_topic_sub_mid = None
        self._request_topic_sub_time = 0.0

        self.state.connected = False
        if self.on_state_change:
            self.on_state_change(self.state)

    def _on_message(self, client, userdata, msg):
        for handler in self._raw_message_handlers:
            try:
                handler(msg.topic, msg.payload)
            except Exception:
                logger.exception(
                    "[%s] raw-message handler crashed for topic=%s",
                    self.serial_number,
                    msg.topic,
                )
        try:
            try:
                raw = msg.payload.decode()
            except UnicodeDecodeError:
                # Some firmware versions (e.g. A1 Mini 01.07.02.00) send payloads
                # with non-UTF-8 bytes. Replace invalid bytes to keep JSON parseable.
                raw = msg.payload.decode(errors="replace")
                logger.warning(
                    "[%s] MQTT payload contained non-UTF-8 bytes (topic=%s, len=%d)",
                    self.serial_number,
                    msg.topic,
                    len(msg.payload),
                )
            payload = json.loads(raw)
            # Track last message time - receiving a message proves we're connected
            self._last_message_time = time.time()
            self.state.connected = True

            # Intercept request-topic messages (print commands from slicer/BamDude)
            if msg.topic == self.topic_publish:
                self._handle_request_message(payload)
                return

            # Count status reports per connection so check_staleness() can tell
            # "printer never sent a report" apart from a mid-session quiet gap.
            if msg.topic == self.topic_subscribe:
                self._report_messages_since_connect += 1

            # Log message if logging is enabled
            if self._logging_enabled:
                self._message_log.append(
                    MQTTLogEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        topic=msg.topic,
                        direction="in",
                        payload=payload,
                    )
                )
            self._process_message(payload)
        except json.JSONDecodeError:
            pass  # Ignore non-JSON MQTT messages (e.g. binary or malformed payloads)

    def _handle_request_message(self, data: dict) -> None:
        """Intercept print commands on the request topic to capture ams_mapping."""
        print_data = data.get("print", {})
        if not isinstance(print_data, dict):
            return
        command = print_data.get("command", "")
        if command == "project_file":
            if "ams_mapping" in print_data:
                self._captured_ams_mapping = print_data["ams_mapping"]
                logger.info(
                    "[%s] Captured ams_mapping from print command: %s",
                    self.serial_number,
                    self._captured_ams_mapping,
                )
            # Diagnostic for upstream #1162 follow-up (X2D + FTS routing): when a
            # slicer-launched project_file passes through the request topic, log
            # the full payload so we can diff Studio's field set against ours.
            # We pin our own sequence_id to "20000" when sending project_file
            # ourselves, so any other value means the command came from
            # Studio / Orca, not from us.
            if print_data.get("sequence_id") != "20000":
                logger.info(
                    "[%s] External project_file payload: %s",
                    self.serial_number,
                    json.dumps(print_data),
                )

    def _process_message(self, payload: dict):
        """Process incoming MQTT message from printer."""
        # Handle top-level AMS data (comes outside of "print" key)
        # Wrap in try/except to prevent breaking the MQTT connection
        if "ams" in payload:
            try:
                self._handle_ams_data(payload["ams"])
            except Exception as e:
                logger.error("[%s] Error handling AMS data: %s", self.serial_number, e)

        # Handle xcam data (camera settings and AI detection) at top level
        if "xcam" in payload:
            xcam_data = payload["xcam"]
            logger.debug("[%s] Received xcam data at top level: %s", self.serial_number, xcam_data)
            self._parse_xcam_data(xcam_data)
            # Fire state change callback for top-level xcam (not nested in "print")
            if "print" not in payload and self.on_state_change:
                self.on_state_change(self.state)

        # Handle system responses (accessories info, etc.)
        if "system" in payload:
            system_data = payload["system"]
            logger.debug("[%s] Received system data: %s", self.serial_number, system_data)
            self._handle_system_response(system_data)

        # Firmware operations answer in their own envelope, not under ``print``.
        # BS carries a second copy of the command-error check for exactly this
        # (``DeviceManager.cpp``, the ``j["upgrade"]`` branch) — the same
        # ``err_code``, the same sequence-id test, a different key.
        if "upgrade" in payload:
            upgrade_data = payload["upgrade"]
            if isinstance(upgrade_data, dict) and "command" in upgrade_data:
                self._handle_upgrade_error_reply(upgrade_data)

        # Handle info responses (firmware version info from get_version command)
        if "info" in payload:
            info_data = payload["info"]
            if isinstance(info_data, dict) and info_data.get("command") == "get_version":
                self._handle_version_info(info_data)

        # Parse WiFi signal at top level (some printers send it here)
        if "wifi_signal" in payload:
            wifi_signal = payload["wifi_signal"]
            if isinstance(wifi_signal, (int, float)):
                self.state.wifi_signal = int(wifi_signal)
            elif isinstance(wifi_signal, str):
                try:
                    self.state.wifi_signal = int(wifi_signal.replace("dBm", "").strip())
                except ValueError:
                    pass  # Ignore unparseable wifi_signal strings; field is non-critical

            # Detect ethernet: wifi_signal == -90 is a sentinel for "WiFi disabled/ethernet"
            from backend.app.utils.printer_models import has_ethernet

            if has_ethernet(self.model):
                self.state.wired_network = self.state.wifi_signal == -90

        # Parse developer LAN mode from top-level "fun" field
        # Some firmware versions send "fun" at the top level, others inside "print"
        if "fun" in payload and self.state.developer_mode is None:
            try:
                fun_val = payload["fun"]
                fun_int = fun_val if isinstance(fun_val, int) else int(fun_val, 16)
                new_dev_mode = (fun_int & 0x20000000) == 0
                if new_dev_mode != self.state.developer_mode:
                    self.state.developer_mode = new_dev_mode
                    if self.on_state_change:
                        self.on_state_change(self.state)
            except (ValueError, TypeError):
                pass

        # Motor-noise cali support is derived from the ``fun`` function bitfield,
        # bit 10 (BS DeviceManager.cpp:4385). ``fun`` may arrive at the top level
        # (here) or nested in ``print`` (handled in _update_state); parse both so
        # every model/firmware layout is covered. Whichever message carries
        # ``fun`` last wins — same key, same bit.
        if "fun" in payload:
            try:
                _fun_val = payload["fun"]
                _fun_int = _fun_val if isinstance(_fun_val, int) else int(str(_fun_val), 16)
                self.state.device_cali_support["support_motor_noise_cali"] = bool((_fun_int >> 10) & 0x1)
                # Safety tab support bits (BS): fun bit 12 = open-door check,
                # bit 62 = idle heating protection.
                self.state.print_options.support_open_door = bool((_fun_int >> 12) & 0x1)
                self.state.print_options.support_idle_heating = bool((_fun_int >> 62) & 0x1)
            except (ValueError, TypeError):
                pass

        if "print" in payload:
            print_data = payload["print"]
            # Handle gcode_line ACK - resolve ACK listener for HTTP wait
            if isinstance(print_data, dict) and print_data.get("command") == "gcode_line" and "result" in print_data:
                seq_id = print_data.get("sequence_id")
                result = print_data.get("result", "")
                reason = print_data.get("reason", "")
                logger.info(
                    "[%s][MACRO] gcode_line ACK: seq=%s, result=%s, reason=%s, macro=%s",
                    self.serial_number,
                    seq_id,
                    result,
                    reason,
                    self.state.macro_executing,
                )
                # ⚠️ The printer has now said "I received this and acted on it",
                # which is better evidence than a broker PUBACK — that only means
                # the hop in between is content. Keeping the packet retriable past
                # this point is not caution; it is a second execution waiting for
                # a disconnect. Applies to a refusal too ("device busy" is still
                # an answer): re-delivering a declined movement command later,
                # when the machine is in a different state, is the hazard itself.
                _retired_mid = self._mid_by_sequence.pop(seq_id, None) if seq_id else None
                if _retired_mid is not None and _drop_queued_message(self._client, _retired_mid):
                    logger.debug(
                        "[%s] Withdrew seq=%s from the retry queue — the printer confirmed it",
                        self.serial_number,
                        seq_id,
                    )
                if seq_id and seq_id in self._ack_listeners:
                    event, result_dict = self._ack_listeners.pop(seq_id)
                    result_dict["success"] = result == "success"
                    result_dict["reason"] = reason
                    event.set()
                    # If ACK failed, clear macro state immediately
                    if result != "success" and self.state.macro_executing:
                        macro_name = self.state.macro_executing
                        self.state.macro_executing = None
                        logger.warning(
                            "[%s][MACRO] Printer rejected GCode, macro '%s' failed", self.serial_number, macro_name
                        )
                        if self.on_macro_complete:
                            self.on_macro_complete(macro_name, "failed")

            # Check if xcam is nested inside print data
            if "xcam" in print_data:
                logger.debug("[%s] Found xcam inside print data: %s", self.serial_number, print_data["xcam"])
                self._parse_xcam_data(print_data["xcam"])

            # Log when we see gcode_state changes
            if "gcode_state" in print_data:
                logger.debug(
                    f"[{self.serial_number}] Received gcode_state: {print_data.get('gcode_state')}, "
                    f"gcode_file: {print_data.get('gcode_file')}, subtask_name: {print_data.get('subtask_name')}"
                )

            # Detect dual-nozzle BEFORE processing AMS data (tray_now disambiguation needs it)
            # device.extruder.info with >= 2 entries only exists on dual-nozzle printers (H2D, H2D Pro)
            if not self._is_dual_nozzle and "device" in print_data:
                dev = print_data.get("device")
                if isinstance(dev, dict):
                    ext_info = dev.get("extruder", {}).get("info", [])
                    if isinstance(ext_info, list) and len(ext_info) >= 2:
                        self._is_dual_nozzle = True
                        logger.info("[%s] Detected dual-nozzle printer from device.extruder.info", self.serial_number)

            # Handle AMS data that comes inside print key
            if "ams" in print_data:
                try:
                    self._handle_ams_data(print_data["ams"])
                except Exception as e:
                    logger.error("[%s] Error handling AMS data from print: %s", self.serial_number, e)

            # AMS Settings dialog echoes: the printer reflects the most recently
            # accepted ``print_option`` values directly under the ``print`` key.
            # Same hold-timer pattern as the ams.* flags above.
            _ams_echo_now = time.time()
            if "auto_switch_filament" in print_data:
                if (self.state.ams_settings_hold.get("ams_auto_switch_filament", 0) + 3.0) < _ams_echo_now:
                    self.state.ams_auto_switch_filament = bool(print_data["auto_switch_filament"])
            if "air_print_detect" in print_data:
                if (self.state.ams_settings_hold.get("ams_air_print_detect", 0) + 3.0) < _ams_echo_now:
                    self.state.ams_air_print_detect = bool(print_data["air_print_detect"])

            # Printer Settings dialog echoes — direct field echoes for the
            # print_option toggles. Each respects a 3 s hold from
            # printer_settings_hold so a freshly-toggled flag isn't
            # immediately overwritten by the printer's confirm push.
            _ps_now = time.time()
            _ps_ttl = 3.0

            def _ps_hold(flag: str) -> bool:
                ts = self.state.printer_settings_hold.get(flag)
                return ts is not None and (_ps_now - ts) < _ps_ttl

            po = self.state.print_options
            # BS reads several print-option VALUES from home_flag when the named
            # echo is absent — P1/A1-series send only home_flag, not the named
            # fields (DevPrintOptionsParser lines 14-33): auto-recovery bit 4,
            # sound bit 17, filament-tangle bit 20, nozzle-blob bit 24. The named
            # echoes below override where present.
            _hf_val = print_data.get("home_flag")
            if isinstance(_hf_val, int):
                _hfu = _hf_val & 0xFFFFFFFF
                if not _ps_hold("auto_recovery"):
                    po.auto_recovery_step_loss = bool((_hfu >> 4) & 0x1)
                if not _ps_hold("sound_enable"):
                    po.sound_enable = bool((_hfu >> 17) & 0x1)
                if not _ps_hold("filament_tangle"):
                    po.filament_tangle_detect = bool((_hfu >> 20) & 0x1)
                if not _ps_hold("nozzle_blob"):
                    po.nozzle_blob_detect = bool((_hfu >> 24) & 0x1)
                if not _ps_hold("air_print_nonvisual"):
                    po.air_print_nonvisual = bool((_hfu >> 28) & 0x1)  # BS ams_air_print_status
            # BS-parity: the top-level ``cfg`` hex bitfield carries steady-state
            # VALUES for most options (DevPrintOptionsParser cfg part, lines
            # 181-232). X2D-class printers send values here rather than as named
            # fields; P1/A1 use home_flag (above). Each respects its 3 s hold so a
            # just-toggled value isn't clobbered; the named echoes below still
            # override on a fresh set.
            _cfg_val = print_data.get("cfg")
            if _cfg_val is not None:
                try:
                    _cfg_int = _cfg_val if isinstance(_cfg_val, int) else int(str(_cfg_val), 16)
                except (ValueError, TypeError):
                    _cfg_int = None
                if _cfg_int is not None:
                    _c = _cfg_int
                    if not _ps_hold("open_door"):
                        po.open_door_check = (_c >> 20) & 0x3
                    if not _ps_hold("idle_heating"):
                        po.idle_heating_protect = (_c >> 32) & 0x3
                    if not _ps_hold("auto_recovery"):
                        po.auto_recovery_step_loss = bool((_c >> 16) & 0x1)
                    if not _ps_hold("sound_enable"):
                        po.sound_enable = bool((_c >> 22) & 0x1)
                    if not _ps_hold("filament_tangle"):
                        po.filament_tangle_detect = bool((_c >> 23) & 0x1)
                    if not _ps_hold("nozzle_blob"):
                        po.nozzle_blob_detect = bool((_c >> 24) & 0x1)
                    if not _ps_hold("save_remote_to_storage"):
                        po.save_remote_to_storage = (_c >> 19) & 0x1
                    if not _ps_hold("purify_air"):
                        po.air_purification = (_c >> 36) & 0x3
                    if not _ps_hold("snapshot"):
                        po.snapshot_enabled = ((_c >> 38) & 0x3) == 2
                    if not _ps_hold("smart_nozzle_blob"):
                        po.nozzle_blob_v2 = (_c >> 43) & 0x3  # 0 off / 1 on / 2 auto
                    if not _ps_hold("ai_monitoring"):
                        po.ai_monitoring_sensitivity = {0: "never_halt", 1: "low", 2: "medium", 3: "high"}.get(
                            (_c >> 13) & 0x3
                        )

            # Accumulate per-option support (BS DevPrintOptionsParser parity).
            self._parse_print_option_support(print_data)

            self._latch_flow_type_flags(print_data)
            if "auto_recovery" in print_data and not _ps_hold("auto_recovery"):
                po.auto_recovery_step_loss = bool(print_data["auto_recovery"])
            if "sound_enable" in print_data and not _ps_hold("sound_enable"):
                po.sound_enable = bool(print_data["sound_enable"])
            if "filament_tangle_detect" in print_data and not _ps_hold("filament_tangle"):
                po.filament_tangle_detect = bool(print_data["filament_tangle_detect"])
            if "nozzle_blob_detect" in print_data and not _ps_hold("nozzle_blob"):
                po.nozzle_blob_detect = bool(print_data["nozzle_blob_detect"])
            if "build_plate_marker_detect" in print_data and not _ps_hold("plate_type"):
                po.plate_type_detect = bool(print_data["build_plate_marker_detect"])
            if "plate_align_check" in print_data and not _ps_hold("plate_align"):
                po.plate_align_check = bool(print_data["plate_align_check"])
            if "air_purification" in print_data and not _ps_hold("purify_air"):
                po.air_purification = int(print_data["air_purification"])
            if "xcam_door_open_check" in print_data and not _ps_hold("open_door"):
                po.open_door_check = int(print_data["xcam_door_open_check"])
            if "xcam__save_remote_print_file_to_storage" in print_data and not _ps_hold("save_remote_to_storage"):
                po.save_remote_to_storage = int(print_data["xcam__save_remote_print_file_to_storage"])

            # Handle vir_slot (H2-series external spool data) - list of external trays
            # Process vir_slot FIRST so it takes priority over vt_tray
            if "vir_slot" in print_data:
                vir_slot = print_data["vir_slot"]
                if isinstance(vir_slot, list) and vir_slot:
                    # Fix: single-nozzle printers (X1C, P1S, A1) report their single
                    # external slot with id=255 in vir_slot, but tray_now=254 when active.
                    # Remap id=255→254 for single-slot printers so active detection works.
                    # Dual-nozzle (H2D) has 2 slots: id=254 (Ext-L) and id=255 (Ext-R).
                    if len(vir_slot) == 1 and str(vir_slot[0].get("id", "")) == "255":
                        vir_slot[0]["id"] = "254"
                    self.state.raw_data["vt_tray"] = vir_slot

            # Handle vt_tray (virtual tray / external spool) data
            # Only use vt_tray if vir_slot is NOT in this message AND we don't already
            # have vir_slot data (H2-series sends vt_tray as a single active spool dict
            # which would overwrite the correct multi-slot vir_slot data)
            if "vt_tray" in print_data and "vir_slot" not in print_data:
                vt_tray = print_data["vt_tray"]
                existing = self.state.raw_data.get("vt_tray")
                # Don't let a single-spool vt_tray dict overwrite multi-slot vir_slot data
                if isinstance(vt_tray, dict) and isinstance(existing, list) and len(existing) > 1:
                    pass  # Keep the vir_slot data
                else:
                    if isinstance(vt_tray, dict):
                        vt_tray = [vt_tray]
                    self.state.raw_data["vt_tray"] = vt_tray

            # The AMS change-hash in _handle_ams_data sees AMS units only, and
            # _handle_ams_data already ran above — so a change to the external
            # spool alone (swapping generic TPU for generic ABS on the printer)
            # never re-triggers on_ams_change, leaving a stale inventory
            # assignment on the ams_id=255 slot (#2575). Detect external-spool
            # identity changes here and fire the same callback. Placed after BOTH
            # the vir_slot and vt_tray stores above so the H2-series shape (and
            # its 255->254 single-slot id remap) is already normalised into
            # raw_data["vt_tray"]. Wrapped like the two _handle_ams_data call
            # sites so a callback fault can't break the MQTT message loop.
            try:
                self._maybe_trigger_external_spool_change()
            except Exception as e:
                logger.error("[%s] Error detecting external-spool change: %s", self.serial_number, e)

            # Parse ams_status directly from print data (NOT from print.ams)
            # ams_status is a combined value: lower 8 bits = sub status, bits 8-15 = main status
            # Main status: 0=idle, 1=filament_change, 2=rfid_identifying, 3=assist, 4=calibration
            # Sub status (when main=1): 2=heating, 3=AMS feeding, 4=retract, 6=push, 7=purge
            if "ams_status" in print_data:
                raw_ams_status = print_data["ams_status"]
                if isinstance(raw_ams_status, str):
                    try:
                        self.state.ams_status = int(raw_ams_status)
                    except ValueError:
                        self.state.ams_status = 0
                else:
                    self.state.ams_status = raw_ams_status if raw_ams_status is not None else 0

                # Compute main and sub status
                self.state.ams_status_sub = self.state.ams_status & 0xFF
                self.state.ams_status_main = (self.state.ams_status >> 8) & 0xFF

                # Log when ams_status changes (for filament change tracking debug)
                logger.debug(
                    f"[{self.serial_number}] ams_status: {self.state.ams_status} "
                    f"(main={self.state.ams_status_main}, sub={self.state.ams_status_sub})"
                )

            # Check for K-profile response (extrusion_cali)
            if "command" in print_data:
                cmd = print_data.get("command")
                logger.debug("[%s] Received command response: %s", self.serial_number, cmd)
                # ⚠️ The name alone cannot answer the question this logging exists
                # for: whether a command we are watching is fresh or a replay of
                # one already answered. ``sequence_id`` is what separates them,
                # and it lives in the payload — so at DEBUG the whole thing goes
                # to the log, credentials masked (see _loggable).
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[%s]   payload: %s", self.serial_number, _loggable(print_data))
                # One router for every command's verdict, ahead of the per-command
                # branches below. BS runs the same check once for all commands
                # (DeviceManager.cpp, guarded by is_studio_cmd/is_cloud_cmd)
                # rather than teaching each sender to read its own reply.
                self._handle_command_error_reply(print_data)
                if cmd == "set_ctt":
                    self._handle_set_ctt_reply(print_data)
                elif cmd in ("extrusion_cali_set", "extrusion_cali_del"):
                    self._route_ack(print_data)
                elif cmd in ("extrusion_cali_sel", "ams_filament_setting"):
                    logger.debug("[%s] %s response: %s", self.serial_number, cmd, print_data)
                # AMS drying responses are rare (user-initiated only) and the
                # full payload — including `result` and any `reason` code —
                # is the only way to diagnose silent rejections like #1447.
                # INFO level so the body lands in support bundles by default.
                elif cmd == "ams_filament_drying":
                    logger.info("[%s] ams_filament_drying response: %s", self.serial_number, print_data)
                # Check for developer mode probe response
                if (
                    cmd == "ams_filament_setting"
                    and self._dev_mode_probe_seq is not None
                    and print_data.get("sequence_id") == self._dev_mode_probe_seq
                ):
                    self._handle_dev_mode_probe_response(print_data)
                # Track user-initiated ams_filament_setting responses (#887
                # zombie detection). Reset both the timer AND the unanswered
                # counter on ANY response — the response proves the channel is
                # alive, so the counter must not stay armed even when the
                # watchdog already zeroed `_last_ams_cmd_time` on a previous
                # tick. The original `and self._last_ams_cmd_time > 0` guard
                # caused #1164: one sluggish response (>10s) would set the
                # counter to 1 and zero the timer; the late response arrived
                # but was ignored by this branch (timer is 0); the counter
                # stayed at 1 indefinitely; the very next slow response —
                # possibly hours later, on a totally unrelated command —
                # would take it to 2 and force-reconnect, surfacing as
                # "filament config doesn't reach the printer ~6 changes in".
                elif cmd == "ams_filament_setting":
                    self._last_ams_cmd_time = 0.0
                    self._ams_cmd_unanswered = 0
            # A K-profile query echoes the nozzle diameter it was ASKED for at
            # the top level of its reply. ``_update_state`` reads a top-level
            # ``nozzle_diameter`` as the installed hardware, so feeding these
            # replies to it overwrites the real nozzle with whichever size was
            # last queried (upstream #2663). ``git_backup`` sweeps
            # 0.2/0.4/0.6/0.8 in turn, so the printer ended up recorded as 0.8
            # until the next status push — and ``print_scheduler``'s nozzle
            # mismatch gate (#1899) then refused to dispatch, failing prints
            # with a bogus "printer has 0.8mm".
            #
            # Both query commands are guarded, not just the one upstream fixed:
            # ``extrusion_cali_query_result`` publishes ``nozzle_diameter`` too
            # (see its payload), so its reply carries the same echo. It has no
            # caller today — Auto PA is parked for want of a lidar printer — but
            # a guard that covers only the reachable half is a trap set for
            # whoever wires it up.
            #
            # Neither reply carries status telemetry, so nothing is lost by
            # skipping the state update. The nozzle comes from push_status
            # alone, the same conclusion ``_handle_system_response`` reached
            # about ``get_accessories``.
            cali_query_reply = print_data.get("command") in (
                "extrusion_cali_get",
                "extrusion_cali_get_result",
            )

            if "command" in print_data and print_data.get("command") == "extrusion_cali_get":
                self._handle_kprofile_response(print_data)
                # Also mirror the same payload into Plan-1 cali history
                # (PACalibHistoryEntry list) for the Filament Calibration
                # History modal. KProfile stays the source of truth for the
                # legacy K-profiles UI; this is the new typed view.
                self._handle_extrusion_cali_history(print_data)

            if "command" in print_data and print_data.get("command") == "extrusion_cali_get_result":
                self._handle_extrusion_cali_get_result(print_data)

            if not cali_query_reply:
                self._update_state(print_data)

    def _handle_system_response(self, data: dict):
        """Handle system responses including accessories info.

        Note: get_accessories returns stale/incorrect nozzle_type data on H2D.
        The correct nozzle data comes from push_status, so we don't update
        nozzle type/diameter from get_accessories. We just log the response
        for debugging purposes.
        """
        command = data.get("command")

        if command == "get_accessories":
            # Log response for debugging - but DON'T use it to update nozzle data
            # because it returns stale values (e.g., 'stainless_steel' when the
            # actual nozzle is 'HH01' hardened steel high-flow)
            logger.debug("[%s] Accessories response (not used for nozzle data): %s", self.serial_number, data)

    def _handle_version_info(self, data: dict):
        """Handle version info response from get_version command.

        Parses firmware version from the 'ota' module in the module list.
        Also extracts AMS unit firmware versions from AMS modules and stores
        them on the corresponding AMS unit in raw_data so the status route can
        expose them to the frontend.

        AMS module naming conventions (numeric suffix is the AMS unit ID):
        - ``ams/<id>``  – original AMS
        - ``n3f/<id>``  – AMS 2 Pro (H2D Pro and similar)
        - ``n3s/<id>``  – AMS HT (H2D Pro and similar)

        Message format:
        {
            "command": "get_version",
            "module": [
                {"name": "ota", "sw_ver": "01.08.05.00"},
                {"name": "rv1126", "sw_ver": "00.00.14.74"},
                {"name": "ams/0", "sw_ver": "00.00.06.96", "sn": "ABC123"},
                {"name": "n3f/0", "sw_ver": "03.00.21.29", "sn": "19C06A552504488"},
                {"name": "n3s/128", "sw_ver": "03.00.21.29", "sn": "19F06A561801096"},
                ...
            ]
        }
        """
        modules = data.get("module", [])
        if not isinstance(modules, list):
            return

        state_changed = False
        for module in modules:
            if not isinstance(module, dict):
                continue
            if module.get("name") == "ota":
                version = module.get("sw_ver")
                if version:
                    old_version = self.state.firmware_version
                    self.state.firmware_version = version
                    if old_version != version:
                        logger.info("[%s] Firmware version: %s", self.serial_number, version)
                    state_changed = True
                break

        # Extract AMS unit firmware versions from AMS modules.
        # See module-level _AMS_MODULE_PREFIXES for supported naming conventions.
        # Always cache regardless of whether AMS data has arrived yet - get_version
        # often arrives before the first push_status, so caching must be unconditional.
        ams_raw = self.state.raw_data.get("ams")
        for module in modules:
            if not isinstance(module, dict):
                continue
            name = module.get("name", "")
            if not any(name.startswith(prefix) for prefix in _AMS_MODULE_PREFIXES):
                continue
            try:
                ams_id = int(name.split("/", 1)[1])
            except (ValueError, IndexError):
                continue
            sw_ver = module.get("sw_ver", "")
            sn = module.get("sn", "")

            # Extract module type from prefix (e.g. "ams/0" → "ams", "n3f/0" → "n3f")
            module_type = name.split("/", 1)[0]

            # Always cache so _apply_ams_version_cache can apply it when AMS data arrives
            if sw_ver or sn or module_type:
                self._ams_version_cache[ams_id] = {"sw_ver": sw_ver, "sn": sn, "module_type": module_type}
                state_changed = True

            # Also directly update any AMS unit already present in raw_data
            if ams_raw and isinstance(ams_raw, list):
                for ams_unit in ams_raw:
                    if not isinstance(ams_unit, dict):
                        continue
                    try:
                        unit_id = int(ams_unit.get("id")) if ams_unit.get("id") is not None else None
                    except (ValueError, TypeError):
                        unit_id = None
                    if unit_id == ams_id:
                        if sw_ver:
                            ams_unit["sw_ver"] = sw_ver
                            logger.debug("[%s] AMS %s firmware: %s", self.serial_number, ams_id, sw_ver)
                        # Only set sn from version info if not already present in AMS data
                        if sn and not ams_unit.get("sn"):
                            ams_unit["sn"] = sn
                        if module_type:
                            ams_unit["module_type"] = module_type
                        break

        # Full module inventory for the Printer Settings → Add-ons tab: keep the
        # printer body + every accessory that advertises a display name
        # (``product_name``) — AMS units, filament buffer/hub, exhaust fan, etc.
        # Internal control boards (mc/th/ap/smc/ixmj/…) report an empty
        # product_name and are dropped. Mirrors what BS lists on Update Device.
        addon_modules: list[ModuleInfo] = []
        for module in modules:
            if not isinstance(module, dict):
                continue
            product_name = str(module.get("product_name", "") or "").strip()
            if not product_name:
                continue
            addon_modules.append(
                ModuleInfo(
                    name=str(module.get("name", "") or ""),
                    product_name=product_name,
                    hw_ver=str(module.get("hw_ver", "") or ""),
                    sw_ver=str(module.get("sw_ver", "") or ""),
                    serial=str(module.get("sn", "") or ""),
                )
            )
        if addon_modules:
            self.state.modules = addon_modules
            state_changed = True

        # Trigger state change callback AFTER both loops so AMS sn/sw_ver are
        # included in the broadcast (not just the printer firmware version).
        if state_changed and self.on_state_change:
            self.on_state_change(self.state)

        # Warn if any AMS unit is still missing serial number or firmware version
        # after processing the version info response. Warn only once per connection
        # to avoid repeated noise on older firmware that doesn't report these fields.
        if ams_raw and isinstance(ams_raw, list):
            for ams_unit in ams_raw:
                if not isinstance(ams_unit, dict):
                    continue
                ams_id = ams_unit.get("id", "?")
                if not ams_unit.get("sn") and not ams_unit.get("serial_number"):
                    key = (ams_id, "sn")
                    if key not in self._ams_version_warned:
                        self._ams_version_warned.add(key)
                        logger.warning(
                            "[%s] AMS unit %s: serial number not available in version info",
                            self.serial_number,
                            ams_id,
                        )
                if not ams_unit.get("sw_ver"):
                    key = (ams_id, "sw_ver")
                    if key not in self._ams_version_warned:
                        self._ams_version_warned.add(key)
                        logger.warning(
                            "[%s] AMS unit %s: firmware version not available in version info",
                            self.serial_number,
                            ams_id,
                        )

    def _apply_ams_version_cache(self, ams_list: list) -> None:
        """Apply cached AMS firmware/SN (from get_version) onto an AMS list in-place.

        get_version may arrive before pushall/AMS status, and AMS unit IDs may be
        strings in MQTT payloads. This helper normalizes IDs and fills missing
        sw_ver/sn fields without overwriting values already present.
        """
        if not ams_list or not isinstance(ams_list, list):
            return
        cache = self._ams_version_cache
        if not cache:
            return
        for unit in ams_list:
            if not isinstance(unit, dict):
                continue
            raw_id = unit.get("id")
            try:
                unit_id = int(raw_id) if raw_id is not None else None
            except (ValueError, TypeError):
                unit_id = None
            if unit_id is None:
                continue
            cached = cache.get(unit_id)
            if not cached:
                continue
            sw_ver = cached.get("sw_ver") or ""
            sn = cached.get("sn") or ""
            if sw_ver and not unit.get("sw_ver"):
                unit["sw_ver"] = sw_ver
            # Only set sn if not already present in AMS data
            if sn and not unit.get("sn") and not unit.get("serial_number"):
                unit["sn"] = sn
            module_type = cached.get("module_type") or ""
            if module_type and not unit.get("module_type"):
                unit["module_type"] = module_type

    def _apply_xcam_support(self, xcam: dict) -> None:
        """BS: ai_monitoring + buildplate-type support = xcam has ``cfg``;
        buildplate-mark support = xcam has ``buildplate_marker_detector``
        (DevPrintOptionsParser lines 42/82/113). Only turns support ON (BS never
        clears these in the xcam branch)."""
        sup = self.state.print_option_support
        if "cfg" in xcam:
            sup["ai_monitoring"] = True
            sup["plate_type"] = True
        if "buildplate_marker_detector" in xcam:
            sup["plate_mark"] = True

    def _parse_print_option_support(self, data: dict) -> None:
        """Mirror BS ``DevPrintOptionsParser::ParseDetectionV1_0`` +
        ``DevConfig::ParsePrintOptionsConfig`` — accumulate each option's
        is_support into ``state.print_option_support``. Sources are applied in BS
        order (home_flag → xcam → cfg → fun → named bools → fun2); later sources
        override earlier. Each source is only read when the printer sent it, so a
        sparse P1-series push leaves modern-only options untouched (default off in
        compute_printer_supports)."""
        sup = self.state.print_option_support

        _hx = parse_hex_bitfield

        hf = data.get("home_flag")
        if isinstance(hf, int):
            u = hf & 0xFFFFFFFF
            sup["sound"] = bool((u >> 18) & 1)
            sup["filament_tangle"] = bool((u >> 19) & 1)
            sup["nozzle_blob"] = bool((u >> 25) & 1)
            sup["air_print_nonvisual"] = bool((u >> 29) & 1)  # BS is_support_air_print_detection

        xcam = data.get("xcam")
        if isinstance(xcam, dict):
            self._apply_xcam_support(xcam)

        cfg = _hx(data.get("cfg"))
        if cfg is not None:
            sup["snapshot"] = ((cfg >> 38) & 0x3) in (1, 2)
            # Store-sent-files support: X2D-class printers carry the value at cfg
            # bit 19 but don't send the named support_save_remote_print_file_to_storage
            # bool. BS shows the row for them (verified vs BS on X2D); P1/A1 send no
            # top-level cfg and correctly hide it. The named bool below still wins
            # when a printer does report it.
            sup["save_remote_to_storage"] = True

        fun = _hx(data.get("fun"))
        if fun is not None:
            sup["filament_tangle"] = bool((fun >> 9) & 1)
            sup["spaghetti_detector"] = bool((fun >> 42) & 1)
            sup["pileup_detector"] = bool((fun >> 43) & 1)
            sup["nozzleclumping_detector"] = bool((fun >> 44) & 1)
            sup["airprinting_detector"] = bool((fun >> 45) & 1)
            sup["sound"] = bool((fun >> 8) & 1)
            sup["nozzle_blob"] = bool((fun >> 13) & 1)
            # BS ``is_support_door_open_check = get_flag_bits(fun, 12)``. It was
            # already being decoded into ``print_options.support_open_door`` in
            # two other places and read by nobody; stashing it HERE is what makes
            # it reachable, because this dict is the one the capability computer
            # consults and "absent" means "not reported" — a distinction
            # ``support_open_door``'s ``False`` default cannot express.
            sup["open_door_check"] = bool((fun >> 12) & 1)
            # BS ``is_support_partskip = get_flag_bits(fun, 49)``. Skipping a
            # part needs BOTH this and object labelling in the sliced plate —
            # BS checks the second as ``is_model_support_partskip``. We had only
            # the model half, and only in the UI.
            sup["partskip"] = bool((fun >> 49) & 1)
            # BS ``SetSupportCoolingFilter(get_flag_bits(fun, 46))``. Gates the
            # "Filter" sub-mode, which exists only on the cooling air-duct mode.
            sup["cooling_filter"] = bool((fun >> 46) & 1)
            # BS ``m_support_mqtt_bet_ctrl = get_flag_bits(fun, 39)`` (the typo
            # is theirs). It picks which command carries a bed setpoint:
            # ``command_set_bed`` sends ``set_bed_temp`` as JSON when this is
            # set and falls back to ``M140`` over gcode_line when it is not.
            sup["mqtt_bed_ctrl"] = bool((fun >> 39) & 1)
            # The other two halves of the same protocol split, and they sit right
            # beside it: BS ``DevAxis`` reads homing from bit 32 and axis control
            # from bit 38. Each picks a structured MQTT command over the g-code
            # BS falls back to — ``back_to_center`` and ``xyz_ctrl``.
            sup["mqtt_homing"] = bool((fun >> 32) & 1)
            sup["mqtt_axis_ctrl"] = bool((fun >> 38) & 1)
            # BS ``is_support_internal_timelapse = get_flag_bits(fun, 28)``. A
            # machine with somewhere of its own to put a timelapse — which makes
            # the SD card irrelevant to whether one can be recorded at all.
            sup["internal_timelapse"] = bool((fun >> 28) & 1)

        if isinstance(data.get("support_build_plate_marker_detect"), bool):
            sup["plate_mark"] = data["support_build_plate_marker_detect"]
        if isinstance(data.get("support_auto_recovery_step_loss"), bool):
            sup["auto_recovery"] = data["support_auto_recovery_step_loss"]
        if isinstance(data.get("support_prompt_sound"), bool):
            sup["sound"] = data["support_prompt_sound"]
        if isinstance(data.get("support_filament_tangle_detect"), bool):
            sup["filament_tangle"] = data["support_filament_tangle_detect"]
        if isinstance(data.get("support_ai_monitoring"), bool):
            sup["ai_monitoring_devcfg"] = data["support_ai_monitoring"]
        if isinstance(data.get("support_first_layer_inspect"), bool):
            sup["first_layer_inspector"] = data["support_first_layer_inspect"]
        if isinstance(data.get("support_save_remote_print_file_to_storage"), bool):
            sup["save_remote_to_storage"] = data["support_save_remote_print_file_to_storage"]
        # The two AMS Settings checkboxes BS gates on named bools rather than on
        # bits (``DeviceManager.cpp``: ``is_support_update_remain`` /
        # ``is_support_filament_backup``). The mirrored config carries the same
        # keys, but the live report is the printer's own answer and wins.
        # Which fans the machine physically has. BS ``DevFan::ParseV2_0`` reads
        # both, and they are the only honest answer: the printer publishes
        # ``big_fan1_speed`` / ``big_fan2_speed`` whether or not the fan exists,
        # so a reported speed proves nothing. Measured on an A1 Mini, which has
        # only part cooling and still echoed 100 % into all three fields.
        if isinstance(data.get("support_aux_fan"), bool):
            sup["aux_fan"] = data["support_aux_fan"]
        if isinstance(data.get("support_timelapse"), bool):
            sup["timelapse"] = data["support_timelapse"]
        if isinstance(data.get("support_chamber_fan"), bool):
            sup["chamber_fan"] = data["support_chamber_fan"]
        if isinstance(data.get("support_update_remain"), bool):
            sup["update_remain"] = data["support_update_remain"]
        if isinstance(data.get("support_filament_backup"), bool):
            sup["filament_backup"] = data["support_filament_backup"]

        fun2 = _hx(data.get("fun2"))
        if fun2 is not None:
            # BS ``is_support_update_remain_hide_display`` — a SECOND condition on
            # the remaining-capacity checkbox, ANDed with the support flag above.
            # A machine can support the feature and still be told not to show it.
            sup["update_remain_hide_display"] = bool((fun2 >> 6) & 1)
            sup["plate_align"] = bool((fun2 >> 2) & 1)
            sup["purify_air"] = bool((fun2 >> 4) & 1)
            sup["fod_check"] = bool((fun2 >> 13) & 1)
            sup["displacement_detection"] = bool((fun2 >> 14) & 1)
            sup["smart_nozzle_blob"] = bool((fun2 >> 15) & 1)
            # BS ``is_support_print_with_emmc`` (DeviceManager.cpp:4408) — may a
            # print be sent when no card is inserted.
            sup["print_with_emmc"] = bool(fun2 & 1)
            # BS ``is_support_model_internal_storage`` (DeviceManager.cpp:4413) —
            # does the file browser get an internal-storage tab. ⚠️ A DIFFERENT
            # question from the bit above: Studio gates the storage tab on this
            # one (MediaFilePanel.cpp:274) and the send on the other. A machine
            # can have one without the other; never collapse them into one flag.
            sup["model_internal_storage"] = bool((fun2 >> 17) & 1)

    def _parse_xcam_data(self, xcam_data):
        """Parse xcam data for camera settings and AI detection options."""
        if not isinstance(xcam_data, dict):
            return
        # BS parity: xcam presence gates ai_monitoring / buildplate support.
        self._apply_xcam_support(xcam_data)

        current_time = time.time()

        # Helper to check if we should accept incoming value for a module
        # OrcaSlicer pattern: simple hold timer, ignore ALL data for 3 seconds after command
        def should_accept_value(module_name: str, incoming_value: bool) -> bool:
            """Check if we should accept an incoming xcam value.

            OrcaSlicer pattern: After sending a command, ignore incoming data
            for 3 seconds. After that, accept whatever the printer sends.
            """
            if module_name not in self._xcam_hold_start:
                return True  # No hold timer, accept incoming

            hold_start = self._xcam_hold_start[module_name]
            elapsed = current_time - hold_start

            if elapsed > self._xcam_hold_time:
                # Hold timer expired - accept incoming and clear hold
                del self._xcam_hold_start[module_name]
                logger.debug("[%s] Hold expired for %s, accepting %s", self.serial_number, module_name, incoming_value)
                return True

            # Within hold period - ignore incoming data
            logger.debug(
                f"[{self.serial_number}] Ignoring {module_name}={incoming_value} "
                f"(hold active, {elapsed:.1f}s < {self._xcam_hold_time}s)"
            )
            return False

        # Log all xcam fields for debugging
        logger.debug("[%s] Parsing xcam data - all fields: %s", self.serial_number, list(xcam_data.keys()))

        # The xcam.cfg bitmask contains the ACTUAL detector states - the individual
        # boolean fields (spaghetti_detector, etc.) are often stale/cached.
        # Layout per BS DevPrintOptionsParser (each detector = [enabled, sens_low,
        # sens_high], enabled is the LOW bit; sensitivity = the two bits ABOVE it):
        # - spaghetti: enabled 7, sens 8-9
        # - pileup:    enabled 10, sens 11-12
        # - clump:     enabled 13, sens 14-15
        # - airprint:  enabled 16, sens 17-18
        # Sensitivity values: 0=low, 1=medium, 2=high
        if "cfg" in xcam_data:
            cfg = xcam_data["cfg"]
            logger.debug("[%s] xcam cfg bitmask: %s (binary: %s)", self.serial_number, cfg, bin(cfg))

            def decode_detector(enabled_bit):
                """Decode a detector from xcam.cfg (BS layout): enabled = ``enabled_bit``,
                sensitivity = the two bits above it."""
                enabled = bool((cfg >> enabled_bit) & 1)
                sens_bits = (cfg >> (enabled_bit + 1)) & 0x3
                sensitivity = {0: "low", 1: "medium", 2: "high"}.get(sens_bits, "medium")
                return enabled, sensitivity

            # Spaghetti detector (enabled 7, sens 8-9)
            cfg_spaghetti, cfg_sensitivity = decode_detector(7)
            if should_accept_value("spaghetti_detector", cfg_spaghetti):
                old_value = self.state.print_options.spaghetti_detector
                if cfg_spaghetti != old_value:
                    logger.debug(
                        f"[{self.serial_number}] spaghetti_detector changed (from cfg): {old_value} -> {cfg_spaghetti}"
                    )
                self.state.print_options.spaghetti_detector = cfg_spaghetti

            # Check hold timer for sensitivity before accepting
            if "halt_print_sensitivity" not in self._xcam_hold_start:
                if cfg_sensitivity != self.state.print_options.halt_print_sensitivity:
                    logger.debug(
                        f"[{self.serial_number}] Sensitivity changed (from cfg): "
                        f"{self.state.print_options.halt_print_sensitivity} -> {cfg_sensitivity}"
                    )
                    self.state.print_options.halt_print_sensitivity = cfg_sensitivity
            else:
                hold_start = self._xcam_hold_start["halt_print_sensitivity"]
                elapsed = current_time - hold_start
                if elapsed <= self._xcam_hold_time:
                    logger.debug(
                        f"[{self.serial_number}] Ignoring cfg sensitivity={cfg_sensitivity} "
                        f"(hold active, {elapsed:.1f}s < {self._xcam_hold_time}s)"
                    )
                else:
                    # Hold expired - accept from cfg
                    if cfg_sensitivity != self.state.print_options.halt_print_sensitivity:
                        logger.debug(
                            f"[{self.serial_number}] Sensitivity synced (from cfg after hold): "
                            f"{self.state.print_options.halt_print_sensitivity} -> {cfg_sensitivity}"
                        )
                        self.state.print_options.halt_print_sensitivity = cfg_sensitivity
                    del self._xcam_hold_start["halt_print_sensitivity"]

            # Pileup detector (bits 8-10)
            cfg_pileup, cfg_pileup_sens = decode_detector(10)
            if should_accept_value("pileup_detector", cfg_pileup):
                if cfg_pileup != self.state.print_options.pileup_detector:
                    logger.debug(
                        f"[{self.serial_number}] pileup_detector changed (from cfg): {self.state.print_options.pileup_detector} -> {cfg_pileup}"
                    )
                    self.state.print_options.pileup_detector = cfg_pileup
            # Pileup sensitivity with hold timer
            if "pileup_sensitivity" not in self._xcam_hold_start:
                if cfg_pileup_sens != self.state.print_options.pileup_sensitivity:
                    logger.debug(
                        f"[{self.serial_number}] pileup_sensitivity changed (from cfg): {self.state.print_options.pileup_sensitivity} -> {cfg_pileup_sens}"
                    )
                    self.state.print_options.pileup_sensitivity = cfg_pileup_sens
            else:
                hold_start = self._xcam_hold_start["pileup_sensitivity"]
                elapsed = current_time - hold_start
                if elapsed > self._xcam_hold_time:
                    if cfg_pileup_sens != self.state.print_options.pileup_sensitivity:
                        logger.debug(
                            f"[{self.serial_number}] pileup_sensitivity synced (from cfg after hold): {self.state.print_options.pileup_sensitivity} -> {cfg_pileup_sens}"
                        )
                        self.state.print_options.pileup_sensitivity = cfg_pileup_sens
                    del self._xcam_hold_start["pileup_sensitivity"]

            # Clump/nozzle clumping detector (bits 11-13)
            cfg_clump, cfg_clump_sens = decode_detector(13)
            if should_accept_value("clump_detector", cfg_clump):
                if cfg_clump != self.state.print_options.nozzle_clumping_detector:
                    logger.debug(
                        f"[{self.serial_number}] nozzle_clumping_detector changed (from cfg): {self.state.print_options.nozzle_clumping_detector} -> {cfg_clump}"
                    )
                    self.state.print_options.nozzle_clumping_detector = cfg_clump
            # Clump sensitivity with hold timer
            if "nozzle_clumping_sensitivity" not in self._xcam_hold_start:
                if cfg_clump_sens != self.state.print_options.nozzle_clumping_sensitivity:
                    logger.debug(
                        f"[{self.serial_number}] nozzle_clumping_sensitivity changed (from cfg): {self.state.print_options.nozzle_clumping_sensitivity} -> {cfg_clump_sens}"
                    )
                    self.state.print_options.nozzle_clumping_sensitivity = cfg_clump_sens
            else:
                hold_start = self._xcam_hold_start["nozzle_clumping_sensitivity"]
                elapsed = current_time - hold_start
                if elapsed > self._xcam_hold_time:
                    if cfg_clump_sens != self.state.print_options.nozzle_clumping_sensitivity:
                        logger.debug(
                            f"[{self.serial_number}] nozzle_clumping_sensitivity synced (from cfg after hold): {self.state.print_options.nozzle_clumping_sensitivity} -> {cfg_clump_sens}"
                        )
                        self.state.print_options.nozzle_clumping_sensitivity = cfg_clump_sens
                    del self._xcam_hold_start["nozzle_clumping_sensitivity"]

            # Airprint detector (bits 14-16)
            cfg_airprint, cfg_airprint_sens = decode_detector(16)
            if should_accept_value("airprint_detector", cfg_airprint):
                if cfg_airprint != self.state.print_options.airprint_detector:
                    logger.debug(
                        f"[{self.serial_number}] airprint_detector changed (from cfg): {self.state.print_options.airprint_detector} -> {cfg_airprint}"
                    )
                    self.state.print_options.airprint_detector = cfg_airprint
            # Airprint sensitivity with hold timer
            if "airprint_sensitivity" not in self._xcam_hold_start:
                if cfg_airprint_sens != self.state.print_options.airprint_sensitivity:
                    logger.debug(
                        f"[{self.serial_number}] airprint_sensitivity changed (from cfg): {self.state.print_options.airprint_sensitivity} -> {cfg_airprint_sens}"
                    )
                    self.state.print_options.airprint_sensitivity = cfg_airprint_sens
            else:
                hold_start = self._xcam_hold_start["airprint_sensitivity"]
                elapsed = current_time - hold_start
                if elapsed > self._xcam_hold_time:
                    if cfg_airprint_sens != self.state.print_options.airprint_sensitivity:
                        logger.debug(
                            f"[{self.serial_number}] airprint_sensitivity synced (from cfg after hold): {self.state.print_options.airprint_sensitivity} -> {cfg_airprint_sens}"
                        )
                        self.state.print_options.airprint_sensitivity = cfg_airprint_sens
                    del self._xcam_hold_start["airprint_sensitivity"]

            # FOD check (xcam.cfg bit 21) + displacement (bit 22) + buildplate
            # align (bit 20) — value only, no sensitivity (BS DevPrintOptions.cpp
            # :77-84).
            cfg_fod = bool((cfg >> 21) & 0x1)
            if should_accept_value("fod_check", cfg_fod):
                self.state.print_options.fod_check = cfg_fod
            cfg_disp = bool((cfg >> 22) & 0x1)
            if should_accept_value("displacement_detection", cfg_disp):
                self.state.print_options.displacement_detection = cfg_disp
            cfg_align = bool((cfg >> 20) & 0x1)
            if should_accept_value("plate_offset_switch", cfg_align):
                self.state.print_options.plate_align_check = cfg_align

        # Camera settings
        if "ipcam_record" in xcam_data:
            self.state.ipcam = xcam_data.get("ipcam_record") == "enable"
        if "timelapse" in xcam_data:
            self.state.timelapse = xcam_data.get("timelapse") == "enable"
            # Track if timelapse was ever active during this print
            if self.state.timelapse and self._was_running:
                self._timelapse_during_print = True

        # Skip spaghetti_detector boolean field - we read from cfg bitmask above
        if "print_halt" in xcam_data:
            self.state.print_options.print_halt = bool(xcam_data.get("print_halt"))
        # Skip halt_print_sensitivity field - it's always stale ("medium").
        # We read the actual spaghetti sensitivity from xcam.cfg bits 8-9 above.
        if "first_layer_inspector" in xcam_data:
            new_value = bool(xcam_data.get("first_layer_inspector"))
            if should_accept_value("first_layer_inspector", new_value):
                self.state.print_options.first_layer_inspector = new_value
        if "printing_monitor" in xcam_data:
            new_value = bool(xcam_data.get("printing_monitor"))
            if should_accept_value("printing_monitor", new_value):
                self.state.print_options.printing_monitor = new_value
        if "buildplate_marker_detector" in xcam_data:
            new_value = bool(xcam_data.get("buildplate_marker_detector"))
            if should_accept_value("buildplate_marker_detector", new_value):
                self.state.print_options.buildplate_marker_detector = new_value
                # BS reads BOTH buildplate mark and type values from this same
                # nested field (DevPrintOptions.cpp:112/120).
                self.state.print_options.plate_type_detect = new_value
        if "allow_skip_parts" in xcam_data:
            new_value = bool(xcam_data.get("allow_skip_parts"))
            if should_accept_value("allow_skip_parts", new_value):
                self.state.print_options.allow_skip_parts = new_value

        # Additional AI detectors - these are decoded from cfg bitmask above, not from
        # individual boolean fields (which are not sent by the printer)
        # pileup_detector, nozzle_clumping_detector, airprint_detector - from cfg
        # auto_recovery_step_loss and filament_tangle_detect - tracked locally only
        if "auto_recovery_step_loss" in xcam_data:
            self.state.print_options.auto_recovery_step_loss = bool(xcam_data.get("auto_recovery_step_loss"))
        if "filament_tangle_detect" in xcam_data:
            self.state.print_options.filament_tangle_detect = bool(xcam_data.get("filament_tangle_detect"))

    @staticmethod
    def _resolve_local_slot_from_mapping(local_slot: int, mapping_raw: list | None) -> int | None:
        """Resolve a local AMS slot ID to a global tray ID using the MQTT mapping field.

        The MQTT mapping field is an array of snow-encoded values:
        each entry = ams_hw_id * 256 + slot_id (65535 = unmapped).

        Finds entries where the local slot matches, then computes the global tray ID.
        Returns the global ID if exactly one AMS matches, or None if ambiguous/unavailable.
        """
        if not isinstance(mapping_raw, list) or not mapping_raw:
            return None

        candidates: set[int] = set()
        for value in mapping_raw:
            if not isinstance(value, int) or value >= 65535:
                continue
            ams_hw_id = value >> 8
            slot = value & 0xFF
            if 0 <= ams_hw_id <= 3 and (slot & 0x03) == local_slot:
                candidates.add(ams_hw_id * 4 + local_slot)
            elif 128 <= ams_hw_id <= 135 and local_slot == 0:
                candidates.add(ams_hw_id)

        if len(candidates) == 1:
            return candidates.pop()
        return None

    def _normalize_a2l_am_units(self, ams_list) -> None:
        """A2L AMS-Lite normalisation: rewrite the physical unit id 16 → 6 in
        place, as early as possible, so every downstream reader — the merge,
        ``apply_tray_exist_bits`` (bit base 24), the API, usage tracking, the DB
        constraint — sees the normalised id and needs no special-casing.
        ``tray_now`` (local) and the outbound wire are handled separately. Only
        id 16 is ever touched, so every other printer/AMS type is unaffected.
        Runs on both the dict-wrapped and bare-list AMS shapes.
        """
        if not isinstance(ams_list, list):
            return
        for unit in ams_list:
            if not isinstance(unit, dict):
                continue
            try:
                uid = int(unit.get("id"))
            except (TypeError, ValueError):
                continue
            if uid == A2L_LITE_PHYSICAL_AMS_ID:
                unit["id"] = A2L_LITE_NORMALIZED_AMS_ID
                if not self._has_a2l_am_unit:
                    logger.info(
                        "[%s] A2L AMS-Lite detected (unit id 16) — normalising to id %d",
                        self.serial_number,
                        A2L_LITE_NORMALIZED_AMS_ID,
                    )
                self._has_a2l_am_unit = True

    def _maybe_trigger_external_spool_change(self):
        """Fire ``on_ams_change`` when the external spool's identity changes.

        The AMS change-hash in ``_handle_ams_data`` is built only from AMS units,
        so an external-spool-only filament swap would otherwise never re-run the
        inventory reconciliation that unlinks a stale ``ams_id=255`` assignment
        (the branch in ``main.on_ams_change``). The reconciliation reads
        ``vt_tray`` from live status itself, so we only need to re-fire the
        callback with the current merged AMS data (#2575).

        Reads ``raw_data["vt_tray"]``, which by this point holds either the real
        ``vt_tray`` or the H2-series ``vir_slot`` list (``_process_message`` folds
        the latter into the same key, single-slot ``id`` 255->254 remap applied).

        BamDude divergence — the first observation SEEDS the hash without firing.
        Upstream fires on the None -> first-value transition, but on our tree the
        connect-time pushall carries both ``print.ams`` and ``vt_tray`` in ONE
        message, and ``_previous_ams_hash`` also starts at None: upstream's shape
        therefore dispatches ``on_ams_change`` twice, concurrently, on every
        printer on every (re)connect. That handler holds a DB session across
        Spoolman HTTP I/O, so a guaranteed double-run is a real cost. The AMS path
        already covers connect-time reconciliation; this detector only has to
        catch *subsequent* swaps, which is exactly what the bug is about.
        """
        import hashlib

        vt_tray = self.state.raw_data.get("vt_tray")
        if not isinstance(vt_tray, list):
            return
        # Identity fields only — deliberately exclude `remain` so a print's
        # steadily-dropping fill percentage doesn't fire on every MQTT push.
        fp_parts = [
            f"{vt.get('id')}:{vt.get('tray_type')}:{vt.get('tray_color')}:"
            f"{vt.get('tag_uid')}:{vt.get('tray_uuid')}:{vt.get('tray_info_idx')}"
            for vt in vt_tray
            if isinstance(vt, dict)
        ]
        vt_hash = hashlib.md5(":".join(fp_parts).encode(), usedforsecurity=False).hexdigest()
        if vt_hash == self._previous_vt_tray_hash:
            return
        seeding = self._previous_vt_tray_hash is None
        self._previous_vt_tray_hash = vt_hash
        if seeding or not self.on_ams_change:
            return
        logger.debug("[%s] External spool identity changed, triggering sync callback", self.serial_number)
        self.on_ams_change(self.state.raw_data.get("ams") or [])

    def _handle_ams_data(self, ams_data):
        """Handle AMS data changes for Spoolman integration.

        This is called when we receive top-level AMS data in MQTT messages.
        It detects changes and triggers the callback for Spoolman sync.
        """
        import hashlib

        # Handle nested ams structure: {"ams": {"ams": [...]}} or {"ams": [...]}
        # Also handle P1S partial updates: {"tray_now": ..., "tray_tar": ...} without "ams" key
        ams_list = None
        if isinstance(ams_data, dict):
            if "ams" in ams_data:
                ams_list = ams_data["ams"]
                self._normalize_a2l_am_units(ams_list)
            # Log all AMS dict fields to debug tray_now for H2D dual-nozzle
            non_list_fields = {k: v for k, v in ams_data.items() if k != "ams"}
            if non_list_fields:
                logger.debug("[%s] AMS dict fields: %s", self.serial_number, non_list_fields)

            # AMS system-level user settings (BS "AMS Settings" dialog).
            # Each respects a 3-second hold-timer so a just-sent toggle isn't
            # clobbered by the printer's interleaved echo (BS HOLD_TIME_3SEC).
            _ams_settings_now = time.time()
            _ams_hold_ttl = 3.0

            def _ams_hold_active(flag_name: str) -> bool:
                ts = self.state.ams_settings_hold.get(flag_name)
                return ts is not None and (_ams_settings_now - ts) < _ams_hold_ttl

            if "insert_flag" in ams_data and not _ams_hold_active("ams_insertion_update"):
                self.state.ams_insertion_update = bool(ams_data["insert_flag"])
            if "power_on_flag" in ams_data and not _ams_hold_active("ams_power_on_update"):
                self.state.ams_power_on_update = bool(ams_data["power_on_flag"])
            if "calibrate_remain_flag" in ams_data and not _ams_hold_active("ams_remain_capacity"):
                self.state.ams_remain_capacity = bool(ams_data["calibrate_remain_flag"])

            # IMPORTANT: Parse ams_status FIRST before tray_now, so we have fresh status
            # when checking if we're in filament change mode for tray_now disambiguation
            if "ams_status" in ams_data:
                raw_ams_status = ams_data["ams_status"]
                if isinstance(raw_ams_status, str):
                    try:
                        self.state.ams_status = int(raw_ams_status)
                    except ValueError:
                        self.state.ams_status = 0
                else:
                    self.state.ams_status = raw_ams_status if raw_ams_status is not None else 0
                # Compute main and sub status
                self.state.ams_status_sub = self.state.ams_status & 0xFF
                self.state.ams_status_main = (self.state.ams_status >> 8) & 0xFF
                logger.debug(
                    f"[{self.serial_number}] ams_status: {self.state.ams_status} "
                    f"(main={self.state.ams_status_main}, sub={self.state.ams_status_sub})"
                )

            # Parse tray_tar / tray_pre (RAW). These identify the slot the firmware
            # now expects (tray_tar) and the slot loaded before (tray_pre) — the key
            # signal for a runout PAUSE where AMS Filament Backup has advanced to the
            # next compatible slot (upstream #2587). Stored raw here; globalised at
            # the API boundary because that resolution needs the AMS layout. On
            # H2D/multi-AMS these are local slot numbers (0-3), not global IDs.
            for _tray_key in ("tray_tar", "tray_pre"):
                if _tray_key in ams_data:
                    _raw = ams_data[_tray_key]
                    if isinstance(_raw, str):
                        try:
                            _val = int(_raw)
                        except ValueError:
                            _val = 255
                    else:
                        _val = _raw if _raw is not None else 255
                    _prev = getattr(self.state, _tray_key)
                    setattr(self.state, _tray_key, _val)
                    # Log changes only while paused — the moment the operator cares —
                    # so a healthy print's normal tar churn doesn't spam the log.
                    if _val != _prev and _val not in (255, -1) and self.state.state == "PAUSE":
                        logger.info(
                            "[%s] AMS %s changed to %s while paused (expected/previous slot signal)",
                            self.serial_number,
                            _tray_key,
                            _val,
                        )

            # Parse tray_now from AMS dict - this is the currently loaded tray global ID
            # Note: tray_tar is also available but on H2D it's just slot number (0-3), not global ID
            if "tray_now" in ams_data:
                raw_tray_now = ams_data["tray_now"]
                # Convert string to int if needed
                if isinstance(raw_tray_now, str):
                    try:
                        parsed_tray_now = int(raw_tray_now)
                    except ValueError:
                        parsed_tray_now = 255
                else:
                    parsed_tray_now = raw_tray_now if raw_tray_now is not None else 255

                # H2D dual-nozzle printers report only slot number (0-3), not global tray ID
                # Use active_extruder + ams_extruder_map to determine which AMS the slot belongs to
                # Single-nozzle printers with multiple AMS (e.g. P2S) also report local slot IDs (#420)
                # - disambiguated below using MQTT mapping field
                ams_map = self.state.ams_extruder_map
                if self._is_dual_nozzle and 0 <= parsed_tray_now <= 3:
                    # First, check if we have a pending target that matches this slot
                    pending_target = self.state.pending_tray_target
                    if pending_target is not None:
                        pending_slot = pending_target % 4
                        if pending_slot == parsed_tray_now:
                            # Slot matches our pending target - use the full global ID
                            logger.debug(
                                f"[{self.serial_number}] H2D tray_now disambiguation: "
                                f"slot {parsed_tray_now} matches pending_tray_target {pending_target} -> using global ID {pending_target}"
                            )
                            self.state.tray_now = pending_target
                            # Clear pending target now that load is confirmed
                            self.state.pending_tray_target = None
                        else:
                            # Slot doesn't match our pending target - something changed, use slot as-is
                            logger.warning(
                                f"[{self.serial_number}] H2D tray_now: slot {parsed_tray_now} doesn't match "
                                f"pending_tray_target {pending_target} (slot {pending_slot}) - using slot as global ID"
                            )
                            self.state.tray_now = parsed_tray_now
                            # Clear pending target since it's stale
                            self.state.pending_tray_target = None
                    else:
                        # No pending target - use h2d_extruder_snow for accurate disambiguation
                        # H2D sends snow field in device.extruder.info with AMS ID in high byte
                        active_ext = self.state.active_extruder  # 0=right, 1=left

                        # Best source: use snow value from device.extruder.info if available
                        snow_tray = self.state.h2d_extruder_snow.get(active_ext)
                        if snow_tray is not None and snow_tray != 255:
                            # snow_tray is already normalized to global ID
                            # Verify the slot matches what we see in tray_now
                            # Regular AMS: slot = global_id % 4; AMS HT (128-135): single slot = 0
                            snow_slot = snow_tray % 4 if snow_tray < 128 else (0 if snow_tray <= 135 else -1)
                            if snow_slot == parsed_tray_now:
                                if self.state.tray_now != snow_tray:
                                    logger.debug(
                                        f"[{self.serial_number}] H2D tray_now from snow: "
                                        f"extruder[{active_ext}] snow={snow_tray} (slot {snow_slot})"
                                    )
                                self.state.tray_now = snow_tray
                            else:
                                # Slot mismatch - snow field may not have updated yet, trust snow
                                logger.debug(
                                    f"[{self.serial_number}] H2D tray_now: ams.tray_now slot {parsed_tray_now} "
                                    f"!= snow slot {snow_slot}, using snow value {snow_tray}"
                                )
                                self.state.tray_now = snow_tray
                        else:
                            # Fallback: snow not available, use ams_extruder_map (less reliable)
                            # Find ALL AMS units on the active extruder
                            ams_on_extruder = []
                            for ams_id_str, ext_id in ams_map.items():
                                if ext_id == active_ext:
                                    try:
                                        ams_on_extruder.append(int(ams_id_str))
                                    except ValueError:
                                        pass  # Skip AMS IDs that aren't valid integers

                            if len(ams_on_extruder) == 1:
                                # Single AMS on this extruder - unambiguous
                                active_ams_id = ams_on_extruder[0]
                                if 128 <= active_ams_id <= 135:
                                    # AMS-HT: single slot per unit, global ID = unit ID
                                    global_tray_id = active_ams_id
                                else:
                                    global_tray_id = active_ams_id * 4 + parsed_tray_now
                                logger.debug(
                                    f"[{self.serial_number}] H2D tray_now fallback: "
                                    f"slot {parsed_tray_now} + single AMS {active_ams_id} -> global ID {global_tray_id}"
                                )
                                self.state.tray_now = global_tray_id
                            elif len(ams_on_extruder) > 1:
                                # Multiple AMS on this extruder - keep current if valid, else try to narrow down
                                current_tray = self.state.tray_now
                                # Determine which AMS unit and slot the current tray belongs to
                                if 0 <= current_tray <= 15:
                                    current_ams = current_tray // 4
                                    current_slot = current_tray % 4
                                elif 128 <= current_tray <= 135:
                                    current_ams = current_tray  # AMS-HT: ID = tray ID
                                    current_slot = 0
                                else:
                                    current_ams = -1
                                    current_slot = -1
                                if current_ams in ams_on_extruder and current_slot == parsed_tray_now:
                                    # Current is valid and matches slot - keep it
                                    logger.debug(
                                        f"[{self.serial_number}] H2D tray_now: multiple AMS {ams_on_extruder}, "
                                        f"keeping current {current_tray} (matches slot {parsed_tray_now})"
                                    )
                                else:
                                    # Filter candidates: AMS-HT (128-135) only valid for slot 0
                                    if parsed_tray_now > 0:
                                        candidates = [a for a in ams_on_extruder if a <= 3]
                                    else:
                                        candidates = ams_on_extruder
                                    if len(candidates) == 1:
                                        cand = candidates[0]
                                        resolved = cand if 128 <= cand <= 135 else cand * 4 + parsed_tray_now
                                        logger.debug(
                                            f"[{self.serial_number}] H2D tray_now: multiple AMS {ams_on_extruder}, "
                                            f"narrowed to AMS {cand} -> global ID {resolved}"
                                        )
                                        self.state.tray_now = resolved
                                    else:
                                        # Genuinely ambiguous - use slot as-is (will be wrong for non-first AMS)
                                        logger.warning(
                                            f"[{self.serial_number}] H2D tray_now: multiple AMS {ams_on_extruder} on extruder {active_ext}, "
                                            f"no snow field, using slot {parsed_tray_now} (may be incorrect)"
                                        )
                                        self.state.tray_now = parsed_tray_now
                            else:
                                # No AMS on this extruder - use slot as-is
                                logger.warning(
                                    f"[{self.serial_number}] H2D tray_now: no AMS on extruder {active_ext}, "
                                    f"using slot {parsed_tray_now}"
                                )
                                self.state.tray_now = parsed_tray_now
                elif not self._is_dual_nozzle and 0 <= parsed_tray_now <= 3:
                    # #1822: on an all-external-spool print the slicer maps every slot to -1;
                    # the firmware still reports tray_now = the physical slot, which stuck the
                    # H2S active-tray highlight on AMS slot 1. Promote to 254 (external spool)
                    # so the highlight lands on the external spool. Narrow scope — only the
                    # all-(-1) mapping; AMS-only [5] and mixed [5,-1] mappings fall through.
                    captured = self._captured_ams_mapping
                    if captured and all(s == -1 for s in captured):
                        if self.state.tray_now != 254:
                            logger.debug(
                                f"[{self.serial_number}] tray_now external-spool override (#1822): "
                                f"slot {parsed_tray_now} -> 254"
                            )
                        self.state.tray_now = 254
                    else:
                        # Single-nozzle printer with tray_now in 0-3 range.
                        # P2S (and possibly other models) with multiple AMS units sends LOCAL slot IDs
                        # in tray_now, not global tray IDs (#420). Use the MQTT mapping field
                        # (snow-encoded) to resolve the correct AMS unit.
                        ams_exist_raw = ams_data.get("ams_exist_bits", "0")
                        try:
                            ams_exist = int(ams_exist_raw, 16) if isinstance(ams_exist_raw, str) else int(ams_exist_raw)
                        except (ValueError, TypeError):
                            ams_exist = 0
                        num_ams = bin(ams_exist).count("1")

                        if self._has_a2l_am_unit and num_ams <= 1:
                            # A2L AMS-Lite (normalised unit 6): the firmware reports
                            # tray_now as a LOCAL 0-3 slot, so globalise to 24+slot —
                            # otherwise usage tracking keys the wrong spool (it would
                            # deduct from AMS 0's slot).
                            self.state.tray_now = A2L_LITE_GLOBAL_BASE + parsed_tray_now
                        elif num_ams > 1:
                            # Multiple AMS on single-nozzle - tray_now is likely a local slot ID.
                            # Cross-reference with MQTT mapping field to find the correct AMS unit.
                            if self._has_a2l_am_unit:
                                # A2L Lite + a regular AMS attached together is out of
                                # scope: the flat mapping ids are unknown for that combo
                                # and could collide with AMS 0. Fall through to the
                                # mapping-based resolve, but warn — a capture is needed.
                                logger.warning(
                                    "[%s] A2L AMS-Lite alongside another AMS unit is unsupported — "
                                    "tray_now resolution may be wrong (needs a mixed-setup capture)",
                                    self.serial_number,
                                )
                            mapping_raw = self.state.raw_data.get("mapping")
                            resolved = self._resolve_local_slot_from_mapping(parsed_tray_now, mapping_raw)
                            if resolved is not None:
                                if resolved != parsed_tray_now:
                                    logger.debug(
                                        f"[{self.serial_number}] Multi-AMS tray_now: "
                                        f"local slot {parsed_tray_now} -> global ID {resolved} (from mapping)"
                                    )
                                self.state.tray_now = resolved
                            else:
                                # No mapping available (not printing, or ambiguous) - use as-is.
                                # This matches the old behavior and is correct for AMS 0.
                                self.state.tray_now = parsed_tray_now
                        else:
                            # Single AMS - local slot 0-3 equals global ID
                            self.state.tray_now = parsed_tray_now
                else:
                    # tray_now > 3 means it's already a global ID, or 255 means unloaded
                    # Note: Do NOT clear pending_tray_target on tray_now=255 here.
                    # During filament change, the printer sends 255 first (unload), then the slot.
                    # We only clear pending_tray_target explicitly in ams_unload_filament().
                    # Trust the printer's reported value.
                    self.state.tray_now = parsed_tray_now

                # Track last valid tray for usage tracking (survives retract → 255 at print end)
                # Valid physical trays: 0-15 (regular AMS), 24-27 (A2L AMS-Lite,
                # normalised unit 6), 128-135 (AMS-HT), 254 (external spool)
                tn = self.state.tray_now
                if (
                    (0 <= tn <= 15)
                    or (A2L_LITE_GLOBAL_BASE <= tn <= A2L_LITE_GLOBAL_BASE + 3)
                    or (128 <= tn <= 135)
                    or tn == 254
                ):
                    # Log tray change for mid-print usage splitting. Gate on
                    # the print-lifecycle flags (`_was_running` set on first
                    # RUNNING / new print, `_completion_triggered` set when
                    # on_print_complete fires) instead of ``state in
                    # ("RUNNING", "PAUSE")`` — P2S firmware briefly transitions
                    # out of RUNNING during AMS auto-fallback (#957), so a
                    # literal-string gate misses the switch and the usage
                    # tracker double-credits at completion (original tray gets
                    # the full 3MF estimate via slot_to_tray + the AMS
                    # remain%-delta path adds the fallback weight on top).
                    if tn != self.state.last_loaded_tray and self._was_running and not self._completion_triggered:
                        self.state.tray_change_log.append((tn, self.state.layer_num))
                        logger.info(
                            "[%s] Tray change during print: tray=%d at layer=%d",
                            self.serial_number,
                            tn,
                            self.state.layer_num,
                        )
                    self.state.last_loaded_tray = self.state.tray_now

                logger.debug("[%s] tray_now updated: %s", self.serial_number, self.state.tray_now)

            # NOTE: ams_status is parsed BEFORE tray_now (see above) to ensure correct
            # state when checking filament change mode for H2D disambiguation

            # P1S/P1P send partial updates without "ams" key - this is valid, not an error
            # We've already processed the status fields above, so just return if no ams list
            if ams_list is None:
                logger.debug("[%s] AMS partial update (no tray data)", self.serial_number)
                return
        elif isinstance(ams_data, list):
            ams_list = ams_data
            self._normalize_a2l_am_units(ams_list)
        else:
            logger.warning("[%s] Unexpected AMS data format: %s", self.serial_number, type(ams_data))
            return

        # Merge AMS data instead of replacing, to handle partial updates
        # During prints, the printer may only send updates for active AMS units
        # We need deep merging at the tray level to preserve fields like tray_sub_brands
        existing_ams = self.state.raw_data.get("ams", [])
        existing_by_id = {ams.get("id"): ams for ams in existing_ams if ams.get("id") is not None}

        # Update existing units with new data, add new units
        for ams_unit in ams_list:
            ams_id = ams_unit.get("id")
            if ams_id is not None:
                existing_unit = existing_by_id.get(ams_id)
                if existing_unit and "tray" in ams_unit:
                    # Deep merge trays to preserve fields from previous updates
                    existing_trays = {t.get("id"): t for t in existing_unit.get("tray", []) if t.get("id") is not None}
                    merged_trays = []
                    for new_tray in ams_unit.get("tray", []):
                        tray_id = new_tray.get("id")
                        if tray_id is not None and tray_id in existing_trays:
                            # Merge: start with existing, update with new non-empty values
                            merged_tray = existing_trays[tray_id].copy()
                            # Detect slot-clearing updates (spool removal):
                            # When tray_type is explicitly empty, clear everything
                            # including RFID data (tag_uid/tray_uuid).
                            slot_clearing = new_tray.get("tray_type") == ""
                            # Some printers (e.g. H2D) only send {id, state} in
                            # incremental updates when a tray is not fully loaded.
                            # state=11 means loaded; other values (9=empty,
                            # 10=spool present but filament not in feeder) indicate
                            # the slot should be cleared.  Without this, old
                            # tray_type/tray_color persist indefinitely (#784).
                            #
                            # BUT this is regular-AMS semantics. An AMS-HT
                            # (single-tray high-temp dry box, id >= 128) reports
                            # its loaded tray as state=9, not 11 — it doesn't feed
                            # filament into a shared buffer the way a 4-slot AMS
                            # does. Applying the `state != 11 → empty` rule to an
                            # HT unit wiped a present spool on every power-on, when
                            # the printer sends a partial {id, state=9} for the HT
                            # tray (upstream #2594). Skip the state heuristic for
                            # HT units — a genuine HT spool removal still clears via
                            # the explicit tray_type=="" case above and the
                            # tray_exist_bits cleanup.
                            try:
                                _is_ht_unit = int(ams_id) >= 128
                            except (TypeError, ValueError):
                                _is_ht_unit = False
                            tray_state = new_tray.get("state")
                            if (
                                tray_state is not None
                                and tray_state != 11
                                and not _is_ht_unit
                                and "tray_type" not in new_tray
                                and merged_tray.get("tray_type")
                            ):
                                logger.info(
                                    "[%s] AMS %s tray %s: state=%s (not loaded) - clearing stale tray data",
                                    self.serial_number,
                                    ams_id,
                                    tray_id,
                                    tray_state,
                                )
                                slot_clearing = True
                                # The incremental update only has {id, state} - inject
                                # empty values for all content fields so the merge loop
                                # below clears the stale data from merged_tray.
                                new_tray.update(
                                    {
                                        "tray_type": "",
                                        "tray_sub_brands": "",
                                        "tray_color": "",
                                        "tray_id_name": "",
                                        "tray_info_idx": "",
                                        "tag_uid": "0000000000000000",
                                        "tray_uuid": "00000000000000000000000000000000",
                                        "remain": 0,
                                        "k": None,
                                        "cali_idx": None,
                                    }
                                )
                            for key, value in new_tray.items():
                                # Fields that should always be updated (even with empty/zero values):
                                # - remain, k, id, cali_idx: status indicators where 0 is valid
                                # - tray_type, tray_sub_brands, tray_info_idx, tray_color,
                                #   tray_id_name: slot content indicators that must be cleared
                                #   when a spool is removed (fixes #147 - old AMS empty slot)
                                # NOTE: tag_uid and tray_uuid are NOT in always_update_fields.
                                # They are only cleared during spool removal (slot_clearing=True).
                                # Periodic AMS updates often include empty RFID fields which
                                # would overwrite valid data from the initial pushall.
                                always_update_fields = (
                                    "remain",
                                    "k",
                                    "id",
                                    "cali_idx",
                                    "tray_type",
                                    "tray_sub_brands",
                                    "tray_info_idx",
                                    "tray_color",
                                    "tray_id_name",
                                )
                                if (
                                    key in always_update_fields
                                    or slot_clearing
                                    or value
                                    not in (
                                        None,
                                        "",
                                        "0000000000000000",
                                        "00000000000000000000000000000000",
                                    )
                                ):
                                    merged_tray[key] = value
                            merged_trays.append(merged_tray)
                        else:
                            merged_trays.append(new_tray)
                    # Update ams_unit with merged trays. Spread existing_unit
                    # FIRST so top-level fields the partial update omits —
                    # dry_time, info (which drives dry_status / dry_sub_status),
                    # humidity, temp — are preserved instead of dropped. The
                    # printer sends tray-bearing partials that carry no drying
                    # fields; without this, dry_time reads as absent → 0 and the
                    # falling-edge detector below fires a false "drying complete"
                    # (#1462). Mirrors the no-tray branch's merge semantics.
                    ams_unit = {**existing_unit, **ams_unit, "tray": merged_trays}
                elif existing_unit:
                    # Partial update without tray data: merge new fields into existing
                    # unit to preserve tray, sn, sw_ver, and other accumulated data.
                    ams_unit = {**existing_unit, **ams_unit}
                existing_by_id[ams_id] = ams_unit

        # Convert back to list, sorted by ID for consistent ordering
        merged_ams = sorted(existing_by_id.values(), key=lambda x: x.get("id", 0))

        # Empty-slot cleanup via tray_exist_bits (#147, #1322, #765, #1365).
        # Shared with the VP bridge cache so the slicer-facing view stays in
        # sync with BamDude's AMS card (upstream Bambuddy #1726). See the
        # helper's docstring for the full rationale and the printer-shutdown
        # guard.
        if isinstance(ams_data, dict):
            apply_tray_exist_bits(
                merged_ams,
                ams_data.get("tray_exist_bits"),
                power_on_flag=ams_data.get("power_on_flag", True),
                log_label=self.serial_number,
                annotate_exists=True,
            )

        self.state.raw_data["ams"] = merged_ams

        # Detect AMS drying-complete falling edge per-unit (#1349). When an
        # AMS's ``dry_time`` transitions from >0 to 0 the cycle just
        # finished — fire the callback so smart-plug auto-off-after-drying
        # can run. Works identically for queue-triggered, ambient, and
        # manual drying because we observe the firmware-reported state,
        # not our own intent.
        if self.on_drying_complete:
            for ams_unit in merged_ams:
                try:
                    ams_id = int(ams_unit.get("id", -1))
                except (TypeError, ValueError):
                    continue
                if ams_id < 0:
                    continue
                # Only evaluate the edge when this update carries an explicit
                # dry_time. An absent / unparseable value is NOT zero — treating
                # it as 0 lets a tray-only partial fake a drying-complete edge
                # (#1462). Skip without touching the remembered value so the
                # next update that DOES carry dry_time sees the true previous.
                raw_dry_time = ams_unit.get("dry_time")
                if raw_dry_time is None:
                    continue
                try:
                    current = int(raw_dry_time)
                except (TypeError, ValueError):
                    continue
                previous = self._previous_dry_times.get(ams_id, 0)
                self._previous_dry_times[ams_id] = current
                if previous > 0 and current == 0:
                    logger.info(
                        "[%s] AMS %d drying complete (dry_time %d → 0)",
                        self.serial_number,
                        ams_id,
                        previous,
                    )
                    self.on_drying_complete(ams_id)

        # Drop cached drying-target params for any AMS that has stopped drying
        # (dry_time back to 0), so the badge stops advertising a finished cycle.
        # Runs independently of the on_drying_complete callback above — the badge
        # already hides itself once dry_time hits 0, this is cache hygiene.
        if self._drying_targets:
            for ams_unit in merged_ams:
                raw_dry = ams_unit.get("dry_time")
                if raw_dry is None:
                    continue
                try:
                    if int(raw_dry) == 0:
                        self._drying_targets.pop(int(ams_unit.get("id", -1)), None)
                except (TypeError, ValueError):
                    continue

        # Apply cached AMS firmware/SN from get_version (handles ordering and id type mismatches)
        self._apply_ams_version_cache(merged_ams)
        # Update timestamp for RFID refresh detection (frontend can detect "new data arrived")
        self.state.last_ams_update = time.time()
        logger.debug("[%s] Merged AMS data: %s new units, %s total", self.serial_number, len(ams_list), len(merged_ams))

        # Extract ams_extruder_map from each AMS unit's info field
        # BambuStudio DevFilaSystem.cpp parses info as hex string:
        #   type_id    = get_flag_bits(info, 0, 4)   // bits 0-3: AMS type
        #   extruder_id = get_flag_bits(info, 8, 4)  // bits 8-11: extruder assignment
        # where get_flag_bits uses std::stoull(str, nullptr, 16) - hex parsing.
        # extruder_id: 0=right/main, 1=left/deputy, 0xE=uninitialized (skip)
        #
        # Use merged_ams (not ams_list) to avoid partial MQTT updates overwriting
        # the full map. Merge into existing map to preserve entries from prior updates.

        ams_extruder_map = dict(self.state.ams_extruder_map) if self.state.ams_extruder_map else {}
        for ams_unit in merged_ams:
            ams_id = ams_unit.get("id")
            info = ams_unit.get("info")
            if ams_id is not None and info is not None:
                try:
                    # info is a hex-encoded string in MQTT JSON (e.g. "10001003")
                    info_val = int(str(info), 16)
                    # Extract 4 bits starting at bit 8 for extruder assignment
                    extruder_id = (info_val >> 8) & 0xF
                    if extruder_id == 0xE:
                        # 0xE = uninitialized AMS, skip
                        continue
                    ams_extruder_map[str(ams_id)] = extruder_id
                    logger.debug(f"[{self.serial_number}] AMS {ams_id} info=0x{info} -> extruder {extruder_id}")
                except (ValueError, TypeError):
                    pass  # Skip AMS units with unparseable info bitmask values
        if ams_extruder_map:
            self.state.raw_data["ams_extruder_map"] = ams_extruder_map
            self.state.ams_extruder_map = ams_extruder_map
            logger.debug("[%s] ams_extruder_map: %s", self.serial_number, ams_extruder_map)

        # Extract drying status from info hex string and dry_sf_reason per AMS unit
        # BambuStudio DevFilaSystem.cpp parses info bits:
        #   dry_status     = get_flag_bits(info, 4, 4)   // bits 4-7
        #   dry_sub_status = get_flag_bits(info, 22, 4)  // bits 22-25
        for ams_unit in merged_ams:
            info = ams_unit.get("info")
            if info is not None:
                try:
                    info_val = int(str(info), 16)
                    ams_unit["dry_status"] = (info_val >> 4) & 0xF
                    ams_unit["dry_sub_status"] = (info_val >> 22) & 0xF
                except (ValueError, TypeError):
                    pass  # Skip unparseable info values
            # dry_sf_reason is a per-unit array of cannot-dry reason codes
            if "dry_sf_reason" in ams_unit:
                sf_reason = ams_unit["dry_sf_reason"]
                if isinstance(sf_reason, list):
                    ams_unit["dry_sf_reason"] = [
                        int(r) for r in sf_reason if isinstance(r, int) or (isinstance(r, str) and r.isdigit())
                    ]
                else:
                    ams_unit["dry_sf_reason"] = []

        # Persist updated drying fields back to raw_data
        self.state.raw_data["ams"] = merged_ams

        # Create a hash of relevant AMS data to detect changes.
        #
        # Hash the MERGED state, not the raw incoming payload. A removal that the
        # firmware signals ONLY through tray_exist_bits — still echoing the old
        # tray_type, tag_uid and remain in the payload, which is exactly what an
        # AMS-HT does — is cleared in merged_ams by apply_tray_exist_bits above,
        # while the raw payload's fields sit unchanged. Those three fields are
        # precisely what this hash is built from, so a raw-based hash never
        # flipped: on_ams_change never fired and the spool_assignment row stayed
        # bound to a slot that is now empty (upstream #2670).
        #
        # merged_ams also always spans every unit, so a partial single-unit
        # update cannot hash differently from a full pushall carrying the same
        # state — which a raw-based hash would.
        ams_hash_data = []
        for ams_unit in merged_ams:
            for tray in ams_unit.get("tray", []):
                # Include fields that matter for filament tracking
                ams_hash_data.append(
                    f"{ams_unit.get('id')}:{tray.get('id')}:"
                    f"{tray.get('tray_type')}:{tray.get('tag_uid')}:{tray.get('remain')}"
                )
        ams_hash = hashlib.md5(":".join(ams_hash_data).encode(), usedforsecurity=False).hexdigest()

        # Only trigger callback if AMS data actually changed
        if ams_hash != self._previous_ams_hash:
            self._previous_ams_hash = ams_hash
            if self.on_ams_change:
                logger.debug("[%s] AMS data changed, triggering sync callback", self.serial_number)
                # Pass merged AMS data (not raw ams_list) - partial MQTT updates
                # may lack fields like 'remain' that the merged state preserves
                self.on_ams_change(merged_ams)

        # Upstream #2582: read-back check runs on EVERY AMS push, not just hash
        # changes. The change hash keys on tray_type/tag_uid/remain — NOT
        # tray_info_idx or cali_idx — so an assignment that only swaps the
        # filament id on an already-loaded slot would not flip the hash, and
        # gating the check on it would miss exactly the confirmation we're after.
        if self._pending_assignments:
            self._check_assignment_verifications()

    def register_assignment_verification(
        self,
        ams_id: int,
        tray_id: int,
        tray_info_idx: str,
        tray_color: str,
        cali_idx: int | None,
    ) -> None:
        """Record an assignment we just pushed so subsequent AMS telemetry can
        confirm the tray actually accepted it (upstream #2582).

        Called right after ``ams_set_filament_setting`` + ``extrusion_cali_sel``.
        ``tray_info_idx`` is the primary signal — the slicer/printer echoes the
        accepted filament id back in the per-tray push, so a match means the
        setting landed. ``cali_idx`` (when >= 0) is verified as a secondary signal
        so we can specifically flag "filament loaded but K-profile not applied".

        A blank ``tray_info_idx`` means we had nothing resolvable to send, so
        there is nothing to verify and no record is stored.
        """
        want_idx = (tray_info_idx or "").strip().upper()
        if not want_idx:
            return
        self._pending_assignments[(ams_id, tray_id)] = {
            "tray_info_idx": want_idx,
            "tray_color": (tray_color or "").strip().upper(),
            "cali_idx": cali_idx,
            "deadline": time.monotonic() + self.ASSIGNMENT_VERIFY_TIMEOUT,
            "last_seen_idx": None,
        }

    def note_assignment_cali_idx(self, ams_id: int, tray_id: int, cali_idx: int | None) -> None:
        """Attach the K-profile index actually pushed to a pending verification.

        BamDude divergence from upstream #2582: our assign path delegates the
        ``extrusion_cali_sel`` push to ``calibration_service.
        apply_active_calibration_to_slot``, which re-resolves the LIVE ``cali_idx``
        against the printer's current K-profile list (the stored one is only a
        hint), so the caller that registers the verification doesn't know it yet.
        This lets the calibration helper hand the value back, enabling the
        secondary "filament loaded but K-profile not applied" check. No-op when
        no verification is pending for that slot.
        """
        pending = self._pending_assignments.get((ams_id, tray_id))
        if pending is not None:
            pending["cali_idx"] = cali_idx

    def _find_verify_tray(self, ams_id: int, tray_id: int) -> dict | None:
        """Locate the live tray dict for a pending verification.

        External spools (``ams_id`` 255) live in ``vt_tray`` under global ids
        254/255; regular and HT AMS trays live under ``ams[].tray[]``. HT units
        report a single tray whose id may not equal the logical ``tray_id``, so
        fall back to the sole tray when an id match fails.
        """
        raw = self.state.raw_data or {}
        if ams_id == 255:
            want_ext = 254 + tray_id
            for vt in raw.get("vt_tray", []) or []:
                if isinstance(vt, dict) and str(vt.get("id")) == str(want_ext):
                    return vt
            return None
        for unit in raw.get("ams", []) or []:
            if str(unit.get("id")) != str(ams_id):
                continue
            trays = unit.get("tray", []) or []
            for tray in trays:
                if str(tray.get("id")) == str(tray_id):
                    return tray
            if ams_id >= 128 and len(trays) == 1:
                return trays[0]
            return None
        return None

    def _check_assignment_verifications(self) -> None:
        """Compare each pending assignment against live tray telemetry and fire
        ``on_assignment_verified`` on a match or once the deadline passes.

        Runs on every AMS push. Non-matching-but-still-within-window entries are
        left in place for the next push. The timeout branch only fires when a
        later push arrives after the deadline; if the printer goes silent we
        simply never confirm, which is preferable to inventing a failure.
        """
        now = time.monotonic()
        for key, want in list(self._pending_assignments.items()):
            ams_id, tray_id = key
            tray = self._find_verify_tray(ams_id, tray_id)
            actual_idx = str((tray or {}).get("tray_info_idx") or "").strip().upper()
            if tray is not None and actual_idx:
                want["last_seen_idx"] = actual_idx
            if actual_idx and actual_idx == want["tray_info_idx"]:
                self._pending_assignments.pop(key, None)
                kprofile_applied = True
                want_cali = want.get("cali_idx")
                if want_cali is not None and want_cali >= 0:
                    kprofile_applied = tray.get("cali_idx") == want_cali
                self._fire_assignment_verified(
                    ams_id,
                    tray_id,
                    True,
                    {"tray_info_idx": actual_idx, "kprofile_applied": kprofile_applied},
                )
            elif now >= want["deadline"]:
                self._pending_assignments.pop(key, None)
                self._fire_assignment_verified(
                    ams_id,
                    tray_id,
                    False,
                    {
                        "expected_tray_info_idx": want["tray_info_idx"],
                        "actual_tray_info_idx": want.get("last_seen_idx"),
                        # True when we saw the tray at least once (so the push
                        # channel is alive and the printer really stored a
                        # different/blank id) vs never observing it at all.
                        "saw_tray": want.get("last_seen_idx") is not None,
                    },
                )

    def _fire_assignment_verified(self, ams_id: int, tray_id: int, verified: bool, detail: dict) -> None:
        if verified:
            logger.info(
                "[%s] Assignment verified: AMS%d-T%d now reports %s (kprofile_applied=%s)",
                self.serial_number,
                ams_id,
                tray_id,
                detail.get("tray_info_idx"),
                detail.get("kprofile_applied"),
            )
        else:
            logger.warning(
                "[%s] Assignment NOT confirmed: AMS%d-T%d expected %s, tray shows %s (saw_tray=%s)",
                self.serial_number,
                ams_id,
                tray_id,
                detail.get("expected_tray_info_idx"),
                detail.get("actual_tray_info_idx"),
                detail.get("saw_tray"),
            )
        if self.on_assignment_verified:
            try:
                self.on_assignment_verified(ams_id, tray_id, verified, detail)
            except Exception:
                logger.exception("[%s] on_assignment_verified callback failed", self.serial_number)

    def _update_state(self, data: dict):
        """Update printer state from message data."""
        _previous_state = self.state.state

        # Calibration capability flags from push (m062 / Plan 1). Modern
        # firmware sends explicit boolean fields; legacy X1 advertises via
        # ``func`` bitfield bits 15 (flow) / 16 (PA).
        if isinstance(data.get("support_pa_calibration"), bool):
            self.state.is_support_pa_calibration = bool(data["support_pa_calibration"])
        if isinstance(data.get("support_auto_flow_calibration"), bool):
            self.state.is_support_auto_flow_calibration = bool(data["support_auto_flow_calibration"])
        # ⚠️ This used to read a top-level ``func`` int at bits 15/16, citing BS.
        # There is no such field: BS never reads a top-level ``func`` (its only
        # ``"func"`` is ``part.func`` INSIDE an airduct part), and the two bit
        # positions belong to two different fields it does read. It also OR'd,
        # so a capability once seen could never be withdrawn.
        #
        # BS has three sources, later parse winning (DeviceManager.cpp):
        #
        #   home_flag  bit 15 -> flow, bit 16 -> pa   (legacy, parse_home_flag)
        #   fun        bit  6 -> flow, bit  7 -> pa   (new protocol, hex string)
        #   json ``support_flow_calibration`` -> pa   (see note below)
        #
        # and clamps each of the first two — see _apply_series_calibration_clamps.
        _home_flag_raw = data.get("home_flag")
        if isinstance(_home_flag_raw, int):
            _hf = _home_flag_raw & 0xFFFFFFFF if _home_flag_raw < 0 else _home_flag_raw
            self.state.is_support_auto_flow_calibration = bool((_hf >> 15) & 0x1)
            self.state.is_support_pa_calibration = bool((_hf >> 16) & 0x1)
            self._apply_series_calibration_clamps()
            # BS ``parse_home_flag``: ``is_220V_voltage = get_flag_bits(flag, 3)``.
            # Feeds the bed ceiling, which is LOWER at 220 V on the X1 and O
            # series — see ``utils.temperature_limits.bed_limits``.
            self.state.is_220v = bool((_hf >> 3) & 0x1)
            # Bits 0/1/2 — which axes are homed (BS ``DevAxis::IsAxisAtHomeX/Y/Z``).
            #
            # ⚠️ **Zero means all three ARE home.** BS writes each accessor as
            # ``m_home_flag == 0 ? true : (bit)``, so a flag of exactly 0 is the
            # "nothing reported" sentinel and must NOT be read as "nothing
            # homed" — that would refuse a jog on every printer omitting the
            # field. The whole word is the sentinel, not the individual bit.
            if _hf == 0:
                self.state.axis_at_home = {"x": True, "y": True, "z": True}
            else:
                self.state.axis_at_home = {
                    "x": bool(_hf & 0x1),
                    "y": bool((_hf >> 1) & 0x1),
                    "z": bool((_hf >> 2) & 0x1),
                }

        _fun_bits = parse_hex_bitfield(data.get("fun"))
        if _fun_bits is not None:
            self.state.is_support_auto_flow_calibration = bool((_fun_bits >> 6) & 0x1)
            self.state.is_support_pa_calibration = bool((_fun_bits >> 7) & 0x1)
            self._apply_series_calibration_clamps()

        # Room left for a timelapse, as the camera reports it. BS keeps these in
        # ``DevStorage`` and never asks for them — ``ipcam_get_media_info`` is a
        # separate question, and this is the number the pre-print warning reads.
        _cam = (data.get("device") or {}).get("cam") if isinstance(data.get("device"), dict) else None
        if isinstance(_cam, dict):
            for _key in ("tl_internal_free_kb", "tl_internal_total_kb", "tl_external_free_kb", "tl_external_total_kb"):
                if isinstance(_cam.get(_key), int) and not isinstance(_cam.get(_key), bool):
                    self.state.timelapse_storage[_key] = _cam[_key]
            # Where the finished recording actually went, absolute, named by the
            # printer itself — ``/userdata/media/timelapse/…`` for internal,
            # ``/media/usb0/timelapse/…`` for the card. Measured on X2D
            # 2026-08-14 across four prints on both media.
            #
            # ⚠️ **Cleared to "" the moment a print starts** and filled once the
            # file is closed, so an empty value means "nothing finished yet",
            # not "no camera". Kept verbatim rather than reduced to a medium:
            # the filename is the half that ends the guessing, since finding a
            # recording by timestamp is what it replaces.
            _path = _cam.get("timelapse_path")
            if isinstance(_path, str):
                self.state.timelapse_path = _path

        # BS ``check_enable_np`` — the print payload carrying all four of ``cfg``,
        # ``fun``, ``aux`` and ``stat``. Gates the per-extruder
        # ``set_extrusion_length`` over the g-code E move.
        #
        # ⚠️ **STICKY, and BS's own is not.** BS re-runs this on every push and
        # lets a sparse message set it back to False, which would make the
        # extruder command flip protocol between one message and the next. Which
        # protocol a machine speaks is a property of its firmware, not of the
        # message that happened to arrive — so once seen it stays, the same
        # reasoning as ``is_nozzle_flow_type_supported`` above.
        if all(k in data for k in ("cfg", "fun", "aux", "stat")):
            self.state.enable_np = True

        # What the heaters will accept, as reported. BS keeps these as parsed
        # vectors and asks their size before trusting them, so a malformed range
        # has to end up indistinguishable from an absent one — hence storing the
        # raw value and letting ``temperature_limits`` do the judging.
        for _field in ("nozzle_temp_range", "bed_temp_range"):
            _raw = data.get(_field)
            if isinstance(_raw, list):
                setattr(self.state, _field, _raw)
        if isinstance(data.get("bed_temperature_limit"), int) and not isinstance(
            data.get("bed_temperature_limit"), bool
        ):
            self.state.bed_temperature_limit = data["bed_temperature_limit"]

        # NOT ported: BS also does
        #   is_support_pa_calibration = jj["support_flow_calibration"]
        # — assigning the **flow** key to the **pa** variable, with no
        # counterpart reading a pa key at all. That reads as a slip in BS rather
        # than a protocol fact, and mirroring it would switch PA support on from
        # a flow flag. Left alone deliberately; revisit if a capture shows the
        # firmware really only sends the flow key.

        # BS's legacy ``flag`` bitfield IS the printer's ``home_flag`` field
        # (BS DeviceManager.cpp:1083 ``m_home_flag = flag``). P1/X1-series
        # printers (e.g. P1S) advertise motor-noise cali support here at BIT 21
        # (BS line 1116: ``is_support_motor_noise_cali = ((flag >> 21) & 0x1)``),
        # NOT via the newer ``fun`` string bit 10 that H2/X2-series use. Parse it
        # so those models expose Motor Noise Cancellation exactly like BS. Runs
        # before the ``fun`` parse below, so ``fun`` wins when a printer sends
        # both (mirrors BS: flag@1116 parsed before fun@4385).
        if isinstance(data.get("home_flag"), int):
            self.state.device_cali_support["support_motor_noise_cali"] = bool((int(data["home_flag"]) >> 21) & 0x1)

        # Device-calibration support flags (Device page → Calibration dialog).
        # Modern firmware sends explicit bool/int fields; only stash the keys the
        # printer actually reported. The API resolver merges these OVER the
        # mirrored per-model config base (hybrid gating; see printer_configs.py).
        for _cali_flag in (
            "support_lidar_calibration",
            "support_ai_monitoring",
            "support_nozzle_offset_calibration",
            "support_high_tempbed_calibration",
            "support_clump_position_calibration",
            "support_motor_noise_cali",
        ):
            if isinstance(data.get(_cali_flag), bool):
                self.state.device_cali_support[_cali_flag] = bool(data[_cali_flag])
        if isinstance(data.get("support_bed_leveling"), int):
            self.state.device_cali_support["support_bed_leveling"] = int(data["support_bed_leveling"])
        # Motor-noise cali support is a LIVE-runtime gate in BS, not a static
        # per-model flag: most models don't send an explicit bool and instead
        # advertise it via the ``fun`` function bitfield, bit 10 — exactly what
        # BS reads (DeviceManager.cpp:4385:
        # ``is_support_motor_noise_cali = get_flag_bits(fun, 10)``, where
        # ``get_flag_bits(str, 10)`` == ``(int(str, 16) >> 10) & 1``). ``fun`` is
        # a hex string (occasionally already an int). Parsed AFTER the explicit
        # bool above so ``fun`` wins when both are present (BS ordering). This
        # applies to every model, so P1S/X2D/etc. now expose it like BS does.
        _fun = data.get("fun")
        if _fun is not None:
            try:
                _fun_int = _fun if isinstance(_fun, int) else int(str(_fun), 16)
                self.state.device_cali_support["support_motor_noise_cali"] = bool((_fun_int >> 10) & 0x1)
                # Safety tab support bits (BS): fun bit 12 = open-door check,
                # bit 62 = idle heating protection.
                self.state.print_options.support_open_door = bool((_fun_int >> 12) & 0x1)
                self.state.print_options.support_idle_heating = bool((_fun_int >> 62) & 0x1)
            except (ValueError, TypeError):
                pass

        # Detect cali completion from ``mc_print_stage`` IDLE flip while a
        # cali session is active. BS DeviceManager.cpp:1003 uses the same
        # heuristic: ``mc_print_stage == 1`` AND ``gcode_file`` contains
        # "auto_cali" → cali finished. We mark status=completed so the wizard
        # frontend can advance from Running to the appropriate Save page.
        if isinstance(data.get("mc_print_stage"), (int, str)):
            try:
                stage_val = int(data["mc_print_stage"])
            except (ValueError, TypeError):
                stage_val = -1
            gcode_file = data.get("gcode_file") or self.state.gcode_file or ""
            if stage_val == 1 and self.state.extrusion_cali_status == "running" and "auto_cali" in str(gcode_file):
                self.state.extrusion_cali_status = "completed"

        # Update state fields
        if "gcode_state" in data:
            self.state.state = data["gcode_state"]
        if "gcode_file" in data:
            self.state.gcode_file = data["gcode_file"]
            self.state.current_print = data["gcode_file"]
        if "subtask_name" in data:
            self.state.subtask_name = data["subtask_name"]
            # Prefer subtask_name as current_print if available
            if data["subtask_name"]:
                self.state.current_print = data["subtask_name"]
        if "subtask_id" in data:
            self.state.subtask_id = data["subtask_id"]

        # Connect-edge print reconciliation — the first full status after each
        # fresh connect. A print that finished while BamDude was stopped or
        # disconnected arrives here with no RUNNING history, so live completion
        # detection cannot fire; the reconcile sweep closes it instead. The flag
        # is a per-client one-shot (so only the FIRST status of this connection
        # fires it) but is re-armed on each new client in carry_print_lifecycle_from,
        # so a later disconnect/reconnect re-runs the sweep (#1542 follow-up).
        if not self._startup_reconcile_done and "gcode_state" in data and "gcode_file" in data and self.on_first_status:
            self._startup_reconcile_done = True
            # subtask_name is the reconcile fallback identity for H2/X-series
            # firmware, whose gcode_file is a generic /data/Metadata/plate_N.gcode.
            self.on_first_status(
                self.state.state,
                self.state.gcode_file or "",
                self.state.subtask_id or "",
                self.state.subtask_name or "",
            )

        if "mc_percent" in data:
            # Save last non-zero progress for usage tracking (firmware resets to 0 on cancel)
            if self.state.progress > 0:
                self._last_valid_progress = self.state.progress
            self.state.progress = float(data["mc_percent"])
        if "mc_remaining_time" in data:
            self.state.remaining_time = int(data["mc_remaining_time"])
        if "mc_print_sub_stage" in data:
            new_sub_stage = int(data["mc_print_sub_stage"])
            if new_sub_stage != self.state.mc_print_sub_stage:
                logger.debug(
                    f"[{self.serial_number}] mc_print_sub_stage changed: "
                    f"{self.state.mc_print_sub_stage} -> {new_sub_stage}"
                )
            self.state.mc_print_sub_stage = new_sub_stage
        if "layer_num" in data:
            new_layer = int(data["layer_num"])
            old_layer = self.state.layer_num
            # Save last non-zero layer for usage tracking (firmware resets to 0 on cancel)
            if old_layer > 0:
                self._last_valid_layer_num = old_layer
            self.state.layer_num = new_layer
            # Trigger layer change callback if layer increased. Both edges are
            # reported: a "fire at layer N" consumer needs to know whether the
            # print crossed N, and dropped reports mean it can jump over it.
            if new_layer > old_layer and self.on_layer_change:
                self.on_layer_change(new_layer, old_layer)
            # Finish-photo on the last-layer edge (#1867). On models that skip the
            # stg_cur=22 stage (e.g. A1 Mini) the stage-22 trigger below never fires and
            # the FINISH fallback only lands after the user's End G-code has run (parked /
            # swapped / cleared plate). Catching the crossing into the final layer captures
            # the real print while it's still on the bed. One-shot via _finish_photo_captured
            # so the stage-22 / FINISH paths become no-ops for this print.
            total_for_finish = self.state.total_layers or 0
            if (
                total_for_finish > 0
                and new_layer >= total_for_finish
                and old_layer < total_for_finish
                and self._was_running
                and not self._finish_photo_captured
                and self.on_finish_photo_moment
            ):
                self._finish_photo_captured = True
                logger.info(
                    "[%s] FINISH PHOTO MOMENT (last-layer) — layer=%s/%s, timelapse_active=%s",
                    self.serial_number,
                    new_layer,
                    total_for_finish,
                    self._timelapse_during_print,
                )
                self.on_finish_photo_moment(
                    {
                        "trigger": "last_layer",
                        "filename": self._previous_gcode_file or self.state.gcode_file,
                        "subtask_name": self.state.subtask_name,
                        "timelapse_was_active": self._timelapse_during_print,
                    }
                )
        if "total_layer_num" in data:
            # Guard against the firmware's end-of-print total_layer_num=0 push clobbering the
            # cached total — a zeroed total zeroed the linear-usage denominator and credited
            # every gram of an AMS-Backup mid-print spool swap to the 2nd spool (#1771).
            new_total = int(data["total_layer_num"])
            if new_total > 0:
                self.state.total_layers = new_total

        # Log fan fields once for debugging
        if not hasattr(self, "_fan_fields_logged"):
            fan_fields = {k: v for k, v in data.items() if "fan" in k.lower()}
            if fan_fields:
                logger.debug("[%s] Fan fields in MQTT data: %s", self.serial_number, fan_fields)
                self._fan_fields_logged = True

        # ⚠️ The scale is decided by WHICH FIELD the number came from, never by
        # how big it is. BS ``DevFan::ParseV1_0`` has two branches and prefers
        # the packed one; ours guessed instead, with ``if speed <= 15`` — so a
        # genuine 10 out of 255 (4 %) was read as gear 10 and shown as 67 %.
        # A magnitude cannot say what scale it is in; its source can.
        _gear_fields = ("cooling_fan_speed", "big_fan1_speed", "big_fan2_speed", "heatbreak_fan_speed")
        if "fan_gear" in data:
            packed = _fan_gear_bytes(data["fan_gear"])
            if packed is not None:
                cooling, aux, chamber = packed
                self.state.cooling_fan_speed = _percent_from_byte(cooling)
                self.state.big_fan1_speed = _percent_from_byte(aux)
                self.state.big_fan2_speed = _percent_from_byte(chamber)
        else:
            for field in _gear_fields:
                if field in data:
                    setattr(self.state, field, self._percent_from_gear(data[field]))

        # Calibration stage tracking
        if "stg_cur" in data:
            new_stg = data["stg_cur"]
            # #1721: capture the stage we're transitioning FROM and the raw
            # incoming stage before the macro-tracking branch below can null
            # `new_stg` out — the finish-photo edge detector needs both.
            prev_stg_for_finish_photo = self.state.stg_cur
            incoming_stg_for_finish_photo = data["stg_cur"]
            if new_stg != self.state.stg_cur:
                logger.debug(
                    "[%s] stg_cur: %s (%s) -> %s (%s)",
                    self.serial_number,
                    self.state.stg_cur,
                    get_stage_name(self.state.stg_cur),
                    new_stg,
                    get_stage_name(new_stg),
                )

            # Macro execution tracking via stg_cur transitions
            # Start marker: stg_cur becomes 0 ("Printing") via claim_action:0
            # End marker: stg_cur becomes -1 or 255 (idle) via claim_action:{idle_stg}
            if self.state.macro_executing:
                if self.state.stg_cur != 0 and new_stg == 0:
                    # Macro started executing - push state to frontend
                    logger.info(
                        "[%s][MACRO] Execution started - stg_cur %s->0, macro='%s'",
                        self.serial_number,
                        self.state.stg_cur,
                        self.state.macro_executing,
                    )
                    self.state.stg_cur = new_stg
                    if self.on_state_change:
                        self.on_state_change(self.state)
                elif self.state.stg_cur == 0 and new_stg != 0:
                    # Macro completed - stg_cur left 0 (goes to -1 or 255 = idle)
                    macro_name = self.state.macro_executing
                    self.state.macro_executing = None
                    self.state.stg_cur = new_stg
                    logger.info(
                        "[%s][MACRO] Execution completed - stg_cur 0->%s, macro='%s'",
                        self.serial_number,
                        new_stg,
                        macro_name,
                    )
                    if self.on_macro_complete:
                        self.on_macro_complete(macro_name, "completed")
                    if self.on_state_change:
                        self.on_state_change(self.state)
                    # Skip normal stg_cur assignment below - already set
                    new_stg = None

            if new_stg is not None:
                self.state.stg_cur = new_stg

            # #1721 end-of-print finish photo trigger.
            # Stage 22 = "Filament unloading" fires at end-of-print AND
            # during mid-print color swaps. The end-of-print gate
            # (progress>=99 / layer>=total / remaining<=0) disambiguates
            # — those signals only line up at the real end. Edge-only
            # (prev != 22) so the trigger fires once per stage entry.
            if (
                incoming_stg_for_finish_photo == 22
                and prev_stg_for_finish_photo != 22
                and self._was_running
                and not self._finish_photo_captured
                and self.on_finish_photo_moment
            ):
                progress = self.state.progress or 0.0
                layer_num = self.state.layer_num or 0
                total_layers = self.state.total_layers or 0
                remaining = self.state.remaining_time or 0
                is_end_of_print = progress >= 99 or (total_layers > 0 and layer_num >= total_layers) or remaining <= 0
                if is_end_of_print:
                    self._finish_photo_captured = True
                    logger.info(
                        "[%s] FINISH PHOTO MOMENT (stage-22) — progress=%s, layer=%s/%s, "
                        "remaining=%smin, timelapse_active=%s",
                        self.serial_number,
                        progress,
                        layer_num,
                        total_layers,
                        remaining,
                        self._timelapse_during_print,
                    )
                    self.on_finish_photo_moment(
                        {
                            "trigger": "stage_22",
                            "filename": self._previous_gcode_file or self.state.gcode_file,
                            "subtask_name": self.state.subtask_name,
                            "timelapse_was_active": self._timelapse_during_print,
                        }
                    )
        if "stg" in data:
            self.state.stg = data["stg"] if isinstance(data["stg"], list) else []

        # Temperature data
        temps = {}
        # Log all fields for debugging dual-nozzle temperature discovery (only once)
        if "bed_temper" in data and not hasattr(self, "_temp_fields_logged"):
            temp_fields = {k: v for k, v in data.items() if "temp" in k.lower() or "chamber" in k.lower()}
            logger.debug("[%s] Temperature-related fields: %s", self.serial_number, temp_fields)
            # Log ALL keys in print data for H2D temperature discovery
            all_keys = sorted(data.keys())
            logger.debug("[%s] ALL print data keys (%s): %s", self.serial_number, len(all_keys), all_keys)
            self._temp_fields_logged = True

        # Log vir_slot data (once) - this may contain per-extruder slot mapping for H2D
        if "vir_slot" in data and not hasattr(self, "_vir_slot_logged"):
            logger.debug("[%s] vir_slot data: %s", self.serial_number, data["vir_slot"])
            self._vir_slot_logged = True

        # Log nozzle hardware info fields (once)
        nozzle_fields = {
            k: v
            for k, v in data.items()
            if "nozzle" in k.lower() or "hw" in k.lower() or "extruder" in k.lower() or "upgrade" in k.lower()
        }
        if nozzle_fields and not hasattr(self, "_nozzle_fields_logged"):
            logger.debug("[%s] Nozzle/hardware fields in MQTT data: %s", self.serial_number, nozzle_fields)
            self._nozzle_fields_logged = True
        # Parse active extruder from device.extruder.state bit 8
        # bit 8 = 0 → RIGHT extruder (active_extruder=0)
        # bit 8 = 1 → LEFT extruder (active_extruder=1)
        if "device" in data and isinstance(data.get("device"), dict):
            device = data["device"]
            # One-shot identification probe: surface whatever the firmware uses to
            # name itself so an unknown model in a support bundle becomes self-
            # diagnosing (A2L #1684 surfaced this — get_version had disconnected).
            # INFO level so it lands in support bundles without debug. Falls back
            # to device.keys() if none of the known fields are present (so a
            # future Bambu rename like `model_name` is still observable).
            if not getattr(self, "_device_id_logged", False):
                id_fields = {
                    k: device.get(k)
                    for k in ("dev_model_name", "dev_product_name", "dev_id", "project_name")
                    if k in device
                }
                if id_fields:
                    logger.info("[%s] Device identification: %s", self.serial_number, id_fields)
                else:
                    logger.info(
                        "[%s] Device identification: no known id fields; device.keys=%s",
                        self.serial_number,
                        sorted(device.keys()),
                    )
                self._device_id_logged = True
            if "extruder" in device and "state" in device["extruder"]:
                state_val = device["extruder"]["state"]
                # ``device.extruder.state`` is a packed bitfield. BS
                # ``DevExtruderSystem.cpp`` reads it as:
                #
                #   bits  0..3  total extruder count
                #   bits  4..7  CURRENT extruder id      <- the one printing now
                #   bits  8..11 TARGET extruder id       <- the one being switched to
                #   bits 12..14 switch state
                #   bits 15..18 currently-loading extruder id
                #   bit  19     busy loading
                #
                # We read ``(state_val >> 8) & 0x1`` — the low bit of the
                # **target** field. While a tool change is in flight target and
                # current disagree, so the spool written to the archive (and
                # pushed to Spoolman) was the one being switched TO, not the one
                # that laid the plastic. On a two-extruder machine the values
                # coincide the rest of the time, which is why it looked fine.
                new_extruder = (state_val >> 4) & 0xF
                if new_extruder != self.state.active_extruder:
                    logger.debug(
                        f"[{self.serial_number}] ACTIVE EXTRUDER CHANGED (state bits 4-7): "
                        f"{self.state.active_extruder} -> {new_extruder} (0=right, 1=left) "
                        f"[state={state_val}, target={(state_val >> 8) & 0xF}]"
                    )
                    self.state.active_extruder = new_extruder

        # Log device.extruder structure for active extruder
        if "device" in data and isinstance(data.get("device"), dict):
            device = data["device"]
            if "extruder" in device:
                ext_data = device["extruder"]
                # Log 'state' field - OrcaSlicer uses bits 12-14 for switch state
                if "state" in ext_data:
                    state_val = ext_data["state"]
                    # Extract bits 12-14 (3 bits) for switch state
                    switch_state = (state_val >> 12) & 0x7
                    logger.debug(
                        f"[{self.serial_number}] device.extruder.state={state_val} (switch_state bits 12-14: {switch_state})"
                    )
                # Log 'cur' field if present (might indicate current/active extruder)
                if "cur" in ext_data:
                    logger.debug("[%s] device.extruder.cur: %s", self.serial_number, ext_data["cur"])

        # Filament Track Switch (FTS) detection — upstream #1162. Presence of
        # device.fila_switch in MQTT means the FTS accessory is installed.
        if "device" in data and isinstance(data.get("device"), dict):
            fs_data = data["device"].get("fila_switch")
            if isinstance(fs_data, dict):
                in_raw = fs_data.get("in")
                out_raw = fs_data.get("out")
                self.state.fila_switch = FilaSwitchState(
                    installed=True,
                    in_slots=list(in_raw) if isinstance(in_raw, list) else [],
                    out_extruders=list(out_raw) if isinstance(out_raw, list) else [],
                    stat=int(fs_data.get("stat", 0) or 0),
                    info=int(fs_data.get("info", 0) or 0),
                )

        if "bed_temper" in data:
            temps["bed"] = float(data["bed_temper"])
        if "bed_target_temper" in data:
            temps["bed_target"] = float(data["bed_target_temper"])
        # Check if this is H2D (has device.extruder.info with 2 extruders)
        has_h2d_extruder_info = (
            "device" in data
            and isinstance(data.get("device"), dict)
            and "extruder" in data["device"]
            and isinstance(data["device"]["extruder"].get("info"), list)
            and len(data["device"]["extruder"]["info"]) >= 2
        )

        # Standard nozzle fields: these are for the RIGHT/default nozzle on H2D
        # For H2D, we use these for nozzle_2 (RIGHT), for others use as nozzle (primary)
        # NOTE: On H2D, nozzle_temper seems to mirror left nozzle - we override with extruder_info[0] later
        if "nozzle_temper" in data:
            if has_h2d_extruder_info:
                temps["nozzle_2"] = float(data["nozzle_temper"])  # Will be overridden by extruder_info[0]
            else:
                temps["nozzle"] = float(data["nozzle_temper"])
        if "nozzle_target_temper" in data:
            if has_h2d_extruder_info:
                temps["nozzle_2_target"] = float(data["nozzle_target_temper"])  # RIGHT target on H2D
            else:
                temps["nozzle_target"] = float(data["nozzle_target_temper"])
        # Second nozzle for dual-extruder printers - skip for H2D (uses device.extruder.info instead)
        if not has_h2d_extruder_info:
            # Try multiple possible field names used by different firmware versions
            if "nozzle_temper_2" in data:
                val = float(data["nozzle_temper_2"])
                if -50 < val < 500:  # Valid temp range
                    temps["nozzle_2"] = val
                else:
                    logger.debug("[%s] nozzle_temper_2=%s out of range", self.serial_number, val)
            elif "right_nozzle_temper" in data:
                val = float(data["right_nozzle_temper"])
                if -50 < val < 500:  # Valid temp range
                    temps["nozzle_2"] = val
                else:
                    logger.debug("[%s] right_nozzle_temper=%s out of range", self.serial_number, val)
            if "nozzle_target_temper_2" in data:
                val = float(data["nozzle_target_temper_2"])
                if 0 <= val < 500:  # Valid temp range
                    temps["nozzle_2_target"] = val
                else:
                    logger.debug("[%s] nozzle_target_temper_2=%s out of range", self.serial_number, val)
            elif "right_nozzle_target_temper" in data:
                val = float(data["right_nozzle_target_temper"])
                if 0 <= val < 500:  # Valid temp range
                    temps["nozzle_2_target"] = val
                else:
                    logger.debug("[%s] right_nozzle_target_temper=%s out of range", self.serial_number, val)
            # Also check for left nozzle as primary (some H2 models)
            if "left_nozzle_temper" in data and "nozzle" not in temps:
                temps["nozzle"] = float(data["left_nozzle_temper"])
            if "left_nozzle_target_temper" in data and "nozzle_target" not in temps:
                temps["nozzle_target"] = float(data["left_nozzle_target_temper"])
        if "chamber_temper" in data:
            chamber_val = float(data["chamber_temper"])
            logger.debug("[%s] chamber_temper raw value: %s", self.serial_number, chamber_val)
            # Check if we recently set the target locally (within 5 seconds)
            local_set_time = self.state.temperatures.get("_chamber_target_set_time", 0)
            respect_local = (time.time() - local_set_time) < 5.0
            # H2D protocol: chamber_temper encoding indicates heater state
            # - When > 500: encoded as (target * 65536 + current) - heater is ON
            # - When < 500: direct Celsius current temp only - heater is OFF
            if -50 < chamber_val < 100:
                # Direct value = the CURRENT temperature. It says nothing about
                # the target — BS reads that from the separate top-level ``ctt``
                # (``DevChamber::ParseChamberV1_0``), handled below.
                temps["chamber"] = chamber_val
                if not respect_local and "ctt" not in data:
                    # ⚠️ Asserting 0 here was an inference BS does not make: a
                    # machine soaking at 50 °C renders as target 0, and the
                    # history chart draws a flat zero under a rising curve. Kept
                    # only for firmware that sends neither ``ctt`` nor
                    # ``device.ctc`` — there it is the old behaviour, no worse.
                    temps["chamber_target"] = 0.0
                    logger.debug("[%s] chamber_temper direct value: %s°C, no ctt", self.serial_number, chamber_val)
            else:
                logger.debug("[%s] chamber_temper %s out of direct range", self.serial_number, chamber_val)
                # Try to decode if it looks like an encoded value
                if chamber_val > 500:
                    mqtt_target = int(chamber_val) // 65536
                    current = int(chamber_val) % 65536
                    logger.debug(
                        f"[{self.serial_number}] chamber_temper decoded: mqtt_target={mqtt_target}, current={current}, respect_local={respect_local}"
                    )
                    if -50 < current < 100:
                        temps["chamber"] = float(current)
                    # Store decoded target for later use, but DON'T set chamber_heating here!
                    # Heating state will be calculated later after parsing ctc.info.target (explicit target)
                    # which is the authoritative source the slicer uses.
                    if not respect_local:
                        if 0 <= mqtt_target <= 60:
                            # Store as "decoded" target - may be overridden by explicit target fields
                            temps["_chamber_decoded_target"] = float(mqtt_target)
        # Top-level ``ctt`` — the chamber TARGET on the V1 protocol. BS
        # ``DevChamber::ParseChamberV1_0`` reads exactly two fields:
        #
        #     chamber_temper -> current
        #     ctt            -> target
        #
        # ⚠️ We read the first and not the second, and inferred the target from
        # the shape of the first instead. That inference has no counterpart in
        # BS, and it is the reason a machine soaking at 50 °C could render with
        # a target of 0. It also matters for the preheat stage, which waits on
        # this number.
        if "ctt" in data and not respect_local:
            try:
                _ctt = float(data["ctt"])
            except (TypeError, ValueError):
                _ctt = None
            if _ctt is not None and 0 <= _ctt <= 100:
                temps["chamber_target"] = _ctt

        # Chamber target temperature (set by print file or display)
        if "mc_target_cham" in data:
            mc_target = float(data["mc_target_cham"])
            logger.debug("[%s] mc_target_cham raw value: %s", self.serial_number, mc_target)
            # Filter out encoded/invalid values - valid chamber target is 0-60°C
            if 0 <= mc_target <= 60:
                temps["chamber_target"] = mc_target
        # H2D series: Chamber temp is in info.temp (may be encoded or direct °C)
        # NOTE: Don't set chamber_heating here - let ctc.info.target or fallback logic handle it
        # The encoded target in info.temp may be stale (slicer uses ctc.info.target as source of truth)
        try:
            if "info" in data and isinstance(data["info"], dict):
                info_temp = data["info"].get("temp")
                if info_temp is not None and "chamber" not in temps:
                    # Check for encoded value (target * 65536 + current)
                    if info_temp > 500:
                        # Decode: extract current temperature and target
                        target = info_temp // 65536
                        current = info_temp % 65536
                        temps["chamber"] = float(current)
                        # Store decoded target as fallback (may be overridden by ctc.info.target)
                        if "_chamber_decoded_target" not in temps:
                            temps["_chamber_decoded_target"] = float(target)
                        logger.debug(
                            f"[{self.serial_number}] info.temp encoded: {info_temp} -> current={current}, decoded_target={target}"
                        )
                    elif -50 < info_temp < 100:
                        # Valid direct temperature - heater is OFF
                        temps["chamber"] = float(info_temp)
                        temps["chamber_target"] = 0.0  # Direct value means heater off
                        logger.debug("[%s] info.temp direct: %s°C (heater OFF)", self.serial_number, info_temp)
            # H2D series: Dual extruder temps are in device.extruder.info array
            # Temperature values are encoded as fixed-point (value / 65536 = °C)
            if "device" in data and isinstance(data["device"], dict):
                device = data["device"]
                # Parse dual extruder temperatures
                extruder_data = device.get("extruder", {})
                extruder_info = extruder_data.get("info", [])
                if isinstance(extruder_info, list) and len(extruder_info) >= 1:
                    # H2D nozzle mapping: id=0 is RIGHT nozzle (default), id=1 is LEFT nozzle
                    # Only parse dual nozzle temps if this is actually a dual nozzle printer (H2D)
                    # has_h2d_extruder_info requires len(extruder_info) >= 2
                    if has_h2d_extruder_info:
                        # Right nozzle (extruder 0) - use extruder_info for actual temp, not nozzle_temper
                        # nozzle_temper field seems to mirror left nozzle on H2D, so use extruder_info[0]
                        if "temp" in extruder_info[0]:
                            temp_val = extruder_info[0]["temp"]
                            if temp_val > 500:
                                # Encoded format: temp = target * 65536 + current
                                target = temp_val // 65536
                                current = temp_val % 65536
                                if -50 < current < 500:
                                    temps["nozzle_2"] = float(current)
                                if 0 < target < 500:
                                    temps["nozzle_2_target"] = float(target)
                                temps["nozzle_2_heating"] = target > 0 and current < target
                            elif -50 < temp_val < 500:
                                # Direct Celsius value = heater is OFF
                                temps["nozzle_2"] = float(temp_val)
                                temps["nozzle_2_target"] = 0.0
                                temps["nozzle_2_heating"] = False
                    # Left nozzle (extruder 1) - only for dual nozzle printers
                    # H2D protocol: temp field encoding depends on value
                    # - When > 500: encoded as (target * 65536 + current) - heater is ON
                    # - When < 500: direct Celsius current temp only - heater is OFF
                    if len(extruder_info) >= 2 and "temp" in extruder_info[1]:
                        ext1 = extruder_info[1]
                        temp_val = ext1["temp"]

                        # Check if we recently set the target locally (within 5 seconds)
                        # If so, don't let MQTT data overwrite it
                        local_set_time = self.state.temperatures.get("_nozzle_target_set_time", 0)
                        respect_local_target = (time.time() - local_set_time) < 5.0

                        if temp_val > 500:
                            # Encoded format: temp = target * 65536 + current
                            target = temp_val // 65536
                            current = temp_val % 65536
                            if 0 < target < 500 and not respect_local_target:
                                temps["nozzle_target"] = float(target)
                            if -50 < current < 500:
                                temps["nozzle"] = float(current)
                            # Heating = encoded AND we're using the MQTT target (not local override)
                            # If local target is being respected, use local target to determine heating
                            if respect_local_target:
                                local_target = self.state.temperatures.get("nozzle_target", 0)
                                temps["nozzle_heating"] = local_target > 0 and current < local_target
                            else:
                                temps["nozzle_heating"] = target > 0 and current < target
                        elif -50 < temp_val < 500:
                            # Direct Celsius = heater is OFF (or at target with heater off)
                            temps["nozzle"] = float(temp_val)
                            if not respect_local_target:
                                temps["nozzle_target"] = 0.0
                            temps["nozzle_heating"] = False  # Direct = not heating
                    # Parse H2D snow field (slot now) for accurate tray_now disambiguation
                    # snow encodes AMS ID in high byte: ams_id = snow >> 8, slot = snow & 0xFF
                    if has_h2d_extruder_info:
                        for ext_info in extruder_info:
                            ext_id = ext_info.get("id")
                            snow = ext_info.get("snow")
                            if ext_id is not None and snow is not None and ext_id <= 1:
                                # Normalize H2D snow value to global tray ID
                                ams_id = snow >> 8
                                slot = snow & 0xFF
                                if 0 <= ams_id <= 3:
                                    # Regular AMS slot
                                    global_tray = ams_id * 4 + (slot & 0x03)
                                    old_val = self.state.h2d_extruder_snow.get(ext_id)
                                    if old_val != global_tray:
                                        logger.debug(
                                            f"[{self.serial_number}] H2D extruder[{ext_id}] snow: "
                                            f"raw={snow} (AMS {ams_id} slot {slot}) -> global tray {global_tray}"
                                        )
                                    self.state.h2d_extruder_snow[ext_id] = global_tray
                                elif ams_id == 254 or ams_id == 255:
                                    # External spool or unloaded
                                    normalized = 254 if slot != 255 else 255
                                    old_val = self.state.h2d_extruder_snow.get(ext_id)
                                    if old_val != normalized:
                                        logger.debug(
                                            f"[{self.serial_number}] H2D extruder[{ext_id}] snow: "
                                            f"raw={snow} -> {'external' if normalized == 254 else 'unloaded'}"
                                        )
                                    self.state.h2d_extruder_snow[ext_id] = normalized
                                elif 128 <= ams_id <= 135:
                                    # External spool with hub mapping
                                    old_val = self.state.h2d_extruder_snow.get(ext_id)
                                    if old_val != ams_id:
                                        logger.debug(
                                            f"[{self.serial_number}] H2D extruder[{ext_id}] snow: "
                                            f"raw={snow} -> external hub {ams_id}"
                                        )
                                    self.state.h2d_extruder_snow[ext_id] = ams_id
                # Parse bed heating state from device.bed.info.temp encoding
                # temp > 500 means encoded (target*65536+current), heating = target > 0 AND current < target
                bed_data = device.get("bed", {})
                bed_info = bed_data.get("info", {})
                if "temp" in bed_info:
                    temp_val = bed_info["temp"]
                    if temp_val > 500:
                        target = temp_val // 65536
                        current = temp_val % 65536
                        temps["bed_heating"] = target > 0 and current < target
                    else:
                        temps["bed_heating"] = False
                # Parse chamber temp from device.ctc.info.temp if not already set
                ctc_data = device.get("ctc", {})
                ctc_info = ctc_data.get("info", {})
                # Parse airduct mode (0=cooling, 1=heating) + parts + modes
                airduct_data = device.get("airduct", {})
                if "modeCur" in airduct_data:
                    new_mode = airduct_data["modeCur"]
                    # A push sent before our command reached the printer still
                    # describes the old mode; inside the hold it is stale news.
                    _hold = self.state.printer_settings_hold.get("airduct_mode")
                    if _hold is not None and (time.time() - _hold) < 3.0:
                        pass
                    else:
                        if new_mode != self.state.airduct_mode:
                            logger.debug(
                                f"[{self.serial_number}] airduct_mode changed: {self.state.airduct_mode} -> {new_mode}"
                            )
                        self.state.airduct_mode = new_mode
                self._parse_airduct_parts(airduct_data)
                # Parse chamber temp - may be encoded as (target*65536+current) when > 500
                # Check if we recently set the target locally (within 5 seconds)
                local_set_time = self.state.temperatures.get("_chamber_target_set_time", 0)
                respect_local_target = (time.time() - local_set_time) < 5.0

                # Log ctc_info contents for debugging
                if ctc_info:
                    logger.debug("[%s] ctc_info keys: %s", self.serial_number, list(ctc_info.keys()))

                # FIRST: Parse explicit ctc.info.target if available - this is the authoritative target
                # (what the slicer shows). This OVERRIDES any previously decoded target.
                explicit_target = None
                if "target" in ctc_info:
                    target_val = ctc_info["target"]
                    logger.debug(
                        f"[{self.serial_number}] ctc_info.target explicit value: {target_val}, respect_local={respect_local_target}"
                    )
                    # Filter out invalid values (valid chamber target is 0-60°C)
                    if 0 <= target_val <= 60 and not respect_local_target:
                        explicit_target = float(target_val)
                        temps["chamber_target"] = explicit_target  # Override any previous value
                        logger.debug(
                            f"[{self.serial_number}] Setting chamber_target from ctc_info.target: {explicit_target}"
                        )

                # Parse chamber temp from ctc.info.temp - may be encoded
                if "temp" in ctc_info and "chamber" not in temps:
                    temp_val = ctc_info["temp"]
                    logger.debug("[%s] ctc_info.temp raw value: %s", self.serial_number, temp_val)
                    if temp_val > 500:
                        # Encoded value: decode target and current
                        decoded_target = temp_val // 65536
                        current = temp_val % 65536
                        temps["chamber"] = float(current)
                        logger.debug(
                            f"[{self.serial_number}] ctc_info.temp decoded: target={decoded_target}, current={current}, explicit_target={explicit_target}"
                        )

                        # Determine which target to use for heating state:
                        # Priority: local target > explicit target > decoded target
                        if respect_local_target:
                            local_target = self.state.temperatures.get("chamber_target", 0)
                            temps["chamber_heating"] = local_target > 0 and current < local_target
                        elif explicit_target is not None:
                            # Use explicit ctc.info.target - this is what slicer sees
                            temps["chamber_heating"] = explicit_target > 0 and current < explicit_target
                        else:
                            # Fallback to decoded target only if no explicit target available
                            if not respect_local_target and "chamber_target" not in temps:
                                temps["chamber_target"] = float(decoded_target)
                            temps["chamber_heating"] = decoded_target > 0 and current < decoded_target
                    else:
                        # Direct value (not encoded) - heater is OFF
                        temps["chamber"] = float(temp_val)
                        temps["chamber_heating"] = False
        except Exception as e:
            logger.warning("[%s] Error parsing H2D temperatures: %s", self.serial_number, e)
        if temps:
            # Handle chamber_target: prefer explicit over decoded
            if "_chamber_decoded_target" in temps and "chamber_target" not in temps:
                # No explicit target available, use decoded target from chamber_temper
                temps["chamber_target"] = temps["_chamber_decoded_target"]
            # Remove internal temp key before merging
            temps.pop("_chamber_decoded_target", None)

            # Merge new temps into existing, preserving valid values when new ones are filtered out
            for key, value in temps.items():
                self.state.temperatures[key] = value

            # Calculate chamber_heating after all targets are known
            # Priority: local target (if recent) > explicit target (chamber_target) > 0
            if "chamber" in temps and "chamber_heating" not in temps:
                current = self.state.temperatures.get("chamber", 0)
                local_set_time = self.state.temperatures.get("_chamber_target_set_time", 0)
                respect_local = (time.time() - local_set_time) < 5.0

                if respect_local:
                    # Use locally-set target
                    target = self.state.temperatures.get("chamber_target", 0)
                else:
                    # Use explicit/decoded target from MQTT
                    target = self.state.temperatures.get("chamber_target", 0)

                self.state.temperatures["chamber_heating"] = target > 0 and current < target
                logger.debug(
                    f"[{self.serial_number}] Chamber heating calculated: target={target}, current={current}, heating={self.state.temperatures['chamber_heating']}, respect_local={respect_local}"
                )

            # Debug: log chamber value if it was updated
            if "chamber" in temps:
                logger.debug(
                    f"[{self.serial_number}] Chamber temp updated to: {self.state.temperatures.get('chamber')}, target: {self.state.temperatures.get('chamber_target')}, heating: {self.state.temperatures.get('chamber_heating')}"
                )

            # Calculate nozzle_heating for single nozzle printers (not set by H2D parsing)
            # For H2D, nozzle_heating is set in temps dict; for single nozzle, calculate here
            if "nozzle" in temps and "nozzle_heating" not in temps:
                current = self.state.temperatures.get("nozzle", 0)
                target = self.state.temperatures.get("nozzle_target", 0)
                self.state.temperatures["nozzle_heating"] = target > 0 and current < target

            # Calculate bed_heating for non-H2 printers (H2 sets it from device.bed.info.temp above;
            # standard printers only report bed_temper/bed_target_temper, so mirror the nozzle fallback).
            if "bed" in temps and "bed_heating" not in temps:
                current = self.state.temperatures.get("bed", 0)
                target = self.state.temperatures.get("bed_target", 0)
                self.state.temperatures["bed_heating"] = target > 0 and current < target

        # Parse HMS (Health Management System) errors
        if "hms" in data:
            hms_list = data["hms"]
            logger.debug("[%s] HMS data received: %s", self.serial_number, hms_list)
            self.state.hms_errors = []
            # Reconciled against the whole list once it is rebuilt, below: a
            # verdict on "is the printer refusing our commands" has to be drawn
            # from the absence of the code as much as from its presence.
            verify_failed = False
            if isinstance(hms_list, list):
                for hms in hms_list:
                    if isinstance(hms, dict):
                        # HMS format: {"attr": attribute_code, "code": error_code}
                        # attr contains module/severity info, code contains error number
                        # Both are needed to construct the wiki URL
                        attr = hms.get("attr", 0)
                        code = hms.get("code", 0)
                        if isinstance(attr, str):
                            attr = int(attr.replace("0x", ""), 16) if attr else 0
                        if isinstance(code, str):
                            code = int(code.replace("0x", ""), 16) if code else 0
                        # Severity comes from ``code``, not from ``attr``.
                        # BS ``DevHMSItem::parse`` (DeviceCore/DevHMS.cpp):
                        #
                        #     m_module_num  = (attr >> 16) & 0xFF
                        #     m_part_id     = (attr >> 8)  & 0xFF
                        #     msg_level_int = code >> 16
                        #
                        # This read ``(attr >> 8) & 0xF`` — BS's **part id**, a
                        # different field entirely — so every fault was ranked
                        # by which component reported it. On a wall of printers
                        # the severity pip IS the triage signal, and it was
                        # pointing at noise.
                        severity = _hms_severity_from_code(code)
                        # Module is in attr byte 3 (bits 24-31)
                        module = (attr >> 24) & 0xFF
                        # Skip non-error status codes - all real HMS errors
                        # have code >= 0x4000. Lower values are status/phase
                        # indicators that some firmware sends during normal printing.
                        if code < 0x4000:
                            continue
                        # Skip user-action echoes — the printer firmware emits these
                        # as part of normal user-cancel sequences. They're not faults
                        # and shouldn't count toward "X problem" badges or surface as
                        # red pips on the printer card. Backend's notification path
                        # already suppresses 0500_400E for the same reason.
                        short_code = f"{(attr >> 16) & 0xFFFF:04X}_{code & 0xFFFF:04X}"
                        if short_code in _HMS_USER_ACTION_CODES:
                            continue
                        # Catalog has both 8-char keys (base class) and 16-char keys
                        # (specific variants). The full 16-char identifier preserves the
                        # 32 bits of attr_low + code_high that short_code discards — that's
                        # the firmware's matching key, so try it first and fall back.
                        full_code = f"{attr:08X}{code:08X}"
                        if full_code == HMS_MQTT_VERIFY_FAILED:
                            verify_failed = True
                        actions = get_actions_for_error_code(self.serial_number[:3], full_code)
                        if not actions:
                            actions = get_actions_for_error_code(self.serial_number[:3], short_code.replace("_", ""))
                        self.state.hms_errors.append(
                            HMSError(
                                code=f"0x{code:x}" if code else "0x0",
                                attr=attr,
                                module=module,
                                severity=severity,
                                actions=actions,
                                job_id=self.state.subtask_id,
                                full_code=full_code,
                            )
                        )
            self._apply_mqtt_verify_state(verify_failed)

        # Parse print_error - this is a different error format than HMS
        # print_error is a 32-bit integer where:
        #   - High 16 bits contain module info (e.g., 0x0500)
        #   - Low 16 bits contain error code (e.g., 0x8061)
        # Format on printer screen: [0500-8061] -> short code: 0500_8061
        if "print_error" in data:
            print_error = data["print_error"]
            if print_error and print_error != 0:
                # Extract components: MMMMEEEE -> MMMM_EEEE
                module = (print_error >> 16) & 0xFFFF  # High 16 bits (e.g., 0x0500)
                error = print_error & 0xFFFF  # Low 16 bits (e.g., 0x8061)

                # Values below 0x4000 are status/phase indicators, not real errors.
                # All known HMS errors use 0x4xxx (fatal), 0x8xxx (warning), 0xCxxx (prompt).
                # Some firmware sends low values like 0x0002 during normal printing.
                if error < 0x4000:
                    pass  # Skip - not a real error
                else:
                    # Store in a format that matches the community error database
                    # attr stores the full 32-bit value for reconstruction
                    # code stores the short format string for lookup
                    short_code = f"{module:04X}_{error:04X}"

                    logger.debug(
                        f"[{self.serial_number}] print_error: {print_error} (0x{print_error:08x}) -> short_code={short_code}"
                    )

                    # Same user-action filter as the hms[] branch above —
                    # print_error carries the same cancel echoes (e.g.
                    # 0500_400E) and they must not surface as faults on the
                    # printer card.
                    if short_code in _HMS_USER_ACTION_CODES:
                        pass  # cancel echo — silently drop
                    elif short_code == _DEVICE_BUSY_CODE and self.state.state in _ACTIVE_PRINT_STATES:
                        self._clear_device_busy(print_error)
                    else:
                        # Only add if not already in HMS errors (avoid duplicates)
                        existing_short_codes = {e.short_code for e in self.state.hms_errors}

                        if short_code not in existing_short_codes:
                            # Bambu's HMS catalog keys by 3-letter device code (SN prefix)
                            # and a 16-char short error code without the underscore.
                            actions = get_actions_for_error_code(self.serial_number[:3], short_code.replace("_", ""))
                            # print_error is already 32-bit — f"{print_error:08X}" is the
                            # firmware's matching key with no truncation. Snapshot the live
                            # subtask_id so later job changes don't invalidate the action.
                            self.state.hms_errors.append(
                                HMSError(
                                    code=f"0x{error:x}",
                                    attr=print_error,  # Store full value for display
                                    module=module >> 8,  # High byte of module (e.g., 0x05)
                                    severity=_print_error_severity(error),
                                    actions=actions,
                                    job_id=self.state.subtask_id,
                                    full_code=f"{print_error:08X}",
                                )
                            )

        # AMS Settings — newer firmware embeds the four toggles into the print
        # ``cfg`` field as a hex-string bitfield (BS DeviceManager.cpp:4204).
        # bit 0 = DetectOnInsert, bit 1 = DetectOnPowerup,
        # bit 17 = DetectRemain, bit 18 = AutoRefill.
        # Older builds report the first three under print.ams.{insert,power_on,
        # calibrate_remain}_flag — that path is handled in _handle_ams_data().
        # Respect the 3 s hold-timer in both cases.
        cfg_raw = data.get("cfg")
        if isinstance(cfg_raw, str) and cfg_raw:
            try:
                _cfg_int = int(cfg_raw, 16)
            except ValueError:
                _cfg_int = None
            if _cfg_int is not None:
                _cfg_now = time.time()
                _hold_ttl = 3.0

                def _ams_cfg_hold_active(flag_name: str) -> bool:
                    ts = self.state.ams_settings_hold.get(flag_name)
                    return ts is not None and (_cfg_now - ts) < _hold_ttl

                if not _ams_cfg_hold_active("ams_insertion_update"):
                    self.state.ams_insertion_update = bool((_cfg_int >> 0) & 0x1)
                if not _ams_cfg_hold_active("ams_power_on_update"):
                    self.state.ams_power_on_update = bool((_cfg_int >> 1) & 0x1)
                if not _ams_cfg_hold_active("ams_remain_capacity"):
                    self.state.ams_remain_capacity = bool((_cfg_int >> 17) & 0x1)
                if not _ams_cfg_hold_active("ams_auto_switch_filament"):
                    self.state.ams_auto_switch_filament = bool((_cfg_int >> 18) & 0x1)

        # AMS firmware switch — BS ``DevAmsSystemFirmwareSwitch::ParseFirmwareSwitch``
        # (DevFilaAmsSetting.cpp). Lives under ``upgrade_state``, not under ``ams``.
        #
        # The list is the source of BOTH the ids and the labels. BS builds its
        # combo box from ``m_name`` per entry and never carries a name of its
        # own, which is the only safe shape: the two A1 personalities are
        # IDX_LITE = 0 and IDX_AMS_AMS2_AMSHT = 1, and a label paired with the
        # wrong id reflashes the AMS into the other one.
        _upgrade_state = data.get("upgrade_state")
        if isinstance(_upgrade_state, dict) and "status" in _upgrade_state:
            self.state.firmware_upgrade_status = str(_upgrade_state["status"] or "") or None
        if isinstance(_upgrade_state, dict):
            # Two states in which the printer will not print until its firmware
            # is dealt with. BS ``DevUpgrade::ParseV1_0`` reads both from this
            # same block, and they arrive over LAN like everything else here —
            # ⚠️ despite belonging to a flow that otherwise needs the cloud.
            #
            # ``consistency_request`` is the one that matters on an offline
            # farm: BS's wording is "The firmware version is abnormal. Repairing
            # and updating are required before printing." That is a module
            # version MISMATCH, which is exactly what an SD-card update can
            # leave behind when one module takes the new firmware and another
            # does not — the path our own bulk-firmware feature uses.
            #
            # Without these two the printer simply stops accepting work and the
            # card shows nothing at all. Reading them does not require being
            # able to answer them; see the registry (N3) for that half.
            if isinstance(_upgrade_state.get("consistency_request"), bool):
                self.state.firmware_consistency_request = _upgrade_state["consistency_request"]
            if isinstance(_upgrade_state.get("force_upgrade"), bool):
                self.state.firmware_force_upgrade = _upgrade_state["force_upgrade"]

        # ``device.extruder.info[].info`` bit 1 = filament present in that
        # extruder. Parsed here rather than beside the nozzle temperatures
        # because the only reader is the AMS-firmware-switch refusal, and BS
        # reads the same bit for the same reason.
        _ext = (data.get("device") or {}).get("extruder") if isinstance(data.get("device"), dict) else None
        if isinstance(_ext, dict) and isinstance(_ext.get("info"), list):
            for _idx, _entry in enumerate(_ext["info"]):
                if not isinstance(_entry, dict) or "info" not in _entry:
                    continue
                try:
                    _info_int = int(_entry["info"])
                except (TypeError, ValueError):
                    continue
                _ext_id = _entry.get("id")
                try:
                    _ext_id = int(_ext_id)
                except (TypeError, ValueError):
                    _ext_id = _idx
                self.state.ext_has_filament[_ext_id] = bool((_info_int >> 1) & 0x1)
                # Bit 3 of the same word — BS ``m_has_nozzle``, which gates the
                # nozzle temperature control. ⚠️ Absence is NOT "no hotend": BS
                # defaults the field to true with the reason in a comment ("A/P
                # series does not support nozzle detection"), so only a machine
                # that reports the word at all may ever answer False here.
                self.state.ext_has_nozzle[_ext_id] = bool((_info_int >> 3) & 0x1)

        _ams_fw = _upgrade_state.get("mc_for_ams_firmware") if isinstance(_upgrade_state, dict) else None
        if isinstance(_ams_fw, dict):
            # One hold covers the whole block: a switch we just asked for must
            # not be undone by the report that was already in flight. Same 3 s
            # TTL as every other AMS setting.
            _fw_ts = self.state.ams_settings_hold.get("ams_firmware_switch")
            if _fw_ts is None or (time.time() - _fw_ts) >= 3.0:
                firmwares = _ams_fw.get("firmware")
                if isinstance(firmwares, list):
                    parsed: list[dict] = []
                    for item in firmwares:
                        if not isinstance(item, dict) or "id" not in item:
                            continue
                        try:
                            fw_id = int(item["id"])
                        except (TypeError, ValueError):
                            continue
                        parsed.append(
                            {
                                "id": fw_id,
                                "name": str(item.get("name") or ""),
                                "version": str(item.get("version") or ""),
                            }
                        )
                    # BS keys a std::map, so entries arrive ordered by id and a
                    # duplicate id keeps the last one. Mirror both.
                    self.state.ams_firmwares = sorted({fw["id"]: fw for fw in parsed}.values(), key=lambda fw: fw["id"])

                # An id the list does not contain resets the field rather than
                # being kept — BS does exactly this, and a stale id would point
                # the picker at an entry that no longer exists.
                _known = {fw["id"] for fw in self.state.ams_firmwares}
                if "current_firmware_id" in _ams_fw:
                    try:
                        _sel = int(_ams_fw["current_firmware_id"])
                    except (TypeError, ValueError):
                        _sel = None
                    self.state.ams_firmware_idx_sel = _sel if _sel in _known else None
                if "current_run_firmware_id" in _ams_fw:
                    try:
                        _run = int(_ams_fw["current_run_firmware_id"])
                    except (TypeError, ValueError):
                        _run = None
                    # BS's IDX_DC (-1) is "not reported"; None says the same
                    # thing without a magic number leaking to the frontend.
                    self.state.ams_firmware_idx_run = _run if _run in _known else None
                if "status" in _ams_fw:
                    self.state.ams_firmware_status = str(_ams_fw["status"] or "") or None

        # Parse home_flag first so SD-card / door detection below can use it.
        # Bit 8 = HAS_SDCARD_NORMAL, bit 9 = HAS_SDCARD_ABNORMAL, bit 11 = store-to-SD,
        # bit 18 = wired network, bit 23 = door-open (X1 family only).
        home_flag = None
        if "home_flag" in data:
            home_flag = data["home_flag"]
            # Convert to unsigned 32-bit if negative
            if home_flag < 0:
                home_flag = home_flag & 0xFFFFFFFF

        # AMS Settings — X1 / P1 family ship the "remaining capacity estimate"
        # and "filament backup" toggles in ``home_flag`` (BS DeviceManager.cpp
        # parse_home_flag — bit 7 = DetectRemain, bit 10 = AutoRefill). Newer
        # firmware uses the ``cfg`` hex string path instead (handled above);
        # both paths are idempotent and respect the same 3 s hold-timer.
        if home_flag is not None:
            _hf_now = time.time()
            _hf_ttl = 3.0

            def _hf_hold_active(flag_name: str) -> bool:
                ts = self.state.ams_settings_hold.get(flag_name)
                return ts is not None and (_hf_now - ts) < _hf_ttl

            if not _hf_hold_active("ams_remain_capacity"):
                self.state.ams_remain_capacity = bool((home_flag >> 7) & 0x1)
            if not _hf_hold_active("ams_auto_switch_filament"):
                self.state.ams_auto_switch_filament = bool((home_flag >> 10) & 0x1)

        # SD card presence.
        # Use the top-level `sdcard` field with a permissive truthy check covering
        # the bool / int / "HAS_SDCARD_NORMAL" variants that different firmware
        # revisions emit. We do NOT derive it from home_flag — heartbeat pushes
        # clear bits 8-9 even when a card is inserted, which made the UI badge
        # flap. The only remaining consumer is the firmware-update precondition
        # check in firmware_update.py; other callers were removed upstream.
        if "sdcard" in data:
            raw_sdcard = data["sdcard"]
            if isinstance(raw_sdcard, str):
                # ⚠️ Was ``"HAS_SDCARD" in value`` — a SUBSTRING test, so
                # ``HAS_SDCARD_ABNORMAL`` and ``HAS_SDCARD_READONLY`` both
                # matched and a card the printer is complaining about read as
                # healthy. Match the state, not a prefix of its name.
                _sd = raw_sdcard.strip().upper()
                self.state.sdcard_state = {
                    "HAS_SDCARD_NORMAL": SDCARD_NORMAL,
                    "HAS_SDCARD_ABNORMAL": SDCARD_ABNORMAL,
                    "HAS_SDCARD_READONLY": SDCARD_READONLY,
                    "NORMAL": SDCARD_NORMAL,
                    "TRUE": SDCARD_NORMAL,
                    "1": SDCARD_NORMAL,
                }.get(_sd, SDCARD_NONE)
            else:
                # BS ``DevStorage::ParseV1_0``: the legacy bool is NORMAL or nothing.
                self.state.sdcard_state = SDCARD_NORMAL if raw_sdcard else SDCARD_NONE
            self.state.sdcard = self.state.sdcard_state == SDCARD_NORMAL

        # New protocol: ``aux`` bits 12-13 carry the same four states
        # (BS ``m_storage->set_sdcard_state(get_flag_bits(aux, 12, 2))``).
        # ⚠️ ``aux`` is the one member of the cfg/fun/aux/stat quartet nothing
        # here read — the latch detected it and then parsed the other three.
        _aux_bits = parse_hex_bitfield(data.get("aux"))
        if _aux_bits is not None:
            self.state.sdcard_state = (_aux_bits >> 12) & 0x3
            self.state.sdcard = self.state.sdcard_state == SDCARD_NORMAL
            # BS ``m_has_timelapse_kit = get_flag_bits(aux, 26, 1)``. An add-on
            # that gives a machine somewhere to write a timelapse when its card
            # slot cannot — which is why it can excuse a missing SD card.
            self.state.has_timelapse_kit = bool((_aux_bits >> 26) & 0x1)

        # Store-sent-files-to-SD toggle (home_flag bit 11).
        if home_flag is not None:
            store_to_sdcard = bool((home_flag >> 11) & 1)
            if store_to_sdcard != self.state.store_to_sdcard:
                logger.debug(
                    f"[{self.serial_number}] store_to_sdcard changed: {self.state.store_to_sdcard} -> {store_to_sdcard}"
                )
            self.state.store_to_sdcard = store_to_sdcard

        # Door open detection. The door-open bit is bit 23, but WHICH field
        # carries it is model-dependent (``door_sensor_field``): the X1 family
        # (X1/X1C/X1E) uses ``home_flag`` bit 23; X2D and P2S use ``stat`` bit 23
        # (X2D verified on hardware — its home_flag bit 23 never flips, its ``stat``
        # bit 23 does; P2S inferred from the shared X2D/P2S door-sensor part — see
        # DOOR_SENSOR_STAT_MODELS in printer_models.py). Other enclosed models expose
        # no trustworthy bit, so the field is None and we skip them (avoids the
        # misleading permanent "Door Closed" / flapping badges previously seen on
        # P1S/H2*).
        from backend.app.utils.printer_models import door_sensor_field

        _door_field = door_sensor_field(self.model)
        if _door_field:
            _door_raw = None
            if _door_field == "home_flag":
                _door_raw = home_flag
            elif _door_field == "stat":
                _stat = data.get("stat")
                if _stat is not None:
                    try:
                        _door_raw = int(str(_stat), 16)
                    except (ValueError, TypeError):
                        _door_raw = None
            if _door_raw is not None:
                door_open = (_door_raw & 0x00800000) != 0
                if door_open != self.state.door_open:
                    logger.debug(
                        "[%s] door_open changed: %s -> %s (%s=0x%08X)",
                        self.serial_number,
                        self.state.door_open,
                        door_open,
                        _door_field,
                        _door_raw,
                    )
                self.state.door_open = door_open

        # Parse timelapse status (recording active during print)
        if "timelapse" in data:
            logger.debug("[%s] timelapse field: %s", self.serial_number, data["timelapse"])
            self.state.timelapse = data["timelapse"] is True
            # Track if timelapse was ever active during this print
            if self.state.timelapse and self._was_running:
                self._timelapse_during_print = True

        # Parse ipcam/live view status
        if "ipcam" in data:
            ipcam_data = data["ipcam"]
            logger.debug("[%s] ipcam field: %s", self.serial_number, ipcam_data)
            if isinstance(ipcam_data, dict):
                # Check ipcam_record field for live view status
                self.state.ipcam = ipcam_data.get("ipcam_record") == "enable"
                # Check timelapse field (H2D sends it here, not in xcam)
                if "timelapse" in ipcam_data:
                    timelapse_enabled = ipcam_data.get("timelapse") == "enable"
                    if timelapse_enabled != self.state.timelapse:
                        logger.debug(
                            f"[{self.serial_number}] timelapse changed (from ipcam): {self.state.timelapse} -> {timelapse_enabled}"
                        )
                    self.state.timelapse = timelapse_enabled
                    # Track if timelapse was ever active during this print
                    if self.state.timelapse and self._was_running:
                        self._timelapse_during_print = True
                        logger.debug("[%s] Timelapse detected during print (from ipcam)", self.serial_number)
            else:
                self.state.ipcam = ipcam_data is True

        # Parse WiFi signal strength (dBm)
        if "wifi_signal" in data:
            wifi_signal = data["wifi_signal"]
            logger.debug("[%s] wifi_signal received: %s", self.serial_number, wifi_signal)
            if isinstance(wifi_signal, (int, float)):
                self.state.wifi_signal = int(wifi_signal)
            elif isinstance(wifi_signal, str):
                # Handle string format like "-52dBm"
                try:
                    self.state.wifi_signal = int(wifi_signal.replace("dBm", "").strip())
                except ValueError:
                    pass  # Ignore unparseable wifi_signal strings; field is non-critical

            # Detect ethernet connection: printers on ethernet with WiFi disabled
            # report a hardcoded wifi_signal of -90 dBm. Real WiFi signals vary
            # (typically -30 to -80 dBm). Only check models with an ethernet port.
            from backend.app.utils.printer_models import has_ethernet

            if has_ethernet(self.model):
                self.state.wired_network = self.state.wifi_signal == -90

        # Parse print speed level (1=silent, 2=standard, 3=sport, 4=ludicrous)
        if "spd_lvl" in data:
            new_speed = data["spd_lvl"]
            if new_speed != self.state.speed_level:
                logger.debug(
                    "[%s] speed_level changed: %s -> %s", self.serial_number, self.state.speed_level, new_speed
                )
            self.state.speed_level = new_speed

        # Parse skipped objects from printer status (s_obj field)
        # This allows us to restore skipped objects state after reconnection
        if "s_obj" in data:
            s_obj = data["s_obj"]
            if isinstance(s_obj, list):
                # Update skipped objects from printer's list
                new_skipped = [int(oid) for oid in s_obj if isinstance(oid, (int, str))]
                if new_skipped != self.state.skipped_objects:
                    logger.debug("[%s] skipped_objects updated from printer: %s", self.serial_number, new_skipped)
                    self.state.skipped_objects = new_skipped
                    self._notify_skipped_objects_changed()

        # Parse chamber light status from lights_report
        if "lights_report" in data:
            lights = data["lights_report"]
            logger.debug("[%s] lights_report: %s", self.serial_number, lights)
            if isinstance(lights, list):
                for light in lights:
                    if isinstance(light, dict) and light.get("node") == "chamber_light":
                        new_light_state = light.get("mode") == "on"
                        if new_light_state != self.state.chamber_light:
                            logger.debug(
                                f"[{self.serial_number}] chamber_light changed: {self.state.chamber_light} -> {new_light_state}"
                            )
                        self.state.chamber_light = new_light_state
                        break

        # Parse nozzle hardware info (single nozzle printers)
        if "nozzle_type" in data:
            material, flow = _parse_nozzle_type(str(data["nozzle_type"]))
            if material:
                self.state.nozzles[0].nozzle_type = material
            if flow:
                self.state.nozzles[0].nozzle_flow = flow
        if "nozzle_diameter" in data:
            self.state.nozzles[0].nozzle_diameter = str(data["nozzle_diameter"])

        # Parse nozzle hardware info (dual nozzle printers - H2D series)
        # Left nozzle
        if "left_nozzle_type" in data:
            material, flow = _parse_nozzle_type(str(data["left_nozzle_type"]))
            if material:
                self.state.nozzles[0].nozzle_type = material
            if flow:
                self.state.nozzles[0].nozzle_flow = flow
        if "left_nozzle_diameter" in data:
            self.state.nozzles[0].nozzle_diameter = str(data["left_nozzle_diameter"])
        # Right nozzle
        if "right_nozzle_type" in data:
            material, flow = _parse_nozzle_type(str(data["right_nozzle_type"]))
            if material:
                self.state.nozzles[1].nozzle_type = material
            if flow:
                self.state.nozzles[1].nozzle_flow = flow
        if "right_nozzle_diameter" in data:
            self.state.nozzles[1].nozzle_diameter = str(data["right_nozzle_diameter"])

        # Alternative format for dual nozzle (nozzle_type_2, etc.)
        if "nozzle_type_2" in data:
            material, flow = _parse_nozzle_type(str(data["nozzle_type_2"]))
            if material:
                self.state.nozzles[1].nozzle_type = material
            if flow:
                self.state.nozzles[1].nozzle_flow = flow
        if "nozzle_diameter_2" in data:
            self.state.nozzles[1].nozzle_diameter = str(data["nozzle_diameter_2"])

        # H2D/H2C series: Nozzle hardware info is in device.nozzle.info array
        if "device" in data and isinstance(data["device"], dict):
            device = data["device"]
            nozzle_data = device.get("nozzle", {})
            nozzle_info = nozzle_data.get("info", [])
            if isinstance(nozzle_info, list):
                # H2 series: nozzle_info contains extended nozzle data (wear, serial,
                # max_temp, etc.) for all nozzles: L/R hotend (IDs 0,1) and rack slots
                # (IDs 16-21 on H2C). Store ALL entries so the frontend can use them
                # for hover cards on both the L/R indicator and the nozzle rack card.
                if nozzle_info:
                    self.state.nozzle_rack = sorted(
                        [
                            {
                                "id": n.get("id", i),
                                "type": str(n.get("type", "")),
                                "diameter": str(n.get("diameter", "")),
                                "wear": n.get("wear"),
                                "stat": n.get("stat"),
                                # H2C uses "tm", H2D uses "max_temp"
                                "max_temp": n.get("max_temp") or n.get("tm", 0),
                                # H2C uses "sn", H2D uses "serial_number"
                                "serial_number": str(n.get("serial_number") or n.get("sn", "")),
                                # H2C uses "color_m", H2D uses "filament_colour"
                                "filament_color": str(n.get("filament_colour") or n.get("color_m", "")),
                                # H2C uses "fila_id", H2D uses "filament_id"
                                "filament_id": str(n.get("filament_id") or n.get("fila_id", "")),
                                "filament_type": str(n.get("tray_type", "") or n.get("filament_type", "")),
                            }
                            for i, n in enumerate(nozzle_info)
                        ],
                        key=lambda x: x["id"],
                    )
                    if not hasattr(self, "_nozzle_rack_logged") and nozzle_info:
                        self._nozzle_rack_logged = True
                        logger.debug(
                            "[%s] Nozzle info: %d entries, IDs: %s",
                            self.serial_number,
                            len(nozzle_info),
                            [n.get("id") for n in nozzle_info],
                        )
                for nozzle in nozzle_info:
                    idx = nozzle.get("id", 0)
                    if idx < len(self.state.nozzles):
                        if "type" in nozzle and nozzle["type"]:
                            material, flow = _parse_nozzle_type(str(nozzle["type"]))
                            if material:
                                self.state.nozzles[idx].nozzle_type = material
                            if flow:
                                self.state.nozzles[idx].nozzle_flow = flow
                        if "diameter" in nozzle:
                            self.state.nozzles[idx].nozzle_diameter = str(nozzle["diameter"])

        # Normalize vt_tray to list before storing (some firmware sends a dict)
        if "vt_tray" in data and isinstance(data["vt_tray"], dict):
            data["vt_tray"] = [data["vt_tray"]]

        # Preserve AMS, vt_tray, ams_extruder_map, and mapping data when updating raw_data
        # (these fields aren't sent in every MQTT push, only when changed)
        ams_data = self.state.raw_data.get("ams")
        vt_tray_data = self.state.raw_data.get("vt_tray")
        ams_extruder_map_data = self.state.raw_data.get("ams_extruder_map")
        mapping_data = self.state.raw_data.get("mapping")
        self.state.raw_data = data

        # Restore preserved fields immediately after raw_data assignment
        if ams_data is not None:
            self.state.raw_data["ams"] = ams_data
        if vt_tray_data is not None:
            self.state.raw_data["vt_tray"] = vt_tray_data
        if ams_extruder_map_data is not None:
            self.state.raw_data["ams_extruder_map"] = ams_extruder_map_data
        if mapping_data is not None and "mapping" not in data:
            self.state.raw_data["mapping"] = mapping_data

        # Parse developer LAN mode from "fun" field
        if "fun" in data:
            try:
                fun_val = data["fun"]
                fun_int = fun_val if isinstance(fun_val, int) else int(fun_val, 16)
                new_dev_mode = (fun_int & 0x20000000) == 0
                if new_dev_mode != self.state.developer_mode:
                    self.state.developer_mode = new_dev_mode
                    if self.on_state_change:
                        self.on_state_change(self.state)
            except (ValueError, TypeError):
                pass
        elif self.state.developer_mode is None and not self._dev_mode_probed:
            # Two-phase developer mode probe:
            # 1) Wait for a "large" status push (len > 30) confirming printer is streaming
            # 2) Wait at least 5s after connect so the printer has time to send "fun" naturally
            if not self._dev_mode_needs_probe and len(data) > 30:
                self._dev_mode_needs_probe = True
            if self._dev_mode_needs_probe and time.monotonic() - self._connect_time >= 5.0:
                self._probe_developer_mode()
            elif self._dev_mode_needs_probe:
                logger.debug(
                    "[%s] Deferring developer mode probe (%.1fs since connect, need 5s)",
                    self.serial_number,
                    time.monotonic() - self._connect_time,
                )
        elif self._dev_mode_probed and self._dev_mode_probe_seq is not None:
            # Probe sent but no response yet - check for timeout
            elapsed = time.monotonic() - self._dev_mode_probe_time
            if elapsed > 10.0:
                self._dev_mode_probe_failures += 1
                logger.warning(
                    "[%s] Developer mode probe timed out after %.0fs (attempt %d)",
                    self.serial_number,
                    elapsed,
                    self._dev_mode_probe_failures,
                )
                self._dev_mode_probe_seq = None
                if self._dev_mode_probe_failures >= 2:
                    self.force_reconnect_stale_session("developer mode probe unanswered 2×")
                else:
                    self._dev_mode_probed = False  # Allow retry

        # Zombie session detection: if an ams_filament_setting command has been
        # pending for >10s with no response, the publish path is likely dead (#887).
        if self._last_ams_cmd_time > 0:
            elapsed = time.monotonic() - self._last_ams_cmd_time
            if elapsed > 10.0:
                self._ams_cmd_unanswered += 1
                logger.warning(
                    "[%s] ams_filament_setting unanswered for %.0fs (count=%d)",
                    self.serial_number,
                    elapsed,
                    self._ams_cmd_unanswered,
                )
                self._last_ams_cmd_time = 0.0  # don't re-trigger on next push_status
                if self._ams_cmd_unanswered >= 2:
                    self.force_reconnect_stale_session("ams_filament_setting unanswered 2×")
                    self._ams_cmd_unanswered = 0

        # Log mapping data when received (for usage tracking debugging)
        if "mapping" in data:
            logger.debug("[%s] MQTT mapping field: %s", self.serial_number, data["mapping"])

        # Log state transitions for debugging
        if "gcode_state" in data and self.state.state != self._previous_gcode_state:
            logger.debug(
                f"[{self.serial_number}] gcode_state: {self._previous_gcode_state} -> {self.state.state}, "
                f"file: {self.state.gcode_file}, subtask: {self.state.subtask_name}"
            )

        # Detect print start (state changes TO RUNNING with a file)
        current_file = self.state.gcode_file or self.state.current_print
        is_new_print = (
            self.state.state == "RUNNING"
            # ``_previous_gcode_state is None`` means we haven't seen any
            # prior gcode_state in THIS process lifetime yet — either a fresh
            # BamDude start observing a printer already mid-print (the
            # reporter's #1304 scenario in upstream Bambuddy), or a brand-new
            # client that hasn't yet received its first push. In neither case
            # is this a real IDLE→RUNNING transition. Skipping start-fire here
            # is safe: ``_was_running`` still flips below in its own block, so
            # completion detection isn't affected. Mid-print stale reconnects
            # are unaffected — our reconnect path carries ``_previous_gcode_state``
            # across client recreation, so this guard only fires on genuine
            # process-fresh catch-up.
            and self._previous_gcode_state is not None
            and self._previous_gcode_state != "RUNNING"
            and current_file
            and not self._was_running  # Prevent duplicates when resuming from PAUSE
        )
        # Also detect if file changed while running (new print started)
        is_file_change = (
            self.state.state == "RUNNING"
            and current_file
            and current_file != self._previous_gcode_file
            and self._previous_gcode_file is not None
        )

        # Track RUNNING state for more robust completion detection
        running_first_observed = False
        if self.state.state == "RUNNING" and current_file:
            if not self._was_running:
                logger.debug("[%s] Now tracking RUNNING state for %s", self.serial_number, current_file)
                # Check if timelapse was enabled in the same message (xcam parsed before this)
                if self.state.timelapse:
                    self._timelapse_during_print = True
                    logger.debug("[%s] Timelapse detected when entering RUNNING state", self.serial_number)
                # Mark this as the first RUNNING observation of the session.
                # If is_new_print also fires below, on_print_start handles
                # baseline capture and we suppress on_print_running_observed
                # to avoid double-capture. If is_new_print does NOT fire
                # (BamDude started mid-print — the #1304 guard suppressed it),
                # main.py needs this hook to catch the restart-recovery case
                # (#1485 follow-up).
                running_first_observed = True
            self._was_running = True
            self._completion_triggered = False

        if is_new_print or is_file_change:
            # Clear any old HMS errors when a new print starts
            self.state.hms_errors = []
            # Reset layer tracking for new print (needed for layer-based timelapse)
            self.state.layer_num = 0
            # Reset total_layers too so the next print's linear-usage denominator starts clean
            # (paired with the total_layer_num>0 guard above, #1771)
            self.state.total_layers = 0
            # Reset completion tracking for new print
            self._was_running = True
            self._completion_triggered = False
            # #1721: rearm the end-of-print finish-photo trigger for the new print
            self._finish_photo_captured = False
            # Reset last valid progress/layer for usage tracking
            self._last_valid_progress = 0.0
            self._last_valid_layer_num = 0
            # Clear and seed tray change log for mid-print usage splitting
            self.state.tray_change_log.clear()
            tn = self.state.tray_now
            if (
                (0 <= tn <= 15)
                or (A2L_LITE_GLOBAL_BASE <= tn <= A2L_LITE_GLOBAL_BASE + 3)
                or (128 <= tn <= 135)
                or tn == 254
            ):
                self.state.tray_change_log.append((tn, 0))
            # Initialize timelapse tracking based on current state
            # NOTE: xcam data is parsed BEFORE this code runs in _process_message,
            # so self.state.timelapse may already be set from this message.
            # We preserve that value instead of blindly resetting to False.
            if self.state.timelapse:
                self._timelapse_during_print = True
                logger.debug("[%s] Timelapse detected at print start", self.serial_number)
            else:
                self._timelapse_during_print = False

        if (is_new_print or is_file_change) and self.on_print_start:
            logger.info(
                f"[{self.serial_number}] PRINT START detected - file: {current_file}, "
                f"subtask: {self.state.subtask_name}, is_new: {is_new_print}, is_file_change: {is_file_change}"
            )
            self.on_print_start(
                {
                    "filename": current_file,
                    "subtask_name": self.state.subtask_name,
                    "remaining_time": self.state.remaining_time * 60
                    if self.state.remaining_time > 0
                    else None,  # Convert minutes to seconds
                    "raw_data": data,
                    "ams_mapping": self._captured_ams_mapping,
                }
            )
        elif running_first_observed and self.on_print_running_observed:
            # Restart-recovery hook (#1485 follow-up): BamDude started mid-
            # print, so the #1304 first-push guard suppressed on_print_start,
            # but we still need main.py to capture a fresh timelapse baseline
            # before the printer uploads the in-flight MP4. Same payload
            # shape as on_print_start so the consumer can reuse fields.
            logger.info(
                f"[{self.serial_number}] RUNNING observed without PRINT START "
                f"(restart-recovery) - file: {current_file}, subtask: {self.state.subtask_name}"
            )
            self.on_print_running_observed(
                {
                    "filename": current_file,
                    "subtask_name": self.state.subtask_name,
                    "remaining_time": self.state.remaining_time * 60 if self.state.remaining_time > 0 else None,
                    "raw_data": data,
                    "ams_mapping": self._captured_ams_mapping,
                }
            )

        # Detect print completion (FINISH = success, FAILED = error, IDLE = aborted)
        # Use _was_running flag in addition to _previous_gcode_state for more robust detection
        # This handles cases where server restarts during a print
        should_trigger_completion = (
            self.state.state in ("FINISH", "FAILED")
            and not self._completion_triggered
            and self.on_print_complete
            and (
                self._previous_gcode_state == "RUNNING"  # Normal transition
                or (self._was_running and self._previous_gcode_state != self.state.state)  # After server restart
                # Pre-print failure (#1111): printer rejected the job during setup
                # — wrong nozzle size, AMS error, etc. The print never reaches
                # RUNNING, so without this branch neither the RUNNING check nor
                # _was_running match and the queue item stays stuck at "printing".
                # Restricted to FAILED from pre-print states so a stale FAILED on
                # first connection (prev=None) still can't accidentally fire.
                or (self.state.state == "FAILED" and self._previous_gcode_state in ("PREPARE", "SLICING"))
            )
        )
        # For IDLE, only trigger if we just came from RUNNING (explicit abort/cancel)
        if (
            self.state.state == "IDLE"
            and self._previous_gcode_state == "RUNNING"
            and not self._completion_triggered
            and self.on_print_complete
        ):
            should_trigger_completion = True

        # Log when we FIRST see a terminal state but DON'T trigger completion (diagnostics)
        # Only log on the transition (prev != current) to avoid flooding logs every MQTT update
        if (
            not should_trigger_completion
            and self.state.state in ("FINISH", "FAILED")
            and self._previous_gcode_state != self.state.state
        ):
            logger.info(
                f"[{self.serial_number}] State is {self.state.state} but completion NOT triggered: "
                f"prev={self._previous_gcode_state}, was_running={self._was_running}, "
                f"already_triggered={self._completion_triggered}, has_callback={bool(self.on_print_complete)}"
            )
            # Mark as triggered so state is clean for the next print cycle
            self._completion_triggered = True

        if should_trigger_completion:
            if self.state.state == "FINISH":
                status = "completed"
            elif self.state.state == "FAILED":
                status = "failed"
            else:
                status = "aborted"
            logger.info(
                f"[{self.serial_number}] PRINT COMPLETE detected - state: {self.state.state}, "
                f"status: {status}, file: {self._previous_gcode_file or current_file}, "
                f"subtask: {self.state.subtask_name}, was_running: {self._was_running}, "
                f"timelapse_during_print: {self._timelapse_during_print}"
            )
            timelapse_was_active = self._timelapse_during_print
            # #1721 fallback: if the stage-22 trigger never fired (cancel,
            # external-spool-only, HMS halt, or firmware variant that skips
            # the unload phase) fire the finish-photo moment now. Bed has
            # already dropped, framing is worse, but we still capture.
            # Only on successful completion — aborted/failed prints don't
            # produce a meaningful finish photo.
            if status == "completed" and not self._finish_photo_captured and self.on_finish_photo_moment:
                self._finish_photo_captured = True
                logger.info(
                    f"[{self.serial_number}] FINISH PHOTO MOMENT (FINISH fallback) — "
                    f"stage-22 never fired; capturing at FINISH-state transition"
                )
                self.on_finish_photo_moment(
                    {
                        "trigger": "finish_state",
                        "filename": self._previous_gcode_file or current_file,
                        "subtask_name": self.state.subtask_name,
                        "timelapse_was_active": timelapse_was_active,
                    }
                )
            self._completion_triggered = True
            self._was_running = False
            self._timelapse_during_print = False  # Reset for next print
            # Include HMS errors for failure reason detection
            hms_errors_data = (
                [
                    {"code": e.code, "attr": e.attr, "module": e.module, "severity": e.severity}
                    for e in self.state.hms_errors
                ]
                if self.state.hms_errors
                else []
            )
            self.on_print_complete(
                {
                    "status": status,
                    "filename": self._previous_gcode_file or current_file,
                    "subtask_name": self.state.subtask_name,
                    "raw_data": data,
                    "timelapse_was_active": timelapse_was_active,
                    "hms_errors": hms_errors_data,
                    "ams_mapping": self._captured_ams_mapping,
                    # Last valid progress/layer before firmware reset (for partial usage tracking)
                    "last_progress": self._last_valid_progress,
                    "last_layer_num": self._last_valid_layer_num,
                }
            )
            self._captured_ams_mapping = None

        self._previous_gcode_state = self.state.state
        if current_file:
            self._previous_gcode_file = current_file

        if self.on_state_change:
            self.on_state_change(self.state)

    def _probe_developer_mode(self):
        """Probe developer mode by sending an ams_filament_setting for the external slot.

        Some printers (A1/P1 series) never send the "fun" field in MQTT status.
        We detect developer mode by sending a harmless command and checking the response:
        - result="success" → developer mode ON (commands accepted)
        - result="failed", reason="mqtt message verify failed" → developer mode OFF

        The probe re-sends the current external slot config so it's a no-op on success.
        """
        if not self._client or not self.state.connected:
            return
        self._dev_mode_probed = True
        self._dev_mode_needs_probe = False
        self._dev_mode_probe_time = time.monotonic()
        self._sequence_id += 1
        seq = str(self._sequence_id)
        self._dev_mode_probe_seq = seq

        # Build probe command: re-send current external slot config (no-op on success)
        vt_tray = self.state.raw_data.get("vt_tray", []) if self.state.raw_data else []
        current = vt_tray[0] if vt_tray else {}

        command = {
            "print": {
                "command": "ams_filament_setting",
                "ams_id": 255,
                "tray_id": 0,
                "slot_id": 0,
                "tray_info_idx": current.get("tray_info_idx", ""),
                "tray_type": current.get("tray_type", ""),
                "tray_sub_brands": current.get("tray_sub_brands", ""),
                "tray_color": current.get("tray_color", "00000000"),
                "nozzle_temp_min": current.get("nozzle_temp_min", 0),
                "nozzle_temp_max": current.get("nozzle_temp_max", 0),
                "sequence_id": seq,
            }
        }
        setting_id = current.get("setting_id")
        if setting_id:
            command["print"]["setting_id"] = setting_id

        logger.info("[%s] Probing developer mode via ams_filament_setting (seq=%s)", self.serial_number, seq)
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)

    def _handle_dev_mode_probe_response(self, data: dict):
        """Handle response to the developer mode probe command.

        **Three outcomes, not two** (upstream Bambuddy #2732). Reading "anything
        that is not an explicit refusal" as confirmation is what put
        ``developer_mode: pass`` in the support bundle of a printer that had not
        accepted a command all day: this firmware answers the probe with an empty
        result while refusing everything else. An answer we cannot interpret
        leaves the flag at ``None``, and the diagnostic reports ``skip``.
        """
        self._dev_mode_probe_seq = None
        self._dev_mode_probe_failures = 0
        result = data.get("result", "")
        reason = data.get("reason", "")

        if result == "failed" and "verify failed" in reason:
            self.state.developer_mode = False
            self._dev_mode_from_hms = False
            logger.info("[%s] Developer mode probe: DISABLED (reason=%r)", self.serial_number, reason)
        elif result == "success":
            self.state.developer_mode = True
            self._dev_mode_from_hms = False
            logger.info("[%s] Developer mode probe: ENABLED (result=%r)", self.serial_number, result)
        else:
            logger.info(
                "[%s] Developer mode probe: UNKNOWN — the printer answered without saying either way "
                "(result=%r, reason=%r). Leaving it undetermined rather than inferring a pass.",
                self.serial_number,
                result,
                reason,
            )
            return

        if self.on_state_change:
            self.on_state_change(self.state)

    def _handle_command_error_reply(self, print_data: dict) -> None:
        """Every command's verdict, read in one place.

        BS has exactly one of these (``DeviceManager.cpp``): when a reply carries
        both ``command`` and a numeric ``err_code``, and its ``sequence_id`` says
        the reply is to something Studio sent, it hands the code to
        ``add_command_error_code_dlg`` — one router for the whole protocol rather
        than a reader bolted onto each sender. We had one reader, for ``set_ctt``,
        added a fix ago; every other command we publish went out and its answer
        was dropped on the floor. A refusal and a success looked identical.

        Three conditions, each load-bearing:

        * **``err_code > 0``.** BS treats zero and negatives as "no error" on this
          channel — it is a status word, not a return value. (``set_ctt`` is a
          separate mechanism on a different field: ``errno``, where the
          informative values are *negative*. Both exist; do not merge them.)
        * **The sequence id is ours.** This topic is shared with the printer's
          screen, the Bambu app and the cloud. Acting on a stranger's failed
          command would report a fault the operator did not cause and cannot
          find.
        * **A real command.** ``push_status`` and friends carry no verdict.

        Deliberately NOT appended to ``state.hms_errors``, which is where
        ``print_error`` goes. Those entries describe a condition the printer keeps
        re-reporting while it lasts, so they clear when it stops. A command error
        is one-shot — nothing ever un-reports it — so it would sit on the printer
        card as a permanent fault. It is kept as the last verdict instead, and
        that is also what makes it answerable: "did the thing I just asked for
        work?" is a question about the most recent command, not a list.
        """
        err = print_data.get("err_code")
        if not isinstance(err, int) or isinstance(err, bool) or err <= 0:
            return
        if not self._is_our_sequence_id(print_data.get("sequence_id")):
            return

        command = print_data.get("command")
        # Same 32-bit shape as ``print_error`` — BS looks both up in the one HMS
        # catalogue (``HMSQuery::is_internal_error`` formats it with %08X).
        module = (err >> 16) & 0xFFFF
        code = err & 0xFFFF
        self.state.last_command_error = {
            "command": command,
            "err_code": err,
            "short_code": f"{module:04X}_{code:04X}",
            "sequence_id": print_data.get("sequence_id"),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        logger.warning(
            "[%s] command %s failed: err_code=%s (%04X_%04X)",
            self.serial_number,
            command,
            err,
            module,
            code,
        )

    def _handle_upgrade_error_reply(self, upgrade_data: dict) -> None:
        """The other half of the command-error router — firmware's own envelope.

        BS keeps two copies of this check because the printer answers firmware
        operations under ``upgrade`` rather than ``print``. Same ``err_code``,
        same sequence-id question, different key. Ours publishes there too:
        ``ams_firmware_switch`` sends ``mc_for_ams_firmware_upgrade``.

        ⚠️ **The ownership test defaults the other way here, and that is BS's
        choice, not an oversight of ours.** In the ``print`` branch a reply must
        pass ``is_studio_cmd`` to be acted on; in this one BS starts from
        ``check_studio_cmd = true`` and only clears it when a ``sequence_id`` is
        present and outside the band. A firmware reply carrying no sequence id at
        all is therefore still surfaced — reasonable, because nothing but a
        deliberate operation puts one on the wire.

        **Why this is worth more than a log line.** ``ams_firmware_switch``
        latches ``ams_firmware_status = "SWITCHING"`` the moment the publish
        succeeds, copying BS, and only a *report* from the printer ever clears
        it. A refusal is not a report. So one declined switch left the AMS type
        picker hidden and ``POST`` answering 409 "already in progress" — for the
        life of the process, with nothing on the way to say otherwise.
        """
        err = upgrade_data.get("err_code")
        if not isinstance(err, int) or isinstance(err, bool) or err <= 0:
            return

        seq = upgrade_data.get("sequence_id")
        if seq is not None and not self._is_our_sequence_id(seq):
            return

        command = upgrade_data.get("command")
        module = (err >> 16) & 0xFFFF
        code = err & 0xFFFF
        self.state.last_command_error = {
            "command": command,
            "err_code": err,
            "short_code": f"{module:04X}_{code:04X}",
            "sequence_id": seq,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        logger.warning(
            "[%s] upgrade command %s failed: err_code=%s (%04X_%04X)",
            self.serial_number,
            command,
            err,
            module,
            code,
        )

        # Release the optimistic latch, or the refusal is indistinguishable from
        # a switch still running. Cleared to None rather than to a guessed
        # status: what the AMS is actually on will arrive in the next report,
        # and inventing a value here would race it.
        if self.state.ams_firmware_status == "SWITCHING":
            self.state.ams_firmware_status = None
            self.state.ams_firmware_idx_sel = None
            self.state.ams_settings_hold.pop("ams_firmware_switch", None)

    def _handle_set_ctt_reply(self, print_data: dict) -> None:
        """The printer's verdict on a chamber setpoint, which we asked for and
        then ignored.

        BS (``DeviceManager.cpp``, the ``set_ctt`` reply branch) surfaces two
        codes to the operator:

        * ``errno == -2`` — **refused.** Low-temperature filament (PLA/PETG/TPU)
          is loaded, so the firmware will not heat the chamber at all.
        * ``errno == -4`` — **silently retargeted.** A setpoint below 40 °C does
          not activate chamber control; the printer sets the target to 0.

        The second is why this matters beyond a log line: the preheat stage waits
        for the chamber to reach its target, and under ``-4`` there is no target
        to reach. It would wait out its whole timeout and then start the print
        into a cold chamber, reporting nothing — the "failed soak that looks
        exactly like a successful one".

        Recorded on state rather than raised: this arrives on the MQTT thread,
        and the caller that wants it (preheat) is elsewhere.
        """
        errno = print_data.get("errno")
        if not isinstance(errno, int) or errno == 0:
            return

        self.state.temperatures["_chamber_set_errno"] = errno
        if errno == -2:
            logger.warning(
                "[%s] set_ctt refused: low-temperature filament (PLA/PETG/TPU) is loaded, "
                "the firmware will not heat the chamber",
                self.serial_number,
            )
            # The setpoint did not take. Drop the optimistic local target so the
            # soak is not waiting on a number the printer rejected.
            self.state.temperatures["chamber_target"] = 0.0
            self.state.temperatures.pop("_chamber_target_set_time", None)
        elif errno == -4:
            logger.warning(
                "[%s] set_ctt below 40C: chamber control not activated, printer set the target to 0",
                self.serial_number,
            )
            self.state.temperatures["chamber_target"] = 0.0
            self.state.temperatures.pop("_chamber_target_set_time", None)
        else:
            logger.warning("[%s] set_ctt returned errno=%s", self.serial_number, errno)

    def _apply_series_calibration_clamps(self) -> None:
        """BS's two hardcoded overrides, applied after every bitfield read.

        ``DeviceManager.cpp``, at BOTH parse sites (``parse_home_flag`` and the
        ``fun`` parse), with Bambu's own comment::

            if (is_series_o()) is_support_flow_calibration = false;
                // todo: Temp modification due to incorrect machine push message for H2D
            if (is_series_p()) is_support_pa_calibration = false;
                // todo: Temp modification due to incorrect machine push message for P

        This is **firmware Bambu knows is lying**: the machine advertises a
        capability it does not have, and BS refuses to believe the bit. A
        data-driven port misses these by construction, which is why they sit
        here rather than in the config layer — the config is right, the *printer*
        is wrong.

        ⚠️ The series comes from ``printer_series`` in the mirrored config, not
        from a model guess: the X2D reports ``series_x1``, so the O-clamp covers
        the H2 family only. Getting that from a name would have caught X2D too.

        Kept as its own method because it must run after **each** source — BS
        clamps at both parse sites, and a later source that skipped the clamp
        would silently re-enable what the earlier one refused.
        """
        # Local import, matching how this file reaches every other util module
        # (printer_models is imported the same way) — keeps the import graph
        # acyclic without anyone having to check.
        from backend.app.utils.printer_configs import printer_series

        series = printer_series(self.model)
        if series == "series_o":
            self.state.is_support_auto_flow_calibration = False
        elif series == "series_p1p":
            self.state.is_support_pa_calibration = False

    def _apply_mqtt_verify_state(self, verify_failed: bool) -> None:
        """Reconcile ``developer_mode`` with the printer's own verdict on our commands.

        :data:`HMS_MQTT_VERIFY_FAILED` is the only *direct* evidence we ever get
        that control commands are being refused, so it outranks the probe in both
        directions:

        * **present** → ``developer_mode`` is definitively False, whatever the
          probe concluded. The probe can only read the response to its own
          ``ams_filament_setting``; on P1 firmware a refusal is reported here
          instead, so the probe answers ENABLED while every print silently dies.
        * **gone again** → drop the HMS-derived False back to unknown and re-arm
          the probe, so a user who enables Developer Mode and restarts the
          printer is not stuck behind a verdict nothing would ever revisit.

        A False that came from the probe itself is left alone — this only ever
        unwinds its own latch.
        """
        if verify_failed:
            if not self._dev_mode_from_hms:
                logger.warning(
                    "[%s] Printer reported HMS %s (MQTT command verification failed): it is "
                    "rejecting control commands, so prints, temperature changes and filament "
                    "loads will be ignored. Enable Developer Mode on the printer and restart it.",
                    self.serial_number,
                    HMS_MQTT_VERIFY_FAILED,
                )
            self._dev_mode_from_hms = True
            self.state.developer_mode = False
            return

        if self._dev_mode_from_hms:
            logger.info(
                "[%s] HMS %s cleared — developer mode is undetermined again, re-probing.",
                self.serial_number,
                HMS_MQTT_VERIFY_FAILED,
            )
            self._dev_mode_from_hms = False
            self.state.developer_mode = None
            self._dev_mode_probed = False
            self._dev_mode_needs_probe = True

    def _request_push_all(self):
        """Request full status update from printer."""
        if self._client:
            message = {"pushing": {"command": "pushall"}}
            self._client.publish(self.topic_publish, json.dumps(message), qos=1)

    def _request_version(self):
        """Request firmware version info from printer."""
        if self._client:
            self._sequence_id += 1
            message = {
                "info": {
                    "sequence_id": str(self._sequence_id),
                    "command": "get_version",
                }
            }
            logger.debug("[%s] Requesting firmware version info", self.serial_number)
            self._client.publish(self.topic_publish, json.dumps(message), qos=1)

    def request_status_update(self) -> bool:
        """Request a full status update from the printer (public API).

        Sends both pushall and get_accessories commands to refresh all data
        including nozzle hardware info.

        Returns:
            True if the request was sent, False if not connected.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] request_status_update: not connected", self.serial_number)
            return False
        logger.debug("[%s] Requesting status update (pushall)", self.serial_number)
        self._request_push_all()
        # Note: get_accessories returns stale nozzle data on H2D.
        # The correct nozzle data comes from push_status response.
        return True

    def _prime_kprofile_request(self):
        """Send a priming K-profile request on connect.

        Bambu printers often ignore the first K-profile request after connection,
        so we send a dummy request on connect to 'prime' the system.
        """
        if self._client:
            self._sequence_id += 1
            command = {
                "print": {
                    "command": "extrusion_cali_get",
                    "filament_id": "",
                    "nozzle_diameter": "0.4",
                    "sequence_id": str(self._sequence_id),
                }
            }
            logger.debug("[%s] Sending K-profile priming request", self.serial_number)
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)

    def connect(self, loop: asyncio.AbstractEventLoop | None = None):
        """Connect to the printer MQTT broker.

        Args:
            loop: The asyncio event loop to use for thread-safe callbacks.
                  If not provided, will try to get the running loop.
        """
        self._loop = loop
        BambuMQTTClient._client_instance_counter += 1
        client_id = f"bamdude_{self.serial_number}_{os.getpid()}_{BambuMQTTClient._client_instance_counter}"
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )

        # Bambu's broker has racy PUBACK matching with paho's QoS=1 inflight
        # tracking (#1164). The default ceiling of 20 wedges sessions after
        # ~16-20 cumulative commands; lifting it well above any realistic
        # session count keeps QoS=1 working without changing wire-protocol
        # behaviour across printer models (A1, P1S, X1C, H2D, P2S, X2D —
        # all need QoS=1 for reliability). The 0.4.x watchdog reconnect
        # stays as defence-in-depth.
        self._client.max_inflight_messages_set(1000)

        self._client.username_pw_set("bblp", self.access_code)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message

        # TLS setup - Bambu uses self-signed certs
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        # Same reasoning as ImplicitFTP_TLS in bambu_ftp.py: create_default_context()
        # inherits its TLS floor from the OpenSSL build instead of declaring one.
        # Every Bambu broker (X1C, H2D on :8883) speaks TLS 1.2 and refuses
        # 1.0/1.1, so this floor is a no-op on the wire and closes the gap on
        # bare-metal installs whose build would otherwise allow TLS 1.0.
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._client.tls_set_context(ssl_context)

        # Backoff reconnects to avoid tight reconnect loops on unstable brokers.
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

        # Keepalive: paho sends PINGREQs at this interval, broker considers
        # client dead at 1.5x.  30s is a good balance - fast enough to detect
        # real network loss (45s), not so aggressive that transient hiccups
        # trigger false disconnects.  Stale detection (60s no messages) handles
        # the P1S/P1P firmware bug where the broker stops publishing but the
        # TCP connection stays alive.
        self._client.connect_async(self.ip_address, self.MQTT_PORT, keepalive=30)
        self._client.loop_start()

    def start_print(
        self,
        filename: str,
        plate_id: int = 1,
        ams_mapping: list[int] | None = None,
        bed_levelling: str | bool = True,
        flow_cali: str | bool = False,
        layer_inspect: bool = False,
        timelapse: bool = False,
        use_ams: bool = True,
        nozzle_offset_cali: str | bool = False,
        nozzle_mapping: str | None = None,
        storage: str = "external",
        file_md5: str = "",
        timelapse_storage: str | None = None,
    ):
        """Start a print job on the printer.

        The file should already be uploaded — to the printer's root directory
        over FTP, or into its internal storage over the file tunnel.

        Args (this stage):
            storage: ``"external"`` (the card, via FTP) or ``"internal"`` (eMMC,
                via the tunnel). Decides the URL scheme, which is **not** one
                form with the medium substituted — see the command below.
            file_md5: digest of the uploaded bytes, used only on the internal
                path. The FTP path has always sent an empty one.
            timelapse_storage: where the recording goes — ``"internal"`` or
                ``"external"``, already resolved against the card's state by
                :func:`~backend.app.utils.timelapse.resolve_storage`. ``None``
                leaves the machine to its own devices, which is what every
                dispatch did before this parameter existed.

        Args:
            filename: Name of the uploaded file
            plate_id: Plate number to print (default 1)
            ams_mapping: List of tray IDs for each filament slot in the 3MF.
                         Global tray ID = (ams_id * 4) + slot_id, external = 254
            timelapse: Record timelapse video
            bed_levelling: Auto bed levelling before print
            flow_cali: Flow/pressure advance calibration
            layer_inspect: First layer AI inspection
            use_ams: Use AMS for automatic filament changes
            nozzle_offset_cali: Run nozzle offset calibration before print
                (dual-nozzle printers only — silently forced off on single-nozzle)
            nozzle_mapping: Opaque JSON string captured from BambuStudio's
                project_file for H2C rack-swap (O1C2) (#1780). When non-null
                AND the printer is dual-nozzle, parsed and injected as the
                ``nozzle_mapping`` array on the dispatched project_file so the
                firmware honours the user's slicer pick instead of falling
                back to "last matching nozzle" auto-pick. Silently ignored on
                single-nozzle printers; fail-open on malformed JSON.

        Note: the ``vibration_cali`` field in the MQTT payload is kept for
        firmware compatibility but hardcoded to False. Upstream Bambu Studio
        also hardcodes it off for every model (per-print vibration calibration
        was removed from the Studio UI); the standalone calibration wizard
        remains the only way to run it.

        Returns True when the start command was published, False/None otherwise
        (not connected, or the printer is already busy — see the run-state guard).
        """
        # Never dispatch project_file to a printer that is not idle (#2598). This
        # is the single publish choke point for every dispatch path — the queue
        # scheduler, a manual start, a webhook, and a Virtual-Printer forwarded
        # job all funnel through here — so one guard covers them all. Firmware
        # rejects a start while busy with 0500_4004 ("Device is busy and cannot
        # start a new task"), and on an A1 mini that error cancels the RUNNING
        # job. IDLE / FINISH / FAILED are valid start targets; only active-print
        # states are refused. Callers treat False here as a DEFER (leave the queue
        # item pending), not a failure.
        if self.state.state in _ACTIVE_PRINT_STATES:
            logger.warning(
                "[%s] start_print refused: printer busy (gcode_state=%s) — not publishing project_file for %s",
                self.serial_number,
                self.state.state,
                filename,
            )
            return False

        if self._client and self.state.connected:
            # Bambu print command format - matches Bambu Studio's format
            # Build ams_mapping2 from ams_mapping (detailed format with ams_id/slot_id)
            ams_mapping2 = []
            # BambuStudio converts virtual tray IDs (254/255) to -1 in the flat
            # ams_mapping and relies on ams_mapping2 for external spool details.
            # Passing raw 254/255 in the flat array causes H2D firmware to fail
            # with 0700_8012 "Failed to get AMS mapping table".
            flat_ams_mapping = []
            if ams_mapping is not None:
                for tray_id in ams_mapping:
                    # Ensure tray_id is an integer (may be string from JSON)
                    tray_id = int(tray_id) if tray_id is not None else -1
                    if tray_id == -1:
                        # Unmapped filament slot
                        flat_ams_mapping.append(-1)
                        ams_mapping2.append({"ams_id": 255, "slot_id": 255})
                    elif tray_id >= 254:
                        # External/virtual spool: each virtual tray is its own AMS unit
                        # with a single slot (slot 0). BambuStudio convention:
                        #   255 = VIRTUAL_TRAY_MAIN_ID (main/left nozzle)
                        #   254 = VIRTUAL_TRAY_DEPUTY_ID (deputy/right nozzle)
                        # Single-nozzle printers (P1S, A1, X1C, **H2S**) always need ams_id=255.
                        # Only dual-nozzle printers use the actual tray_id (254 for deputy).
                        # Flat mapping must use -1 (firmware doesn't accept raw 254/255).
                        # Source-of-truth is the runtime ``_is_dual_nozzle`` flag set from
                        # device.extruder.info (>=2 entries); model name is the fallback
                        # for the brief window after connect before push data arrives.
                        # Upstream Bambuddy #1386 / commit 96fd4bb7 — H2S shares the H
                        # family's calibration-int format but is single-nozzle, so the
                        # previous classifier that put H2S into the dual-nozzle bucket
                        # silently routed external-spool prints to ams_id=254 and the
                        # firmware rejected the dispatch with ``07FF_8012``.
                        _is_dual_nozzle = self._is_dual_nozzle or (
                            self.model and self.model.upper().strip() in ("H2D", "H2D PRO", "H2DPRO", "H2C", "X2D")
                        )
                        ext_ams_id = tray_id if _is_dual_nozzle else 255
                        flat_ams_mapping.append(-1)
                        ams_mapping2.append({"ams_id": ext_ams_id, "slot_id": 0})
                    elif tray_id >= 128:
                        # AMS-HT: global tray ID IS the ams_id (single tray per unit)
                        flat_ams_mapping.append(tray_id)
                        ams_mapping2.append({"ams_id": tray_id, "slot_id": 0})
                    elif (_a2l := a2l_lite_wire_ids(tray_id // 4, tray_id)) is not None:
                        # A2L AMS-Lite (normalised global 24-27): flat mapping is the
                        # LOCAL slot 0-3 and ams_mapping2 carries {ams_id:16,
                        # slot_id:0-3} — both CONFIRMED against the firmware's own
                        # mapping (flat [1], ams_mapping2 {ams_id:16, slot_id:1}).
                        _wire_ams, _wire_slot, _ = _a2l
                        flat_ams_mapping.append(_wire_slot)
                        ams_mapping2.append({"ams_id": _wire_ams, "slot_id": _wire_slot})
                    else:
                        # Regular AMS tray: Global tray ID = (ams_id * 4) + slot_id
                        ams_id = tray_id // 4
                        slot_id = tray_id % 4
                        flat_ams_mapping.append(tray_id)
                        ams_mapping2.append({"ams_id": ams_id, "slot_id": slot_id})

            # Bambu print command format — matches Bambu Studio's format.
            # The calibration/leveling fields (timelapse, bed_leveling,
            # flow_cali, vibration_cali, layer_inspect) are JSON booleans for
            # every model. An earlier revision integer-encoded them for the H2
            # family (H2D/H2S/H2C/X2D) on the belief that H2 firmware required
            # 0/1 — but a BambuStudio request-topic capture from a real H2D
            # sends plain booleans, and the integer encoding made the H2S
            # silently skip flow-dynamics calibration (#1478). use_ams is the
            # one field that genuinely must stay boolean: H2D Pro firmware
            # reads an integer use_ams as a nozzle index (1 = deputy), which is
            # what actually caused the wrong-extruder routing behind #1386.
            # Dual-nozzle gating for AMS-routing / use_ams branches. EXCLUDES
            # H2S (single-nozzle). Source-of-truth is the runtime
            # ``_is_dual_nozzle`` flag set from device.extruder.info (>=2
            # entries); model name is the fallback for the brief window after
            # connect before push data arrives. Upstream Bambuddy #1386.
            is_dual_nozzle = self._is_dual_nozzle or (
                self.model and self.model.upper().strip() in ("H2D", "H2D PRO", "H2DPRO", "H2C", "X2D")
            )

            # Reconcile use_ams against the resolved ams_mapping for single-nozzle
            # printers — the mapping is authoritative about whether this print
            # actually feeds from the AMS. Skip for dual-nozzle printers, where
            # use_ams encodes nozzle routing rather than an AMS on/off flag.
            #
            # Two symmetric corrections:
            # (a) A mapping that resolves a *real* AMS tray (0-253) forces
            #     use_ams=True even if it arrived False. A print sliced against a
            #     Virtual Printer (which advertises no AMS) carries use_ams=false on
            #     its queue item, but at dispatch the AutoQueue colour-matches a real
            #     printer and resolves a real AMS slot; without this the stale False
            #     reaches the printer, which ignores the mapped slot and aborts at
            #     layer 0 on the empty external spool (#2595).
            # (b) Only an *explicit* external/virtual spool (254/255) may downgrade
            #     to use_ams=False (P1S/P1P with no AMS rejects use_ams=True). An
            #     unresolved slot (-1) does NEITHER — treating it as external
            #     silently started the print against an empty feed (#2589). Real tray
            #     0-253, external >=254, unresolved -1 stay distinct so an unresolved
            #     mapping fails loudly (or is recomputed upstream) instead of going
            #     external, and is never force-enabled by (a).
            if ams_mapping and not is_dual_nozzle:
                has_real_tray = any(t is not None and 0 <= int(t) <= 253 for t in ams_mapping)
                all_external = all(t is None or int(t) >= 254 for t in ams_mapping)
                if has_real_tray and not use_ams:
                    use_ams = True
                    logger.info(
                        "[%s] AMS mapping resolved a real slot — setting use_ams=True (#2595)",
                        self.serial_number,
                    )
                elif use_ams and all_external:
                    use_ams = False
                    logger.info(
                        "[%s] All filament slots use external spool — setting use_ams=False",
                        self.serial_number,
                    )

            # No-AMS external spool fix (PR #2 by latsss):
            # On printers without a physical AMS, firmware rejects -1 (unmapped)
            # and 254 (virtual tray) in ams_mapping with 0700_8012 "Failed to get
            # AMS mapping table". Fix: remap -1→0 and omit ams_mapping2 entirely.
            # Dual-nozzle excluded — use_ams controls nozzle routing on those.
            no_ams_printer = not use_ams and not is_dual_nozzle and not self.state.raw_data.get("ams")
            if no_ams_printer and flat_ams_mapping:
                flat_ams_mapping = [0 if v == -1 else v for v in flat_ams_mapping]
                logger.info(
                    "[%s] No AMS detected — remapped external spool: ams_mapping=%s, omitting ams_mapping2",
                    self.serial_number,
                    flat_ams_mapping,
                )

            # Unique submission ID per invocation. Third-party MQTT observers
            # (OctoEverywhere, etc.) were treating every reprint as a continuation
            # of the prior task when project_id/subtask_id/task_id were hardcoded
            # "0"; the printer kept broadcasting the old gcode_start_time and
            # observers accumulated compounding durations (#1011).
            # Cap at signed int32 max: P1S firmware (01.10.00.00) clamps oversized
            # task identity fields to 2**31-1, so raw epoch-ms (13 digits, ~1.7e12)
            # overflows and every submission ends up with the same task_id from
            # the printer's perspective — the printer then treats a fresh dispatch
            # as a continuation of the last FAILED job and never leaves IDLE (#1042).
            # Modulo keeps uniqueness within a ~24-day wrap window; `or 1` guards
            # the (astronomically unlikely) zero case since task_id=0 is rejected.
            submission_id = str(int(time.time() * 1000) % 2_147_483_647 or 1)

            # Tri-state calibration (off/auto/on) → wire values. Accepts the
            # legacy bool from every existing caller (byte-identical: off/on map
            # to today's 0/1 + False/True) and the new tri-state string. `auto`
            # (2) is clamped down to `on` (1) on any model whose firmware lacks
            # the auto mode, so 2 only ever reaches a supporting machine. The
            # bool companions (`bed_leveling`/`flow_cali`) stay `mode=='on'`
            # (auto → False), matching BambuStudio. nozzle stays dual-only.
            from backend.app.schemas.calibration_mode import clamp_auto, mode_to_bool, mode_to_int
            from backend.app.utils.printer_models import (
                supports_auto_bed_leveling,
                supports_auto_flow_cali,
                supports_auto_nozzle_offset,
            )

            bed_leveling_bool = mode_to_bool(bed_levelling)
            flow_cali_bool = mode_to_bool(flow_cali)
            auto_bed_leveling_int = clamp_auto(mode_to_int(bed_levelling), supports_auto_bed_leveling(self.model))
            extrude_cali_flag_int = clamp_auto(mode_to_int(flow_cali), supports_auto_flow_cali(self.model))
            nozzle_offset_cali_int = (
                clamp_auto(mode_to_int(nozzle_offset_cali), supports_auto_nozzle_offset(self.model))
                if is_dual_nozzle
                else 0
            )

            command = {
                "print": {
                    "sequence_id": "20000",
                    "command": "project_file",
                    "param": f"Metadata/plate_{plate_id}.gcode",
                    # ⚠️ The two media use DIFFERENT URL schemes, not one scheme
                    # with the storage substituted. Internal takes the storage as
                    # the host and the bare filename with no path at all;
                    # external stays on ftp://. ``brtc://udisk/…`` does not
                    # exist — BambuStudio sends file:///media/usb0/cache/… for a
                    # removable medium, and we never print from one this way.
                    "url": (f"brtc://emmc/{filename}" if storage == "internal" else f"ftp://{filename}"),
                    # ⚠️ ``file`` stays the bare name on BOTH media. Changing it
                    # alongside the url is the natural mistake.
                    "file": filename,
                    # ⚠️ UPPERCASE here, while the tunnel's own upload frame
                    # carries the same digest in lowercase. Sent on BOTH media
                    # now: the FTP path was empty because Bambu's own capture
                    # puts a "from_sd_card" sentinel there for removable media,
                    # but Orca sends a real digest on exactly this command and
                    # this url scheme (captured off a P1S, 2026-08-16). Empty
                    # when the caller has no digest — the key is always sent,
                    # because older firmware rejects a command missing one it
                    # expects.
                    "md5": (file_md5 or "").upper(),
                    "bed_type": "auto",
                    "timelapse": timelapse,
                    "bed_leveling": bed_leveling_bool,
                    "auto_bed_leveling": auto_bed_leveling_int,
                    "flow_cali": flow_cali_bool,
                    # Hardcoded off — upstream Bambu Studio does the same for
                    # every model. Kept in the payload only because older
                    # firmware versions reject the command if the key is
                    # missing. (BamDude decision — per-print vibration cali is
                    # the standalone wizard's job; see method docstring.)
                    "vibration_cali": False,
                    "layer_inspect": layer_inspect,
                    "use_ams": use_ams,
                    # Bit 2 = "record the timelapse to internal storage".
                    # ⚠️ This key was here from the first day, pinned to "0"
                    # because it was copied out of a BambuStudio capture whose
                    # value happened to be zero — External was picked in it, and
                    # a field that reads 0 in every sample looks like padding.
                    # It is not: Studio sends "4" for Internal, and the printer
                    # obeys it over whatever the previous job did.
                    "cfg": task_cfg(timelapse=bool(timelapse), storage=timelapse_storage),
                    # extrude_cali_flag gates flow-dynamics calibration:
                    # 1 = run it, 0 = drop the stage from stg entirely.
                    # We previously sent 2 for "off" (#1478), reading 2 as "skip
                    # the explicit pass but verify stored PA via the cali stage".
                    # Live H2D 01.x testing showed 2 still queued stage 8
                    # ("Calibrating dynamic flow") — near-no-op on K, but the
                    # physical pass still runs. A BambuStudio Send-dialog capture
                    # on the same firmware sends 0 when Flow Calibration is
                    # unchecked; 0 is what actually removes stage 8 (#1721 series).
                    "extrude_cali_flag": extrude_cali_flag_int,
                    "extrude_cali_manual_mode": 0,
                    # 1 = run, 0 = drop the stage (stage 39, "Nozzle offset
                    # calibration"). Sending 2 for "off" left stage 39 queued on
                    # live H2D 01.x (same 2-vs-0 issue as extrude_cali_flag).
                    # BambuStudio exposes the toggle only for dual-nozzle
                    # machines (H2D/H2D Pro/H2C/X2D); on single-nozzle printers we
                    # always drop it so firmware never wastes cycles on a
                    # calibration their head doesn't support (#1682).
                    "nozzle_offset_cali": nozzle_offset_cali_int,
                    "subtask_name": filename.replace(".3mf", "").replace(".gcode", ""),
                    "profile_id": "0",
                    "project_id": submission_id,
                    "subtask_id": submission_id,
                    "task_id": submission_id,
                }
            }

            # Add AMS mapping if provided
            if ams_mapping is not None:
                command["print"]["ams_mapping"] = flat_ams_mapping
                if not no_ams_printer:
                    command["print"]["ams_mapping2"] = ams_mapping2

            # H2C dual-nozzle-rack slicer-pick preservation (#1780).
            # ``nozzle_mapping`` carries the slicer's per-filament physical
            # nozzle position IDs (``list[int]``), JSON-string-encoded when it
            # leaves the queue item; parse here so the wire ships the array,
            # matching BambuStudio's project_file shape. Gate on
            # ``is_dual_nozzle`` — single-nozzle firmwares would ignore it and
            # we err on the side of not emitting unrecognised fields. A parse
            # failure is logged but never blocks the dispatch (fail-open — the
            # firmware falls back to its auto-pick path, i.e. pre-fix behaviour).
            if is_dual_nozzle and nozzle_mapping:
                try:
                    command["print"]["nozzle_mapping"] = json.loads(nozzle_mapping)
                except json.JSONDecodeError:
                    logger.warning(
                        "[%s] Invalid nozzle_mapping JSON on dispatch, omitting (firmware auto-picks): %r",
                        self.serial_number,
                        nozzle_mapping,
                    )

            logger.info("[%s] Sending print command: %s", self.serial_number, json.dumps(command))
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)
            # Record what we dispatched so /cover can pick the right plate
            # thumbnail even when the printer's gcode_file echo is just the
            # 3MF filename without a plate path (#1166). Match the same
            # subtask_name shape we send so the comparison in resolve_plate_id
            # works against state.subtask_name reflected back via MQTT.
            self.state.dispatched_plate_id = plate_id
            self.state.dispatched_subtask = command["print"]["subtask_name"]
            return True
        else:
            # Log why we couldn't send the command
            if not self._client:
                logger.error("[%s] Cannot start print: MQTT client not initialized", self.serial_number)
            elif not self.state.connected:
                logger.error(
                    f"[{self.serial_number}] Cannot start print: Printer not connected (client exists but disconnected). "
                    f"Connection state: {self.state.connected}, Last message: {self._last_message_time}"
                )
            return False

    def stop_print(self) -> bool:
        """Stop the current print job."""
        if self._client and self.state.connected:
            command = {"print": {"command": "stop", "sequence_id": "0"}}
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)
            logger.info("[%s] Sent stop print command", self.serial_number)
            return True
        return False

    # ---------- Printer Settings dialog publishers (Print Options tab) ----------
    # Each publisher matches BS DeviceCore/DevPrintOptions.cpp shapes. All use
    # ``print.command = "print_option"`` with one toggle field per call;
    # snapshot uses ``camera.command = "ipcam_cap_pic_set"``. Return
    # ``(success, sequence_id)``. Hold-timer is stamped on
    # ``state.printer_settings_hold`` so the push parser doesn't clobber
    # the optimistic value during the printer's confirm round-trip.

    def _publish_print_option_bool(
        self, field: str, hold_key: str, enabled: bool, legacy_option_bit: int | None = None
    ) -> tuple[bool, str | None]:
        """Publish one ``print_option`` toggle.

        ``legacy_option_bit`` adds BS's ``option`` bitmask alongside the named
        bool. Only ``auto_recovery`` gets one: BS builds it in
        ``command_set_printing_option``, which takes that single flag and nothing
        else (``option = auto_recovery << PRINT_OP_AUTO_RECOVERY``, and
        ``PRINT_OP_AUTO_RECOVERY`` is 0). The other toggles have no bit and must
        not invent one.
        """
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command: dict = {"print": {"command": "print_option", "sequence_id": seq, field: bool(enabled)}}
        if legacy_option_bit is not None:
            command["print"]["option"] = int(enabled) << legacy_option_bit
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold[hold_key] = time.time()
        return True, seq

    def _publish_print_option_int(self, field: str, hold_key: str, value: int) -> tuple[bool, str | None]:
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {"print": {"command": "print_option", "sequence_id": seq, field: int(value)}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold[hold_key] = time.time()
        return True, seq

    def print_option_auto_recovery(self, enabled: bool) -> tuple[bool, str | None]:
        # BS ships BOTH the named bool and the legacy ``option`` bitmask on this
        # one command; some firmware revisions reject it when only one is
        # present. That was known here — and written down — in a helper nothing
        # called, while the live publisher sent the bool alone.
        return self._publish_print_option_bool(
            "auto_recovery", "auto_recovery", enabled, legacy_option_bit=PRINT_OP_AUTO_RECOVERY
        )

    def print_option_sound(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("sound_enable", "sound_enable", enabled)

    def print_option_filament_tangle(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("filament_tangle_detect", "filament_tangle", enabled)

    def print_option_nozzle_blob(self, enabled: bool) -> tuple[bool, str | None]:
        return self._publish_print_option_bool("nozzle_blob_detect", "nozzle_blob", enabled)

    def print_option_nozzle_blob_v2(self, value: int) -> tuple[bool, str | None]:
        # Smart nozzle blob (BS): print_option nozzle_blob_detect_v2, 0 off / 1 on / 2 auto.
        return self._publish_print_option_int("nozzle_blob_detect_v2", "smart_nozzle_blob", value)

    def _publish_xcam_setting(self, module: str, hold_key: str, enabled: bool) -> tuple[bool, str | None]:
        """xcam_control_set for a plate/mark toggle, stamping the settings hold
        under ``hold_key`` (so the matching value-read respects it). BS routes
        these through command_xcam_control, not print_option."""
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "xcam": {
                "command": "xcam_control_set",
                "sequence_id": seq,
                "module_name": module,
                "control": bool(enabled),
                "enable": bool(enabled),
                "print_halt": True,
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold[hold_key] = time.time()
        return True, seq

    def print_option_plate_type(self, enabled: bool) -> tuple[bool, str | None]:
        # BS: buildplate type toggle rides xcam_control "buildplate_marker_detector".
        return self._publish_xcam_setting("buildplate_marker_detector", "plate_type", enabled)

    def print_option_plate_align(self, enabled: bool) -> tuple[bool, str | None]:
        # BS: alignment toggle rides xcam_control "plate_offset_switch".
        return self._publish_xcam_setting("plate_offset_switch", "plate_align", enabled)

    def print_option_plate_mark(self, enabled: bool) -> tuple[bool, str | None]:
        # Legacy "detection of build plate position" — same xcam module as type.
        return self._publish_xcam_setting("buildplate_marker_detector", "plate_mark", enabled)

    def print_option_purify_air(self, value: int) -> tuple[bool, str | None]:
        return self._publish_print_option_int("air_purification", "purify_air", value)

    def set_door_open_check(self, value: int) -> tuple[bool, str | None]:
        """Set open-door detection (BS DoorOpenCheckState): 0 disable / 1 notification
        / 2 pause. Uses the ``system``/``set_door_stat`` command — NOT print_option
        (mirrors BS ``command_set_door_open_check``)."""
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {"system": {"command": "set_door_stat", "sequence_id": seq, "config": int(value)}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold["open_door"] = time.time()
        return True, seq

    def set_idle_heating(self, enabled: bool) -> tuple[bool, str | None]:
        """Toggle idle heating protection (BS ``set_against_continued_heating_mode``)."""
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "print": {"command": "set_against_continued_heating_mode", "sequence_id": seq, "enable": bool(enabled)}
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold["idle_heating"] = time.time()
        return True, seq

    def print_option_save_remote_to_storage(self, value: int) -> tuple[bool, str | None]:
        # BS: system/print_cache_set (config bool), NOT print_option.
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {"system": {"command": "print_cache_set", "sequence_id": seq, "config": bool(value)}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold["save_remote_to_storage"] = time.time()
        return True, seq

    def camera_snapshot_enable(self, enabled: bool) -> tuple[bool, str | None]:
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "camera": {
                "command": "ipcam_cap_pic_set",
                "sequence_id": seq,
                "control": "enable" if enabled else "disable",
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold["snapshot"] = time.time()
        return True, seq

    def xcam_control_for_settings(
        self,
        module: str,
        enabled: bool,
        sensitivity: str | None = None,
    ) -> tuple[bool, str | None]:
        """Publish one ``xcam_control_set`` for the Printer Settings router.

        The single writer of this command since its predecessor was deleted.
        That one always appended ``halt_print_sensitivity``, so toggling a
        detector that has no sensitivity of its own — first-layer inspection,
        the buildplate marker — still shipped a sensitivity field the printer
        then applied to whatever detector owns it. This one:
          - returns (ok, sequence_id) for audit-trail correlation,
          - omits ``halt_print_sensitivity`` when ``sensitivity is None``,
          - stamps ``printer_settings_hold[module]``.
        Wire format from BS DevPrintOptions.cpp::command_xcam_control.
        """
        if not self._client or not self.state.connected:
            return False, None
        # Our API/frontend module keys differ from the wire names BS/the printer
        # expect for several detectors — remap before publishing.
        _wire = {
            "purgechutepileup_detector": "pileup_detector",
            "nozzleclumping_detector": "clump_detector",
            "airprinting_detector": "airprint_detector",
            "displacement_detection": "model_movement_check",
            "ai_monitoring": "printing_monitor",
        }.get(module, module)
        self._sequence_id += 1
        seq = str(self._sequence_id)
        command: dict = {
            "xcam": {
                "command": "xcam_control_set",
                "sequence_id": seq,
                "module_name": _wire,
                "control": bool(enabled),
                "enable": bool(enabled),
                "print_halt": True,
            }
        }
        if sensitivity is not None:
            command["xcam"]["halt_print_sensitivity"] = sensitivity
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.printer_settings_hold[module] = time.time()
        return True, seq

    def start_calibration(
        self,
        bed_leveling: bool = False,
        vibration: bool = False,
        motor_noise: bool = False,
        nozzle_offset: bool = False,
        high_temp_heatbed: bool = False,
        lidar: bool = False,
        clump_pos: bool = False,
    ) -> bool:
        """Start printer calibration with selected options.

        Args:
            bed_leveling: Run bed leveling calibration
            vibration: Run vibration compensation calibration
            motor_noise: Run motor noise cancellation calibration
            nozzle_offset: Run nozzle offset calibration (dual nozzle printers)
            high_temp_heatbed: Run high-temperature heatbed calibration
            lidar: Run micro-lidar (xcam) calibration (X1 series)
            clump_pos: Run nozzle-clumping-detection calibration (P2S / H2S)

        Returns:
            True if command was sent, False if not connected
        """
        if not self._client or not self.state.connected:
            return False

        # Build calibration bitmask — matches BambuStudio DeviceManager.cpp
        # command_start_calibration (:1886-1892). The printer runs each selected
        # step, then returns to IDLE. Which bits are *available* is gated
        # per-model in the API (utils/printer_configs.py); the firmware ignores
        # unsupported bits.
        # Bit 0: xcam_cali (micro-lidar)
        # Bit 1: bed_leveling
        # Bit 2: vibration
        # Bit 3: motor_noise
        # Bit 4: nozzle_cali (nozzle offset)
        # Bit 5: bed_cali (high-temp heatbed)
        # Bit 6: clumppos_cali (nozzle-clumping detection)
        option = 0
        if lidar:
            option |= 1 << 0
        if bed_leveling:
            option |= 1 << 1
        if vibration:
            option |= 1 << 2
        if motor_noise:
            option |= 1 << 3
        if nozzle_offset:
            option |= 1 << 4
        if high_temp_heatbed:
            option |= 1 << 5
        if clump_pos:
            option |= 1 << 6

        if option == 0:
            logger.warning("[%s] No calibration options selected", self.serial_number)
            return False

        self._sequence_id += 1

        command = {
            "print": {
                "command": "calibration",
                "sequence_id": str(self._sequence_id),
                "option": option,
            }
        }

        command_json = json.dumps(command)
        self._client.publish(self.topic_publish, command_json, qos=1)
        logger.info(
            f"[{self.serial_number}] Starting calibration: "
            f"lidar={lidar}, bed_leveling={bed_leveling}, vibration={vibration}, "
            f"motor_noise={motor_noise}, nozzle_offset={nozzle_offset}, "
            f"high_temp_heatbed={high_temp_heatbed}, clump_pos={clump_pos} (option={option})"
        )

        return True

    def disconnect(self, timeout: float = 0):
        """Disconnect from the printer."""
        if self._client:
            self._disconnection_event = threading.Event()
            self._client.disconnect()
            self._disconnection_event.wait(timeout=timeout)
            self._client.loop_stop()
            self._client = None
            self.state.connected = False

    def send_command(self, command: dict):
        """Send a command to the printer."""
        if self._client and self.state.connected:
            # Log outgoing message if logging is enabled
            if self._logging_enabled:
                self._message_log.append(
                    MQTTLogEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        topic=self.topic_publish,
                        direction="out",
                        payload=command,
                    )
                )
            # Returned so callers can pair our sequence id with paho's mid; see
            # send_gcode. Additive — every existing caller ignores it.
            return self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        return None

    def register_raw_message_handler(self, handler: Callable[[str, bytes], None]) -> None:
        """Register a handler invoked for every incoming MQTT message.

        Used by the VP MQTT bridge to republish the printer's report pushes to
        slicers connected to a virtual printer in non-proxy mode. Handlers run
        on paho's network thread and must not block; exceptions are caught.
        """
        if handler not in self._raw_message_handlers:
            self._raw_message_handlers.append(handler)

    def unregister_raw_message_handler(self, handler: Callable[[str, bytes], None]) -> None:
        """Unregister a previously-registered raw-message handler."""
        try:
            self._raw_message_handlers.remove(handler)
        except ValueError:
            pass

    def publish_raw(self, topic: str, payload: bytes | str, qos: int = 1) -> bool:
        """Publish a pre-formed payload directly to the printer's MQTT broker.

        Used by the VP MQTT bridge to forward slicer-originated commands
        without going through ``send_command``'s sequence-id mangling. Returns
        False if the underlying paho client isn't ready.
        """
        if self._client is None:
            return False
        try:
            info = self._client.publish(topic, payload, qos=qos)
            return info.rc == mqtt.MQTT_ERR_SUCCESS
        except Exception:
            logger.exception("[%s] publish_raw failed for topic=%s", self.serial_number, topic)
            return False

    def enable_logging(self, enabled: bool = True):
        """Enable or disable MQTT message logging."""
        self._logging_enabled = enabled
        # Don't clear logs when stopping - user can manually clear with clear_logs()

    def get_logs(self) -> list[MQTTLogEntry]:
        """Get all logged MQTT messages."""
        return list(self._message_log)

    def clear_logs(self):
        """Clear the message log."""
        self._message_log.clear()

    @property
    def logging_enabled(self) -> bool:
        """Check if logging is enabled."""
        return self._logging_enabled

    def send_drying_command(
        self, ams_id: int, temp: int, duration: int, mode: int = 1, filament: str = "", rotate_tray: bool = False
    ):
        """Send AMS drying start/stop command.

        Args:
            ams_id: AMS unit ID (0-3 for AMS 2 Pro, 128-135 for AMS-HT)
            temp: Target drying temperature (45-65 for AMS 2 Pro, 45-85 for AMS-HT)
            duration: Drying duration in hours
            mode: 1=start, 0=stop
            filament: Filament type string (e.g. "PLA", "PETG")
            rotate_tray: Whether to rotate the spool during drying for even heat
        """
        if not self._client:
            return False
        self._sequence_id += 1
        # A2L AMS-Lite: normalised id 6 → physical 16 on the wire (the Lite does
        # not actually support drying, but keep the translation consistent). The
        # _drying_targets dict stays keyed by the normalised id so the
        # on_drying_complete callback matches the telemetry.
        wire_ams_id = A2L_LITE_PHYSICAL_AMS_ID if ams_id == A2L_LITE_NORMALIZED_AMS_ID else ams_id
        command = {
            "print": {
                "sequence_id": str(self._sequence_id),
                "command": "ams_filament_drying",
                "ams_id": wire_ams_id,
                "temp": temp,
                "cooling_temp": 20 if mode == 1 else 0,
                "duration": duration,
                "humidity": 0,
                "mode": mode,
                "rotate_tray": rotate_tray,
                "filament": filament,
                "close_power_conflict": False,
            }
        }
        # Log the full wire JSON at INFO so support bundles capture exactly
        # what we sent — needed to diagnose silent rejections (#1447) where
        # the printer ACKs the command but never starts/stops drying.
        # Paired with the ams_filament_drying response-payload INFO log so
        # both halves of the conversation land in the bundle by default.
        wire_json = json.dumps(command)
        self._client.publish(self.topic_publish, wire_json, qos=1)
        logger.info(
            "[%s] Sent ams_filament_drying: %s",
            self.serial_number,
            wire_json,
        )
        # Cache the active-cycle target so the badge can show "PETG @ 65°C"
        # while drying — Bambu only echoes dry_time on subsequent pushes.
        if mode == 1:
            self._drying_targets[ams_id] = {"filament": filament or "", "temp": int(temp)}
        else:
            self._drying_targets.pop(ams_id, None)
        return True

    @staticmethod
    def _entry_nozzle_diameter(entry: dict, envelope: dict) -> str:
        """Which nozzle a calibration entry belongs to (#1748).

        The printer puts ``nozzle_diameter`` on the ``extrusion_cali_get``
        **envelope**. A per-filament entry carries ``setting_id``,
        ``filament_id``, ``name``, ``k_value``, ``n_coef`` and ``cali_idx`` —
        and, on every single-nozzle model, nothing else. Reading it off the
        entry and defaulting to ``"0.4"`` therefore reported *every* profile on
        a 0.6 or 0.8 mm machine as 0.4 mm, while the correct value sat unread on
        the envelope two lines away.

        That was never cosmetic. Editing a profile is delete-and-re-add on
        single-nozzle printers and the dialog rebuilds the diameter from what it
        was shown, so saving an untouched 0.6 mm profile stored it back as 0.4.
        Deletes aimed ``extrusion_cali_del`` at the wrong nozzle the same way.
        And the ``cali_idx`` cascade keys off ``KProfile.nozzle_diameter``
        (``calibration_service.py`` sync path), so on 0.6/0.8 it wrote DB rows
        under the wrong diameter and a spool's profile assignment silently
        failed to stick — the "cannot auto-map a K-profile" half of the report,
        fixed here at the source rather than at each consumer.

        It never reproduced on H2D because that firmware *does* repeat the field
        per entry; the entry is preferred for exactly that reason, and resolved
        per row — a dual-nozzle payload can carry one of each.

        ``"0.4"`` survives only as the floor for a payload that names no
        diameter anywhere: the field is not optional downstream. It is now
        reached when the printer told us nothing, instead of on every entry from
        every single-nozzle machine.
        """
        for value in (entry.get("nozzle_diameter"), envelope.get("nozzle_diameter")):
            if value not in (None, ""):
                return str(value)
        return "0.4"

    def _handle_extrusion_cali_history(self, data: dict) -> None:
        """Mirror ``extrusion_cali_get`` push into ``state.extrusion_cali_history``.

        Same payload that ``_handle_kprofile_response`` reads into the
        legacy KProfile list; this one parses into typed ``PACalibHistoryEntry``
        with float k_value / n_coef and float nozzle_diameter for the new
        Filament Calibration History modal.
        """
        filaments = data.get("filaments", [])
        if not isinstance(filaments, list):
            return
        history: list = []
        for f in filaments:
            if not isinstance(f, dict):
                continue
            try:
                history.append(
                    PACalibHistoryEntry(
                        cali_idx=int(f.get("cali_idx", -1)),
                        name=str(f.get("name", "")),
                        filament_id=str(f.get("filament_id", "")),
                        setting_id=str(f.get("setting_id", "") or ""),
                        nozzle_diameter=float(self._entry_nozzle_diameter(f, data)),
                        nozzle_volume_type=str(f.get("nozzle_volume_type", "standard") or "standard"),
                        extruder_id=int(f.get("extruder_id", 0)),
                        k_value=float(f.get("k_value", 0.0) or 0.0),
                        n_coef=float(f.get("n_coef", 0.0) or 0.0),
                    )
                )
            except (ValueError, TypeError):
                # Tolerate malformed rows; keep the rest of the history.
                pass
        self.state.extrusion_cali_history = history

    def _handle_extrusion_cali_get_result(self, data: dict) -> None:
        """Parse the X1 auto-cali result batch into ``state.extrusion_cali_results``.

        Marks ``extrusion_cali_status='completed'`` so the wizard hook can
        advance from the Running step to the Save step.
        """
        filaments = data.get("filaments", [])
        if not isinstance(filaments, list):
            return
        results: list = []
        for f in filaments:
            if not isinstance(f, dict):
                continue
            try:
                results.append(
                    ExtrusionCaliResult(
                        tray_id=int(f.get("tray_id", 0)),
                        ams_id=int(f.get("ams_id", 0)),
                        slot_id=int(f.get("slot_id", 0)),
                        extruder_id=int(f.get("extruder_id", 0)),
                        nozzle_diameter=float(self._entry_nozzle_diameter(f, data)),
                        nozzle_volume_type=str(f.get("nozzle_volume_type", "standard") or "standard"),
                        filament_id=str(f.get("filament_id", "")),
                        setting_id=str(f.get("setting_id", "") or ""),
                        k_value=float(f.get("k_value", 0.0) or 0.0),
                        n_coef=float(f.get("n_coef", 0.0) or 0.0),
                        confidence=int(f.get("confidence", -1)),
                        nozzle_pos_id=int(f.get("nozzle_pos_id", -1)),
                        nozzle_sn=str(f.get("nozzle_sn", "") or ""),
                    )
                )
            except (ValueError, TypeError):
                pass
        self.state.extrusion_cali_results = results
        self.state.extrusion_cali_status = "completed"

    @staticmethod
    def _kprofiles_digest(profiles: list[KProfile]) -> str:
        """Stable hash of the printer's K-profile list. Same set ↔ same hash,
        regardless of MQTT push duplication. Includes ``slot_id`` so the
        printer's own reorders also count as a change worth syncing to DB."""
        import hashlib

        payload = sorted(
            (
                p.slot_id,
                p.extruder_id,
                p.nozzle_id,
                p.nozzle_diameter,
                p.filament_id,
                p.name,
                p.k_value,
                p.n_coef,
                p.setting_id or "",
            )
            for p in profiles
        )
        return hashlib.md5(repr(payload).encode("utf-8")).hexdigest()

    def _maybe_notify_kprofiles_changed(self, profiles: list[KProfile]) -> None:
        """Fire ``on_kprofiles_changed`` only when the list actually differs
        from the previous push. Saves the DB-sync round-trip on the periodic
        broadcast Bambu firmware emits even when nothing changed."""
        if not self.on_kprofiles_changed:
            return
        digest = self._kprofiles_digest(profiles)
        if digest == self._last_kprofiles_hash:
            return
        self._last_kprofiles_hash = digest
        try:
            self.on_kprofiles_changed()
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] on_kprofiles_changed callback failed: %s", self.serial_number, e)

    def _handle_kprofile_response(self, data: dict):
        """Handle K-profile response from printer."""
        response_nozzle = data.get("nozzle_diameter")
        response_seq_id = str(data.get("sequence_id", "")) or None
        filaments = data.get("filaments", [])

        # Correlate by the sequence_id we sent. Falling back to the nozzle only
        # when the firmware did not echo one back keeps older firmware working
        # without giving up per-request routing on the firmware that does — and
        # the nozzle fallback is exactly what could not tell two concurrent
        # requests apart, so it must stay the *fallback*, never the primary.
        waiter_key: str | None = None
        if response_seq_id and response_seq_id in self._kprofile_waiters:
            waiter_key = response_seq_id
        elif response_nozzle is not None:
            for key, (_event, expected_nozzle, _data) in self._kprofile_waiters.items():
                if expected_nozzle == response_nozzle:
                    waiter_key = key
                    break

        has_pending_request = waiter_key is not None

        # Log all incoming responses when we have a pending request (for debugging)
        if has_pending_request:
            logger.info(
                f"[{self.serial_number}] K-profile response: nozzle={response_nozzle}, "
                f"seq_id={response_seq_id}, {len(filaments)} profiles, matched waiter={waiter_key}"
            )
        elif self._kprofile_waiters:
            # Unsolicited: the printer broadcasts 0.4mm profiles constantly, so a
            # frame that matches no waiter is not an error — just not ours.
            logger.debug(
                f"[{self.serial_number}] Ignoring unmatched K-profile frame: "
                f"nozzle={response_nozzle}, seq_id={response_seq_id}"
            )

        # If no pending request, this is just a broadcast - update state silently and return early
        if not has_pending_request:
            # Still parse profiles to keep state updated, but don't log
            profiles = []
            for f in filaments:
                if isinstance(f, dict):
                    try:
                        cali_idx = f.get("cali_idx", 0)
                        profiles.append(
                            KProfile(
                                slot_id=cali_idx,
                                extruder_id=int(f.get("extruder_id", 0)),
                                nozzle_id=str(f.get("nozzle_id", "")),
                                nozzle_diameter=self._entry_nozzle_diameter(f, data),
                                filament_id=str(f.get("filament_id", "")),
                                name=str(f.get("name", "")),
                                k_value=str(f.get("k_value", "0.000000")),
                                n_coef=str(f.get("n_coef", "0.000000")),
                                ams_id=int(f.get("ams_id", 0)),
                                tray_id=int(f.get("tray_id", -1)),
                                setting_id=f.get("setting_id"),
                            )
                        )
                    except (ValueError, TypeError):
                        pass  # Skip malformed K-profile entries; remaining profiles still usable
            self.state.kprofiles = profiles
            self._maybe_notify_kprofiles_changed(profiles)
            return

        profiles = []

        for i, f in enumerate(filaments):
            if isinstance(f, dict):
                try:
                    # cali_idx is the actual slot/calibration index from the printer
                    cali_idx = f.get("cali_idx", i)
                    profiles.append(
                        KProfile(
                            slot_id=cali_idx,
                            extruder_id=int(f.get("extruder_id", 0)),
                            nozzle_id=str(f.get("nozzle_id", "")),
                            nozzle_diameter=self._entry_nozzle_diameter(f, data),
                            filament_id=str(f.get("filament_id", "")),
                            name=str(f.get("name", "")),
                            k_value=str(f.get("k_value", "0.000000")),
                            n_coef=str(f.get("n_coef", "0.000000")),
                            ams_id=int(f.get("ams_id", 0)),
                            tray_id=int(f.get("tray_id", -1)),
                            setting_id=f.get("setting_id"),
                        )
                    )
                except (ValueError, TypeError) as e:
                    logger.warning("Failed to parse K-profile: %s", e)

        self.state.kprofiles = profiles
        self._maybe_notify_kprofiles_changed(profiles)

        # Deliver to the waiter this frame was correlated to, and only that one.
        # Captured in a local first to avoid a TOCTOU race: the asyncio thread can
        # drop the entry between the lookup and the .set() call, and MQTT
        # callbacks run on a different thread.
        entry = self._kprofile_waiters.get(waiter_key) if waiter_key else None
        if entry:
            event, expected_nozzle, _ = entry
            self._kprofile_waiters[waiter_key] = (event, expected_nozzle, profiles)
            logger.info("[%s] Got %s K-profiles for nozzle=%s", self.serial_number, len(profiles), response_nozzle)
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(event.set)
            else:
                # Fallback for when loop is not available
                event.set()

    async def get_kprofiles(
        self, nozzle_diameter: str = "0.4", timeout: float = 5.0, max_retries: int = 3
    ) -> list[KProfile]:
        """Request K-profiles from the printer with retry logic.

        Bambu printers sometimes ignore the first K-profile request, so we
        implement retry logic to ensure reliable retrieval.

        Args:
            nozzle_diameter: Filter by nozzle diameter (e.g., "0.4")
            timeout: Timeout in seconds to wait for each response attempt
            max_retries: Maximum number of retry attempts

        Returns:
            List of KProfile objects
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot get K-profiles: not connected", self.serial_number)
            return []

        # Capture current event loop for thread-safe callback
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("[%s] No running event loop", self.serial_number)
            return []

        for attempt in range(max_retries):
            # One waiter per request, keyed by the sequence_id we are about to
            # send. Registered BEFORE publishing so a fast reply cannot arrive
            # before there is anything to deliver it to.
            self._sequence_id += 1
            seq_id = str(self._sequence_id)
            self._kprofile_waiters[seq_id] = (asyncio.Event(), nozzle_diameter, None)

            # Send the command with nozzle_diameter filter
            command = {
                "print": {
                    "command": "extrusion_cali_get",
                    "filament_id": "",
                    "nozzle_diameter": nozzle_diameter,
                    "sequence_id": seq_id,
                }
            }

            logger.info(
                f"[{self.serial_number}] Requesting K-profiles for nozzle_diameter={nozzle_diameter} (attempt {attempt + 1}/{max_retries})"
            )
            logger.debug("[%s] K-profile request JSON: %s", self.serial_number, json.dumps(command))
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)

            # Wait for the reply correlated to THIS request's sequence_id.
            try:
                event = self._kprofile_waiters[seq_id][0]
                await asyncio.wait_for(event.wait(), timeout=timeout)
                profiles = self._kprofile_waiters[seq_id][2] or []
                logger.info(
                    f"[{self.serial_number}] Got {len(profiles)} K-profiles for nozzle={nozzle_diameter} on attempt {attempt + 1}"
                )
                return profiles
            except TimeoutError:
                logger.warning(
                    f"[{self.serial_number}] Timeout on K-profiles request attempt {attempt + 1}/{max_retries}"
                )
                if attempt < max_retries - 1:
                    # Brief delay before retry
                    await asyncio.sleep(0.5)
            finally:
                # Only this request's entry — a concurrent caller's waiter must
                # survive, which is the entire point of the registry.
                self._kprofile_waiters.pop(seq_id, None)

        logger.error("[%s] Failed to get K-profiles after %s attempts", self.serial_number, max_retries)
        return []

    def set_kprofile(
        self,
        filament_id: str,
        name: str,
        k_value: str,
        nozzle_diameter: str = "0.4",
        nozzle_id: str = "HS00-0.4",
        extruder_id: int = 0,
        setting_id: str | None = None,
        slot_id: int = 0,
        cali_idx: int | None = None,
    ) -> str | None:
        """Set/update a K-profile on the printer.

        Args:
            filament_id: Bambu filament identifier
            name: Profile name
            k_value: Pressure advance value (e.g., "0.020000")
            nozzle_diameter: Nozzle diameter (e.g., "0.4")
            nozzle_id: Nozzle identifier (e.g., "HS00-0.4")
            extruder_id: Extruder ID (0 or 1 for dual nozzle)
            setting_id: Existing setting ID for updates, None for new
            slot_id: Calibration index (cali_idx) for the profile
            cali_idx: For edits, the existing slot being edited (enables in-place edit)

        Returns:
            The ``sequence_id`` the command was published under — pass it to
            :meth:`await_cali_ack` to learn the printer's verdict — or ``None``
            when nothing was sent. Never the empty string: ``_sequence_id`` is
            incremented before use, so the value is always ≥ 1 and a caller can
            test it for truth.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set K-profile: not connected", self.serial_number)
            return None

        self._sequence_id += 1

        # Build the filament entry - printer uses cali_idx for profile identification
        # For new profiles (slot_id=0), use cali_idx=-1 to tell printer to create new slot
        # For edits, use the provided cali_idx or slot_id
        if cali_idx is not None:
            effective_cali_idx = cali_idx
        else:
            effective_cali_idx = -1 if slot_id == 0 else slot_id

        # Generate a setting_id for new profiles (required by printer)
        # Format: "PF" + 17 random digits
        import random

        if not setting_id and slot_id == 0:
            setting_id = f"PF{random.randint(10000000000000000, 99999999999999999)}"

        filament_entry = {
            "ams_id": 0,
            "cali_idx": effective_cali_idx,
            "extruder_id": extruder_id,
            "filament_id": filament_id,
            "k_value": k_value,
            "n_coef": "0.000000",
            "name": name,
            "nozzle_diameter": nozzle_diameter,
            "nozzle_id": nozzle_id,
            "setting_id": setting_id if setting_id else "",
            # 0, not -1. The X1C validates this field and answers
            # result:"fail" reason:"invalid tray_id" — while applying the write
            # anyway — so with -1 the acknowledgement was useless and could not
            # be gated on. The H2D ignores the value entirely. BambuStudio always
            # sends a real tray_id and defaults it to 0 for a manually entered
            # profile. Measured upstream on both printer classes: -1 fails,
            # 0 succeeds, and cali_idx:-1 is accepted either way, so this one
            # field was the whole cause.
            "tray_id": 0,
        }

        seq = str(self._sequence_id)
        command = {
            "print": {
                "command": "extrusion_cali_set",
                "filaments": [filament_entry],
                "nozzle_diameter": nozzle_diameter,
                "sequence_id": seq,
            }
        }

        command_json = json.dumps(command)
        logger.info(
            f"[{self.serial_number}] Setting K-profile: {name} = {k_value} (cali_idx={effective_cali_idx}, new={slot_id == 0})"
        )
        logger.debug("[%s] K-profile SET command: %s", self.serial_number, command_json)
        # Registered before publishing: the reply can land on the MQTT thread
        # before this call returns, and a verdict with nowhere to go is dropped.
        self._pending_cali_acks[seq] = None
        self._client.publish(self.topic_publish, command_json, qos=1)
        return seq

    def set_kprofiles_batch(
        self,
        profiles: list[dict],
        nozzle_diameter: str = "0.4",
    ) -> str | None:
        """Set multiple K-profiles in a single command (for dual-nozzle).

        Args:
            profiles: List of profile dicts, each with:
                - filament_id, name, k_value, nozzle_id, extruder_id, setting_id (optional), slot_id
            nozzle_diameter: Common nozzle diameter for all profiles

        Returns:
            The published ``sequence_id``, or ``None`` when nothing was sent.
            See :meth:`set_kprofile`.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set K-profiles batch: not connected", self.serial_number)
            return None

        import random

        self._sequence_id += 1

        filament_entries = []
        for p in profiles:
            slot_id = p.get("slot_id", 0)
            cali_idx = p.get("cali_idx")

            if cali_idx is not None:
                effective_cali_idx = cali_idx
            else:
                effective_cali_idx = -1 if slot_id == 0 else slot_id

            setting_id = p.get("setting_id")
            if not setting_id and slot_id == 0:
                setting_id = f"PF{random.randint(10000000000000000, 99999999999999999)}"

            filament_entries.append(
                {
                    "ams_id": 0,
                    "cali_idx": effective_cali_idx,
                    "extruder_id": p.get("extruder_id", 0),
                    "filament_id": p.get("filament_id", ""),
                    "k_value": p.get("k_value", "0.020000"),
                    "n_coef": "0.000000",
                    "name": p.get("name", ""),
                    "nozzle_diameter": nozzle_diameter,
                    "nozzle_id": p.get("nozzle_id", f"HS00-{nozzle_diameter}"),
                    "setting_id": setting_id if setting_id else "",
                    "tray_id": 0,  # not -1 — see set_kprofile
                }
            )

        seq = str(self._sequence_id)
        command = {
            "print": {
                "command": "extrusion_cali_set",
                "filaments": filament_entries,
                "nozzle_diameter": nozzle_diameter,
                "sequence_id": seq,
            }
        }

        command_json = json.dumps(command)
        logger.info("[%s] Setting %s K-profiles in batch", self.serial_number, len(filament_entries))
        logger.debug("[%s] K-profile SET batch command: %s", self.serial_number, command_json)
        self._pending_cali_acks[seq] = None
        self._client.publish(self.topic_publish, command_json, qos=1)
        return seq

    def delete_kprofile(
        self,
        cali_idx: int,
        filament_id: str,
        nozzle_id: str,
        nozzle_diameter: str = "0.4",
        extruder_id: int = 0,
    ) -> str | None:
        """Delete a K-profile from the printer.

        Single BS-parity ``extrusion_cali_del`` shape for every printer
        model. BambuStudio ``MachineObject::command_delete_pa_calibration``
        (DeviceManager.cpp:1905) sends one fixed payload —
        ``extruder_id`` · ``nozzle_id`` · ``filament_id`` · ``cali_idx`` ·
        ``nozzle_diameter`` — with no model branch and no ``setting_id``.
        The old code had an ``is_dual_nozzle`` serial-prefix branch and
        leaked ``setting_id`` into the non-dual branch; both were empirical
        guesses that diverged from upstream.

        (BS also appends ``nozzle_pos`` / ``nozzle_sn`` when an H2D nozzle
        rack is present — BamDude doesn't wire the nozzle-rack feature
        anywhere yet, so those are intentionally omitted.)

        Args:
            cali_idx: The calibration index (slot_id) of the profile to delete
            filament_id: Bambu filament identifier
            nozzle_id: Nozzle identifier (e.g., "HH00-0.4")
            nozzle_diameter: Nozzle diameter (e.g., "0.4")
            extruder_id: Extruder ID (0 or 1 for dual nozzle)

        Returns:
            The published ``sequence_id``, or ``None`` when nothing was sent.
            See :meth:`set_kprofile`.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot delete K-profile: not connected", self.serial_number)
            return None

        self._sequence_id += 1

        seq = str(self._sequence_id)
        command = {
            "print": {
                "command": "extrusion_cali_del",
                "sequence_id": seq,
                "extruder_id": extruder_id,
                "nozzle_id": nozzle_id,
                "filament_id": filament_id,
                "cali_idx": cali_idx,
                "nozzle_diameter": nozzle_diameter,
            }
        }

        command_json = json.dumps(command)
        logger.info(f"[{self.serial_number}] Deleting K-profile: cali_idx={cali_idx}, filament={filament_id}")
        logger.debug("[%s] K-profile DELETE command: %s", self.serial_number, command_json)
        self._pending_cali_acks[seq] = None
        # Use QoS 1 for reliable delivery (at least once)
        self._client.publish(self.topic_publish, command_json, qos=1)
        return seq

    # BS ``AIR_FUN`` (DevFan.h). Only the ids we can act on are named; the parser
    # keeps whatever the printer sends, named or not.
    AIRDUCT_PART_COOLING = 1  # FAN_COOLING_0_AIRDOOR — part cooling
    AIRDUCT_PART_AUX_0 = 2  # FAN_REMOTE_COOLING_0_IDX
    AIRDUCT_PART_CHAMBER = 3  # FAN_CHAMBER_0_IDX — "Exhaust" on P2S/X2D
    AIRDUCT_PART_AUX_1 = 10  # FAN_REMOTE_COOLING_1_IDX — the second aux kit

    @staticmethod
    def _bits(value: int, start: int, count: int) -> int:
        """BS ``MachineObject::get_flag_bits`` — ``(value >> start) & mask``."""
        return (int(value) >> start) & ((1 << count) - 1)

    def _parse_airduct_parts(self, airduct: dict) -> None:
        """Mirror ``device.airduct`` — BS ``DevFan::ParseV3_0`` parity.

        The second auxiliary fan exists **only** here. It is never mirrored into
        a flat ``big_fanX_speed`` field, which is the whole reason it was
        invisible: every consumer read the flat fields.

        Encoding, taken from BS rather than guessed:

        * ``id`` low 4 bits = type (0 fan, 1 air door), bits 4-11 = the part id.
          So the raw ``160`` is part **10**, not 160.
        * ``state`` — **low 8 bits**. The upper bits carry something else, and
          reading the whole word gives a "speed" in the thousands.
        * ``range`` — low 16 bits start, high 16 bits end. The part states its
          own allowed range, so clamping does not need a table.

        ⚠️ **Absent ``parts`` means "this frame did not say", not "no fans".**
        Bambu sends diff pushes constantly; clearing on a frame that simply
        omits the key would retract a fan kit and make the tile flicker. Same
        latching reasoning as the nozzle-flow-type flags.
        """
        if not isinstance(airduct, dict):
            return
        if "subMode" in airduct:
            _hold = self.state.printer_settings_hold.get("airduct_sub_mode")
            if _hold is None or (time.time() - _hold) >= 3.0:
                try:
                    self.state.airduct_sub_mode = int(airduct["subMode"])
                except (TypeError, ValueError):
                    pass

        modes = airduct.get("modeList")
        if isinstance(modes, list) and modes:
            parsed_modes: dict[int, dict] = {}
            for entry in modes:
                if not isinstance(entry, dict) or "modeId" not in entry:
                    continue
                try:
                    mode_id = int(entry["modeId"])
                except (TypeError, ValueError):
                    continue
                # ``ctrl`` / ``off`` carry raw ids, shifted the same way as a
                # part's — BS applies ``>> 4`` to each.
                parsed_modes[mode_id] = {
                    key: [self._bits(v, 4, 8) for v in entry.get(key, []) if isinstance(v, int)]
                    for key in ("ctrl", "off")
                }
            self.state.airduct_modes = parsed_modes

        parts = airduct.get("parts")
        if not isinstance(parts, list) or not parts:
            return
        parsed: dict[int, dict] = {}
        for part in parts:
            if not isinstance(part, dict) or "id" not in part:
                continue
            try:
                raw_id = int(part["id"])
                state = int(part.get("state", 0))
                rng = int(part.get("range", 0))
            except (TypeError, ValueError):
                continue
            parsed[self._bits(raw_id, 4, 8)] = {
                "type": self._bits(raw_id, 0, 4),
                "func": part.get("func"),
                "state": self._bits(state, 0, 8),
                "range_start": self._bits(rng, 0, 16),
                "range_end": self._bits(rng, 16, 16),
            }
        if parsed:
            self.state.airduct_parts = parsed

    def _latch_flow_type_flags(self, print_data: dict) -> None:
        """The live half of BS's nozzle-flow-type capability (#1748).

        ``is_enable_np`` — ``MachineObject::check_enable_np`` — is "the push
        carries the new-protocol quartet". ``has_extra_flow_type`` is "a nozzle
        frame also carried ``flag3``". Their OR is what
        ``is_nozzle_flow_type_supported()`` returns.

        Both latch true and are never cleared. BS re-evaluates ``is_enable_np``
        on each full parse; our pushes are frequently partial, so re-evaluating
        would let a frame carrying only ``gcode_state`` retract a capability the
        printer has, and the Flow Type field would appear and disappear as frames
        arrive. A capability is a property of the printer, not of one message.
        """
        if not self.state.enable_np and all(k in print_data for k in ("cfg", "fun", "aux", "stat")):
            self.state.enable_np = True
        if not self.state.has_extra_flow_type and all(
            k in print_data for k in ("nozzle_diameter", "nozzle_type", "flag3")
        ):
            self.state.has_extra_flow_type = True

    def _route_ack(self, print_data: dict) -> None:
        """Deliver an ``extrusion_cali_set`` / ``_del`` verdict to its waiter.

        Lifted out of ``_update_state`` so it can be reached from a test without
        building a whole status payload — the block it replaces sat several
        hundred lines into that method, which is why nothing covered it.

        Runs on the MQTT thread. Assignment into a dict is atomic under the GIL
        and ``await_cali_ack`` only reads, so no lock is needed here.
        """
        # INFO, not DEBUG: this is the printer's verdict on a write the user just
        # made, and while it sat at DEBUG the one line explaining a failed save
        # was absent from every support bundle. Same reasoning that put
        # ams_filament_drying at INFO.
        logger.info(
            "[%s] %s response: result=%s reason=%s seq=%s",
            self.serial_number,
            print_data.get("command"),
            print_data.get("result"),
            print_data.get("reason", ""),
            print_data.get("sequence_id"),
        )
        logger.debug("[%s] %s full response: %s", self.serial_number, print_data.get("command"), print_data)
        ack_seq = str(print_data.get("sequence_id", ""))
        # Only fill a slot somebody is waiting on. Writing every ack into the
        # dict would make it grow for the lifetime of the process.
        if ack_seq in self._pending_cali_acks:
            self._pending_cali_acks[ack_seq] = print_data

    async def await_cali_ack(self, sequence_id: str | None, timeout: float = 3.0) -> tuple[bool, str]:
        """Wait for the printer's verdict on a K-profile write.

        ``extrusion_cali_set`` / ``extrusion_cali_del`` are answered with
        ``result`` and, on failure, ``reason`` — echoing the ``sequence_id`` the
        write was published under, so an answer can be attributed to the write
        that caused it rather than to whichever write was most recent.

        **Silence is success.** A printer that never answers must not turn every
        save into an error: no answer is not evidence of refusal, and some
        firmware simply does not send one. Only an explicit non-success verdict
        is reported as a failure — which is why this can be gated on at all, and
        why the ``tray_id`` fix had to come first (see :meth:`set_kprofile`).

        Polls rather than waiting on an Event: the writers are synchronous and
        called from the request thread, so there is no loop-safe place to create
        one, and a 3 s ceiling at 50 ms costs at most 60 wake-ups on the slowest
        path. ``get_kprofiles`` uses an Event because it is async throughout.

        Returns ``(ok, detail)``; ``detail`` carries the printer's own reason.
        """
        if not sequence_id:
            return True, ""
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                ack = self._pending_cali_acks.get(sequence_id)
                if ack is not None:
                    result = str(ack.get("result", "")).lower()
                    if result in ("success", "ok", ""):
                        return True, ""
                    reason = str(ack.get("reason", "") or "").strip()
                    return False, reason or result
                await asyncio.sleep(0.05)
            logger.debug("[%s] No cali ack for seq=%s within %ss", self.serial_number, sequence_id, timeout)
            return True, ""
        finally:
            # Only this write's slot. Clearing the dict would drop the verdicts
            # other in-flight writes are still waiting on.
            self._pending_cali_acks.pop(sequence_id, None)

    # =========================================================================
    # Printer Control Commands
    # =========================================================================

    def pause_print(self) -> bool:
        """Pause the current print job."""
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot pause print: not connected", self.serial_number)
            return False

        command = {"print": {"command": "pause", "sequence_id": "0"}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] Sent pause print command", self.serial_number)
        return True

    def resume_print(self) -> bool:
        """Resume a paused print job."""
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot resume print: not connected", self.serial_number)
            return False

        command = {"print": {"command": "resume", "sequence_id": "0"}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] Sent resume print command", self.serial_number)
        return True

    def _clear_device_busy(self, print_error: int) -> None:
        """Take "device is busy" off a printer that is busy printing.

        Not shown, not counted, not notified — the caller skips the append, so
        this fault never enters ``state.hms_errors`` and every reader downstream
        is fed by that one list.

        ⚠️ **The log line is the investigation.** Suppressing this was held back
        precisely because the notification was the only signal by which the code
        was ever noticed (vault: "Device busy прилітає після реконекту"). It
        moves here rather than disappearing, at WARNING, with the payload
        logging beside it — a signal in a log the operator does not have to be
        woken by.

        ⚠️ Publishes directly rather than calling ``clear_hms_errors``: that one
        also empties ``state.hms_errors``, which would take a real, unrelated
        fault down with it.
        """
        now = time.time()
        if now - self._device_busy_cleared_at < _DEVICE_BUSY_CLEAR_INTERVAL:
            return
        self._device_busy_cleared_at = now

        logger.warning(
            "[%s] %s while printing (state=%s, print_error=0x%08X) — suppressed and cleared on the printer",
            self.serial_number,
            _DEVICE_BUSY_CODE,
            self.state.state,
            print_error,
        )
        if not self._client or not self.state.connected:
            return
        command = {"print": {"command": "clean_print_error", "sequence_id": "0"}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)

    def clear_hms_errors(self) -> bool:
        """Clear HMS/print errors on the printer and locally."""
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot clear HMS errors: not connected", self.serial_number)
            return False

        command = {"print": {"command": "clean_print_error", "sequence_id": "0"}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.hms_errors = []
        logger.info("[%s] Sent clear HMS errors command", self.serial_number)
        return True

    def execute_hms_action(self, print_error: str, action: str, job_id: str | None = None) -> bool:
        """Dispatch the user's choice from the HMS-error modal as a printer command.

        Args:
            print_error: Canonical hex identifier for the fault — 8 chars for the
                32-bit `print_error` path, 16 chars for the 64-bit `hms[]` path
                (HMSError.full_code). Converted to its DECIMAL string form for the
                `ignore` / `idle_ignore` commands' `err` field, which is what the
                firmware compares against the active fault (BambuStudio passes
                `std::to_string(int m_error_code)`, i.e. decimal — matching `"05008051"`
                against int 0x05008051 = 83918929 was the pre-#1869 silent-rejection).
                resume / stop keep BambuStudio's plain shape (user-confirmed working).
            action: One of HMSAction's string values.
            job_id: The `subtask_id` snapshotted onto the HMSError at parse-time.
                Required by BambuStudio's `command_hms_ignore` shape; empty string is
                the no-job-id sentinel.

        Returns False when the MQTT client is offline or when `action` is unknown so
        the route surfaces it as a 4xx rather than a silent no-op.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot execute HMS action: not connected", self.serial_number)
            return False

        # Always re-push the full state after a command so the modal's underlying
        # status query reflects the new error list (or absence) on the next tick.
        def publish(payload: dict):
            self._client.publish(self.topic_publish, json.dumps(payload), qos=1)
            self._client.publish(
                self.topic_publish, json.dumps({"pushing": {"command": "pushall", "sequence_id": "0"}}), qos=1
            )

        # BambuStudio's `err` field is the DECIMAL string of the error code's int
        # value (DeviceErrorDialog.cpp passes `std::to_string(m_error_code)` to every
        # command_hms_* call). Our route hands us the hex string — convert. Falls back
        # to the raw input if it isn't parseable so the firmware can reject it and the
        # route can surface 502 instead of us raising ValueError mid-dispatch.
        try:
            err_decimal = str(int(print_error, 16))
        except ValueError:
            err_decimal = print_error

        def hms_resume():
            # Plain resume — verified against the user's H2D/H2S to leave PAUSE cleanly
            # for "Problem Solved and Resume". BambuStudio's command_hms_resume carries
            # err/param/job_id, but the plain shape is confirmed working and changing it
            # without a field test risks regressing a path the user relies on.
            publish({"print": {"command": "resume", "param": "", "sequence_id": "0"}})

        def hms_stop():
            # Same as hms_resume — plain shape, user-confirmed working for "Stop Printing".
            publish({"print": {"command": "stop", "param": "", "sequence_id": "0"}})

        def hms_ignore_command():
            # BambuStudio's `command_hms_ignore` (DeviceManager.cpp:1450) — what the
            # "Ignore this and Resume" button actually publishes. Distinct from
            # `idle_ignore`: this command has the firmware suppress the next re-check of
            # the named fault AND resume the paused print in one operation. The previous
            # code redirected IGNORE_RESUME to a plain `resume`, which is why a wrong-plate
            # HMS came back 1-2 s later — `resume` means "re-check normally" so the
            # firmware re-detected the wrong plate and re-paused with the same code
            # (#1869). BambuStudio routes IGNORE_NO_REMINDER_NEXT_TIME /
            # DONT_REMIND_NEXT_TIME to this same command — the "don't remind" half is the
            # firmware's job.
            publish(
                {
                    "print": {
                        "command": "ignore",
                        "err": err_decimal,
                        "param": "reserve",
                        "job_id": job_id or "",
                        "sequence_id": "0",
                    }
                }
            )

        def hms_idle_ignore(persistent: bool = False):
            # `idle_ignore` is BambuStudio's "dismiss this warning without resuming"
            # command (`command_hms_idle_ignore`, DeviceManager.cpp:1424). type=0
            # dismisses once, type=1 suppresses the same warning permanently. Used by
            # NO_REMINDER_NEXT_TIME, which BambuStudio dispatches via
            # `command_hms_idle_ignore(..., 0)` — NOT the resume-bearing `ignore` command.
            publish(
                {
                    "print": {
                        "command": "idle_ignore",
                        "err": err_decimal,
                        "type": 1 if persistent else 0,
                        "sequence_id": "0",
                    }
                }
            )

        def ams_control(param: str):
            publish({"print": {"command": "ams_control", "param": param, "sequence_id": "0"}})

        def clean_print_error():
            # Matches the existing clear_hms_errors shape — Bambu does not expect
            # print_error in the body; the command clears whatever dialog is active.
            publish({"print": {"command": "clean_print_error", "sequence_id": "0"}})

        def uiop_close():
            # `err` is the 8-char hex short code, uppercased to match BambuStudio.
            publish(
                {
                    "system": {
                        "command": "uiop",
                        "name": "print_error",
                        "action": "close",
                        "source": 1,
                        "type": "dialog",
                        "err": print_error.upper(),
                        "sequence_id": "0",
                    }
                }
            )

        match action:
            case (
                HMSAction.RESUME_PRINTING
                | HMSAction.RESUME_PRINTING_DEFECTS
                | HMSAction.RESUME_PRINTING_PROBELM_SOLVED
                | HMSAction.PROBLEM_SOLVED_RESUME
                | HMSAction.FILAMENT_LOAD_RESUME
                | HMSAction.PROCEED
            ):
                hms_resume()

            case HMSAction.STOP_PRINTING:
                hms_stop()

            case HMSAction.IGNORE_RESUME | HMSAction.IGNORE_NO_REMINDER_NEXT_TIME | HMSAction.DONT_REMIND_NEXT_TIME:
                # All three map to BambuStudio's `command_hms_ignore`
                # (DeviceErrorDialog.cpp:596-602) — resume + suppress-next-check in one.
                # The "no reminder next time" half is the firmware's responsibility, so
                # the wire shape is identical for all three.
                hms_ignore_command()

            case HMSAction.NO_REMINDER_NEXT_TIME:
                # BambuStudio dispatches NO_REMINDER_NEXT_TIME via
                # `command_hms_idle_ignore` with type=0 (DeviceErrorDialog.cpp:588-590) —
                # dismisses the dialog without resuming. Distinct from the IGNORE_* group.
                hms_idle_ignore(persistent=False)

            case HMSAction.FILAMENT_EXTRUDED | HMSAction.DBL_CHECK_DONE:
                ams_control("done")

            case (
                HMSAction.RETRY_FILAMENT_EXTRUDED
                | HMSAction.CONTINUE
                | HMSAction.RETRY_PROBLEM_SOLVED
                | HMSAction.DBL_CHECK_RETRY
            ):
                ams_control("resume")

            case HMSAction.ABORT:
                ams_control("abort")

            case HMSAction.OK_BUTTON:
                clean_print_error()

            case HMSAction.DBL_CHECK_OK:
                clean_print_error()
                uiop_close()

            case HMSAction.DBL_CHECK_RESUME:
                # Plain resume — not HMS-aware, no err/job_id.
                publish({"print": {"command": "resume", "param": "", "sequence_id": "0"}})

            case HMSAction.REFRESH_NOZZLE:
                publish({"print": {"command": "refresh_nozzle", "sequence_id": "0"}})

            case HMSAction.TURN_OFF_FIRE_ALARM:
                publish({"print": {"command": "buzzer_ctrl", "mode": 0, "sequence_id": "0"}})

            case HMSAction.STOP_DRYING:
                publish({"print": {"command": "auto_stop_ams_dry", "sequence_id": "0"}})

            case HMSAction.DISABLE_PURIFICATION:
                publish({"print": {"command": "close_air_filt", "sequence_id": "0"}})

            case _ if action in HMS_UI_ONLY_ACTIONS:
                # UI-only actions — the printer's own screen handles these; the modal
                # still surfaces them so the user has parity with Studio.
                #
                # ⚠️ Nothing is published here, which the CALLER has to know:
                # ``/hms/action`` proves a command landed by waiting for the
                # pushall every published command provokes. With no publish there
                # is no pushall, so an idle printer answered nothing and the route
                # returned **502 "printer did not acknowledge"** for an action that
                # was never meant to reach it — and when a status happened to
                # arrive for unrelated reasons, it reported "Action sent to
                # printer", which is equally untrue. See ``HMS_UI_ONLY_ACTIONS``.
                pass

            case _:
                logger.warning("[%s] Unknown HMS action '%s'", self.serial_number, action)
                return False

        logger.info("[%s] Executed HMS action '%s' (err=%s)", self.serial_number, action, print_error)
        return True

    def skip_objects(self, object_ids: list[int]) -> bool:
        """Skip specific objects during a print.

        This command tells the printer to skip printing the specified objects.
        The object IDs come from the slice_info.config file in the 3MF.

        Args:
            object_ids: List of identify_id values from slice_info.config

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot skip objects: not connected", self.serial_number)
            return False

        if self.state.state != "RUNNING" and self.state.state != "PAUSE":
            logger.warning(
                f"[{self.serial_number}] Cannot skip objects: printer not printing (state={self.state.state})"
            )
            return False

        if not object_ids:
            logger.warning("[%s] Cannot skip objects: no object IDs provided", self.serial_number)
            return False

        # Validate all IDs are integers
        try:
            obj_list = [int(oid) for oid in object_ids]
        except (ValueError, TypeError) as e:
            logger.warning("[%s] Invalid object IDs: %s", self.serial_number, e)
            return False

        self._sequence_id += 1
        command = {"print": {"sequence_id": str(self._sequence_id), "command": "skip_objects", "obj_list": obj_list}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] Sent skip_objects command: %s", self.serial_number, obj_list)

        # Deliberately NOT recorded into state here. ``skipped_objects`` is what
        # the printer says it is skipping, and the only writer is the ``s_obj``
        # branch — BS holds the same line (``m_partskip_ids`` is filled from
        # ``s_obj`` and from nothing else).
        #
        # Writing our own request in first made the state say "skipped" the
        # instant we asked. Firmware can decline: the object may already be
        # finished, the print may have ended between the click and the publish,
        # or the plate may carry no object labels at all. A declined skip then
        # showed as done, and it stayed that way — the echo that would correct
        # it is a *diff* against what we hold, and we had already written the
        # wrong answer into the thing it diffs against.
        #
        # The callback still fires, from that same branch, when the printer
        # confirms. Later than before, and true.
        return True

    def _notify_skipped_objects_changed(self) -> None:
        """Hand the current skipped-object list to the callback, if one is set.

        Never let a consumer's failure reach the MQTT parse loop: this runs on
        the paho network thread, where an exception would take the callback
        chain down mid-status and leave the rest of the payload unparsed.
        """
        if not self.on_skipped_objects_changed:
            return
        try:
            self.on_skipped_objects_changed(list(self.state.skipped_objects))
        except Exception as e:  # noqa: BLE001 — see the docstring
            logger.warning("[%s] on_skipped_objects_changed callback failed: %s", self.serial_number, e)

    def send_gcode(self, gcode: str) -> bool:
        """Send G-code command(s) to the printer.

        Multiple commands can be separated by newlines.

        Args:
            gcode: G-code command(s) to send

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot send G-code: not connected", self.serial_number)
            return False

        self._sequence_id += 1
        command = {"print": {"command": "gcode_line", "param": gcode, "sequence_id": str(self._sequence_id)}}
        info = self.send_command(command)
        # Remember which paho packet carries this sequence, so the printer's own
        # acknowledgement can retire it from the retry queue. Bounded: one entry
        # per in-flight command, removed on its ACK and cleared on connect.
        mid = getattr(info, "mid", None)
        if mid is not None:
            self._mid_by_sequence[str(self._sequence_id)] = mid
        logger.debug("[%s] Sent G-code (seq=%d): %s...", self.serial_number, self._sequence_id, gcode[:50])
        return True

    def register_ack_listener(self, seq_id: str, event: threading.Event, result: dict):
        """Register a one-shot ACK listener for a gcode_line command.

        When the printer responds with result for this sequence_id,
        result["success"] and result["reason"] are set and event is signaled.
        """
        self._ack_listeners[seq_id] = (event, result)

    def temperature_limits(self) -> dict[str, tuple[int, int]]:
        """What this machine's three heaters will accept.

        One answer serving the clamp below and the status snapshot the UI bounds
        its inputs with — two readings of the same rule is how they drift apart,
        and the one that drifts is always the one nobody is looking at.
        """
        from backend.app.utils.temperature_limits import limits_for

        return limits_for(self.model, self.state)

    def set_bed_temperature(self, target: int) -> bool:
        """Set the bed target temperature.

        ⚠️ Which command carries it is not ours to choose — BS's
        ``command_set_bed`` branches on ``m_support_mqtt_bet_ctrl`` (``fun``
        bit 39): a JSON ``set_bed_temp`` where the machine offers it, ``M140``
        where it does not. We sent ``M140`` unconditionally, which is the legacy
        half of a two-way split.

        Args:
            target: Target temperature in Celsius (0 to turn off)

        Returns:
            True if command was sent, False otherwise
        """
        from backend.app.utils.temperature_limits import clamp_target

        target = clamp_target(int(target), self.temperature_limits()["bed"])

        if not self.state.print_option_support.get("mqtt_bed_ctrl"):
            return self.send_gcode(f"M140 S{target}")

        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set bed temperature: not connected", self.serial_number)
            return False

        self._sequence_id += 1
        command = {
            "print": {
                "command": "set_bed_temp",
                "temp": target,
                "sequence_id": str(self._sequence_id),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        return True

    def set_nozzle_temperature(self, target: int, extruder_index: int = 0) -> bool:
        """Set a nozzle's target temperature.

        ⚠️ Two commands again, and the split is by nozzle COUNT, not by the
        printer's age: BS sends the legacy ``M104`` only while the machine has a
        single extruder (``TEMP_OF_NORMAL_TYPE``), and ``set_nozzle_temp`` with
        an explicit ``extruder_index`` as soon as there are two — the deputy
        nozzle has no ``M104`` form at all, since the g-code cannot name which
        one it means.

        Args:
            target: Target temperature in Celsius (0 to turn off)
            extruder_index: 0 = main, 1 = deputy. Ignored on single-nozzle
                machines, which have only one thing it could mean.

        Returns:
            True if command was sent, False otherwise
        """
        from backend.app.utils.temperature_limits import clamp_target

        target = clamp_target(int(target), self.temperature_limits()["nozzle"])

        # BS asks ``GetTotalExtderCount()``, which is the live report. We keep
        # the model as a second opinion because ours starts False and only turns
        # true once ``device.extruder.info`` has arrived — and a dual-nozzle
        # machine that has not sent it yet would otherwise take the ``M104``
        # path, where "which nozzle" cannot be said at all.
        from backend.app.utils.printer_models import is_dual_nozzle_model

        if extruder_index == 0 and not (self._is_dual_nozzle or is_dual_nozzle_model(self.model)):
            return self.send_gcode(f"M104 S{target}")

        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set nozzle temperature: not connected", self.serial_number)
            return False

        self._sequence_id += 1
        command = {
            "print": {
                "command": "set_nozzle_temp",
                "extruder_index": int(extruder_index),
                "target_temp": target,
                "sequence_id": str(self._sequence_id),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        return True

    def set_chamber_temperature(self, target: int) -> bool:
        """Set the chamber target temperature.

        Args:
            target: Target temperature in Celsius (0 to turn off heating)

        Returns:
            True if command was sent, False otherwise
        """
        # BS ``DevChamber::CtrlSetChamberTemp`` — a JSON command, not g-code:
        #     {"print": {"command": "set_ctt", "ctt_val": <int>, "sequence_id": …}}
        # gated on ``SupportChamberEdit()``, i.e. the same models our
        # ``supports_chamber_heater`` now answers from ``support_chamber_temp_edit``.
        #
        # We sent ``M141 S<n>`` over ``gcode_line``. ⚠️ Whether that ever worked
        # is UNVERIFIED in both directions — we have no chamber-heated machine
        # here, and the often-quoted evidence (BS gating M141 on
        # ``!is_BBL_Printer()``) is about the g-code the SLICER generates for a
        # print, not about a live command, which is a different context. What is
        # certain is that ``set_ctt`` is what BS sends live, so that is what we
        # send. Two commands for one setpoint would be a second source of truth.
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set chamber temperature: not connected", self.serial_number)
            return False

        from backend.app.utils.temperature_limits import clamp_target

        target = clamp_target(int(target), self.temperature_limits()["chamber"])

        self._sequence_id += 1
        command = {
            "print": {
                "command": "set_ctt",
                "ctt_val": int(target),
                "sequence_id": str(self._sequence_id),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        result = True
        # Track chamber target locally (MQTT reports encoded values that need filtering)
        if result:
            self.state.temperatures["chamber_target"] = float(target)
            self.state.temperatures["_chamber_target_set_time"] = time.time()
            # Update heating state immediately based on new target
            current_temp = self.state.temperatures.get("chamber", 0)
            self.state.temperatures["chamber_heating"] = target > 0 and current_temp < target
            logger.info(
                f"[{self.serial_number}] Tracking chamber target locally: {target}°C (heating={self.state.temperatures['chamber_heating']})"
            )
        return result

    def set_print_speed(self, mode: int) -> bool:
        """Set the print speed mode.

        Args:
            mode: Speed mode (1=silent, 2=standard, 3=sport, 4=ludicrous)

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set print speed: not connected", self.serial_number)
            return False

        if mode not in (1, 2, 3, 4):
            logger.warning("[%s] Invalid speed mode: %s", self.serial_number, mode)
            return False

        command = {"print": {"command": "print_speed", "param": str(mode), "sequence_id": "0"}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] Set print speed mode to %s", self.serial_number, mode)
        return True

    def _percent_from_gear(self, value) -> int | None:
        """A named fan field as a percentage, the way BS reads it.

        ``DevFan::ParseV1_0``: ``round(floor(v / 1.5) * 25.5)`` maps the raw
        0-15 field onto 0-255 — which is eleven distinct steps, not sixteen.
        Divided back down, the percentage is simply ``floor(v / 1.5) * 10``.

        ⚠️ Our old linear ``v * 100 / 15`` disagreed on **ten of the sixteen**
        raw values, and one disagreement mattered: raw ``1`` is **0 %** in BS —
        the fan is off — and was shown as 7 %, i.e. running.

        ⚠️ A value above 15 is not silently re-scaled. The previous code treated
        anything up to 255 as already-a-percentage, which is what made ``10``
        ambiguous. BS has no such branch; if hardware really sends a wider range
        here we want to find out from the log rather than by a wrong reading.
        """
        if value is None:
            return None
        try:
            raw = int(value)
        except (TypeError, ValueError):
            return None
        if raw > 15:
            if not getattr(self, "_wide_fan_field_logged", False):
                logger.warning(
                    "[%s] fan field out of the 0-15 range BS expects: %s — clamping", self.serial_number, raw
                )
                self._wide_fan_field_logged = True
            return 100
        return int(max(0, raw) / 1.5) * 10

    def set_airduct_mode(self, mode: str | int, submode: int = -1) -> bool:
        """Set the air-duct mode, and optionally its sub-mode.

        BS ``DevFan::command_control_air_duct``::

            {"print": {"command": "set_airduct", "modeId": <id>, "submode": <n>}}

        ``mode`` accepts the numeric BS id, which is what the printer reports in
        its own ``modeList`` and therefore the only thing worth sending. The
        legacy ``"cooling"`` / ``"heating"`` strings are still understood because
        ``services/preheat.py`` speaks in those terms — it chooses a mode from
        the filament, not from a list the user picked.

        ``submode`` is the "Filter" toggle and belongs to the cooling mode alone
        (BS ``UpdatePartSubMode``): ``1`` on, ``0`` off, ``-1`` unchanged. It
        redirects one fan to filtering, which costs cooling — hence its own
        warning in BS, and hence the caller decides, not this method.

        This publishes; it does not judge. Which modes exist, whether a print is
        running and whether the sub-mode applies are the route's questions —
        the same split as :meth:`set_fan_speed`.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set airduct mode: not connected", self.serial_number)
            return False

        if isinstance(mode, str):
            mode_id = AIRDUCT_COOLING_FILT if mode == "cooling" else AIRDUCT_HEATING_INTERNAL_FILT
        else:
            mode_id = int(mode)

        self._sequence_id += 1
        command = {
            "print": {
                "command": "set_airduct",
                "modeId": mode_id,
                "submode": int(submode),
                "sequence_id": str(self._sequence_id),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)

        # Show the new mode at once, and hold off the push already in flight —
        # it still carries the OLD one, and letting it land would snap the
        # selection back a moment after the click. Same three-second contract as
        # every other setting here (``printer_settings_hold``).
        #
        # ⚠️ This is an optimistic write, which elsewhere in this file is a
        # defect — but only where nothing could ever refute it. Here the hold
        # EXPIRES and the printer's own ``modeCur`` wins from then on, so a
        # refused command corrects itself within seconds instead of standing
        # forever. The bounded window is the whole difference.
        self.state.airduct_mode = mode_id
        self.state.printer_settings_hold["airduct_mode"] = time.time()
        if submode != -1:
            self.state.airduct_sub_mode = int(submode)
            self.state.printer_settings_hold["airduct_sub_mode"] = time.time()

        logger.info(
            "[%s] set_airduct modeId=%s submode=%s seq=%s", self.serial_number, mode_id, submode, self._sequence_id
        )
        return True

    def set_fan_speed(self, part_id: int, percent: int) -> bool:
        """Set one airduct fan's speed, 0-100 %.

        **Which wire command depends on the printer, and BS decides it the same
        way** (``Widgets/FanControl.cpp::FanControlNew::command_control_fan``):

            if not is_enable_np or not supports airduct:  M106 P<id> S<0-255>
            else:                                         {"command": "set_fan",
                                                           "fan_index", "speed"}

        So a P2S or X2D — new protocol, airduct present — is driven with
        ``set_fan``, not with ``M106``. Upstream ports ``M106 P10`` here, taken
        from Bambu's machine *profile* gcode; that is what runs inside a print,
        not how the slicer's own control panel drives the fan live. We follow the
        live path, because that is the one this control is.

        ``is_enable_np`` is the flag the K-profile flow-type gate already latches
        — the same "new protocol" question, so there is one answer to it.

        The two protocols disagree about the scale as well: ``M106`` takes
        0-255, ``set_fan`` takes 0-100. Passing a percentage into the wrong one
        would be a fan at 40 % of the speed asked for, silently.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set fan speed: not connected", self.serial_number)
            return False

        if not airduct_fan_controllable(self.state, part_id):
            logger.warning(
                "[%s] Fan %s is forced off by airduct mode %s — command would be ignored",
                self.serial_number,
                part_id,
                self.state.airduct_mode,
            )
            return False

        part = airduct_parts_effective(self.state, self.model).get(part_id)
        # Clamp to the range the part declares, when it declares one — the part
        # states its own limits, so this needs no per-model table.
        low = int(part.get("range_start", 0)) if part else 0
        high = int(part.get("range_end", 100)) if part else 100
        if high <= low:
            low, high = 0, 100
        percent = max(low, min(high, int(percent)))

        self._sequence_id += 1
        if self.state.enable_np and self.state.airduct_parts:
            command = {
                "print": {
                    "command": "set_fan",
                    "fan_index": int(part_id),
                    "speed": percent,
                    "sequence_id": str(self._sequence_id),
                }
            }
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)
            logger.info("[%s] set_fan part=%s speed=%s%%", self.serial_number, part_id, percent)
            return True

        # Old protocol: M106 takes 0-255, and BS **floors** rather than rounds —
        # ``floor(gear * 25.5)``, a gear being ten percent
        # (``FanControlNew::command_control_fan``). Rounding disagreed with it by
        # one unit on gears 1, 5 and 9.
        #
        # ⚠️ Integer arithmetic, and not ``int(percent * 2.55)``: 2.55 has no
        # exact binary form, so that evaluates 100 % to 254.999… and floors it to
        # **254** — wrong at precisely the value people reach for most. BS gets
        # away with a float because 25.5 *is* exactly representable (51/2).
        # ``percent * 51 // 20`` is the same number with no such trap, and it
        # matches BS on every percentage from 0 to 100, not only on gears.
        gcode = f"M106 P{int(part_id)} S{percent * 51 // 20}\n"
        command = {
            "print": {
                "command": "gcode_line",
                "param": gcode,
                "sequence_id": str(self._sequence_id),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] M106 P%s at %s%%", self.serial_number, part_id, percent)
        return True

    def set_chamber_light(self, on: bool) -> bool:
        """Turn chamber light on or off.

        Args:
            on: True to turn on, False to turn off

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set chamber light: not connected", self.serial_number)
            return False

        mode = "on" if on else "off"
        # Control both chamber lights (some printers like H2D have two)
        for led_node in ["chamber_light", "chamber_light2"]:
            self._sequence_id += 1
            command = {
                "system": {
                    "command": "ledctrl",
                    "led_node": led_node,
                    "led_mode": mode,
                    "led_on_time": 500,
                    "led_off_time": 500,
                    "loop_times": 0,
                    "interval_time": 0,
                    "sequence_id": str(self._sequence_id),
                }
            }
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] Set chamber lights %s (seq=%s)", self.serial_number, "on" if on else "off", self._sequence_id)
        return True

    def select_extruder(self, extruder: int) -> bool:
        """Select the active extruder for dual-nozzle printers (H2D).

        Args:
            extruder: Extruder index (0=right, 1=left for H2D)

        Returns:
            True if command was sent, False otherwise
        """
        if extruder not in (0, 1):
            logger.warning("[%s] Invalid extruder: %s", self.serial_number, extruder)
            return False

        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot switch extruder: not connected", self.serial_number)
            return False

        # H2D extruder switching via select_extruder command
        # Command format captured from OrcaSlicer:
        # {"print": {"command": "select_extruder", "extruder_index": 0, "sequence_id": "..."}}
        # extruder_index: 0 = RIGHT, 1 = LEFT
        self._sequence_id += 1
        command = {
            "print": {"command": "select_extruder", "extruder_index": extruder, "sequence_id": str(self._sequence_id)}
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info(
            "[%s] Sent select_extruder command: extruder_index=%s (0=right, 1=left)", self.serial_number, extruder
        )
        return True

    def home_axes(self, axes: str = "XYZ") -> bool:
        """Run the printer's full auto-home sequence.

        The ``axes`` argument is ignored: a bare ``G28`` is always sent so
        Bambu firmware runs its safe multi-step routine (park toolhead →
        home XY → home Z). Partial-axis variants like ``G28 Z`` skip the
        toolhead-park step and can crash the bed into the toolhead on H2C
        / H2D / H2S / X1 where Z-home moves the bed UP — upstream #1052.

        Machines that offer it get BS's ``back_to_center`` instead (``fun``
        bit 32) — the firmware's own homing routine, which is the same safe
        sequence asked for by name rather than by g-code.

        ⚠️ BS's g-code fallback is ``G28 X`` *while printing* and a bare ``G28``
        otherwise. We never send the partial form, and the divergence is safe
        only because ``/home-axes`` refuses outright during a print. If that
        guard is ever lifted, this becomes a live difference.
        """
        if self.state.print_option_support.get("mqtt_homing"):
            if not self._client or not self.state.connected:
                logger.warning("[%s] Cannot home: not connected", self.serial_number)
                return False
            self._sequence_id += 1
            command = {"print": {"command": "back_to_center", "sequence_id": str(self._sequence_id)}}
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)
            return True

        return self.send_gcode("G28")

    def move_axis(self, axis: str, distance: float, speed: int | None = None) -> bool:
        """Jog one axis by a relative distance — BS ``DevAxis::Ctrl_Axis``.

        ``axis`` is "X", "Y", "Z" or "E"; ``distance`` is signed millimetres.
        On Z the sign follows BS's own convention, where **negative closes the
        nozzle-bed gap** ("up" in the UI). On E, negative retracts.

        ⚠️ **Y and Z are inverted on non-CoreXY machines, X and E are not.**
        On a bed-slinger the Z axis carries the toolhead rather than the bed, so
        the same command means the opposite motion — the crash in upstream
        #1334. BS applies the flip to Y as well, which only becomes visible once
        Y is controllable at all.

        ⚠️ **The MQTT path cannot carry a distance.** ``xyz_ctrl`` has room for a
        direction and a coarse/fine ``mode`` (BS: ``mode = abs(value) >= 10``),
        and nothing else — so on a machine that speaks it, 3 mm and 9 mm are the
        same request, as are 10 mm and 200 mm. That is BS's protocol, not a
        simplification made here, and it is why callers must not promise a
        precise distance without checking which path this returns on.
        """
        axis = axis.upper()
        if axis not in ("X", "Y", "Z", "E"):
            logger.warning("[%s] Refusing to move unknown axis %r", self.serial_number, axis)
            return False
        if not distance:
            return False

        from backend.app.utils.printer_configs import is_bed_slinger

        # BS: ``if (!IsArchCoreXY()) { if (axis == "Y" || axis == "Z") value = -value; }``
        if is_bed_slinger(self.model) and axis in ("Y", "Z"):
            distance = -distance

        if self.state.print_option_support.get("mqtt_axis_ctrl"):
            if not self._client or not self.state.connected:
                logger.warning("[%s] Cannot move axis: not connected", self.serial_number)
                return False
            self._sequence_id += 1
            command = {
                "print": {
                    "command": "xyz_ctrl",
                    "axis": axis,
                    "dir": 1 if distance > 0 else -1,
                    "mode": 1 if abs(distance) >= 10 else 0,
                    "sequence_id": str(self._sequence_id),
                }
            }
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)
            return True

        if axis == "E":
            # ⚠️ No endstop or ref-mode wrapper here, and that is BS's shape, not
            # an omission: the extruder has no soft endstops to push, and no
            # reference frame a jog could disturb.
            speed = AXIS_SPEED_E if speed is None else speed
            return self.send_gcode(f"M83\nG0 E{distance:.1f} F{speed}")

        speed = (AXIS_SPEED_XY if axis in ("X", "Y") else AXIS_SPEED_Z) if speed is None else speed
        return self.send_gcode(
            "\n".join(
                [
                    "M211 S",
                    "M211 X1 Y1 Z1",
                    "M1002 push_ref_mode",
                    "G91",
                    f"G1 {axis}{distance:.1f} F{speed}",
                    "M1002 pop_ref_mode",
                    "M211 R",
                ]
            )
        )

    def extruder_control(self, length: float, extruder_index: int = 0) -> bool:
        """Push or pull filament by hand — BS ``command_extruder_control``.

        ``length`` is signed millimetres; negative retracts, which is what BS's
        "up" arrow sends.

        ⚠️ Machines on the new protocol get ``set_extrusion_length``, which names
        the extruder. The g-code fallback cannot: ``G0 E`` acts on whichever
        extruder is active, so on a dual-nozzle H2D it is unable to address the
        second one at all — the same gap the nozzle temperature had.

        ⚠️ **The caller owns the temperature check.** BS refuses below 170 °C
        (``TEMP_THRESHOLD_ALLOW_E_CTRL``) and it is not decoration: cold
        extrusion grinds a flat onto the filament and packs the gear teeth.
        """
        if self.state.enable_np:
            if not self._client or not self.state.connected:
                logger.warning("[%s] Cannot control extruder: not connected", self.serial_number)
                return False
            self._sequence_id += 1
            command = {
                "print": {
                    "command": "set_extrusion_length",
                    "extruder_index": int(extruder_index),
                    # BS casts to int — the protocol carries whole millimetres.
                    "length": int(length),
                    "sequence_id": str(self._sequence_id),
                }
            }
            self._client.publish(self.topic_publish, json.dumps(command), qos=1)
            return True

        return self.move_axis("E", length)

    def _camera_command(self, command: str, **fields) -> bool:
        """Publish on the ``camera`` envelope — a third namespace beside ``print``
        and ``system``, which is why it does not go through the usual helper."""
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot send %s: not connected", self.serial_number, command)
            return False
        self._sequence_id += 1
        payload = {"camera": {"command": command, "sequence_id": str(self._sequence_id), **fields}}
        self._client.publish(self.topic_publish, json.dumps(payload), qos=1)
        return True

    def check_timelapse_storage(self, storage: str, total_layer: int) -> bool:
        """Ask whether there is room for a timelapse of this many layers.

        BS ``command_ipcam_check_timelapse_storage``. ⚠️ The answer arrives in
        the push as ``device.cam.tl_*_free_kb`` rather than as a reply to this
        message, so a caller wanting a fresh number asks and then reads state —
        the command is a nudge, not a query with a return value.
        """
        return self._camera_command(
            "ipcam_get_media_info",
            sub_command="is_timelapse_storage_enough",
            storage=storage,
            total_layer=int(total_layer),
        )

    def delete_oldest_timelapse(self, storage: str, total_layer: int) -> bool:
        """Free space by dropping the oldest recording — BS
        ``command_ipcam_delete_oldest_timelapse``, the "Confirm & Print" branch
        of its low-storage dialog."""
        return self._camera_command(
            "ipcam_delete_oldest_timelapse",
            storage=storage,
            total_layer=int(total_layer),
        )

    def disable_steppers(self) -> bool:
        """Release the motors so the toolhead can be pushed by hand (``M84``).

        BS has no MQTT command for this and publishes the g-code, so there is
        only one path.
        """
        return self.send_gcode("M84")

    def ams_load_filament(self, tray_id: int, extruder_id: int | None = None) -> bool:
        """Load filament from a specific AMS tray.

        Args:
            tray_id: Global tray ID — 0..15 for AMS slots, 254 for external
                spool (single-external printers and Ext-L on dual-nozzle H2D),
                255 for Ext-R on dual-nozzle H2D.
            extruder_id: Unused - kept for API compatibility

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot load filament: not connected", self.serial_number)
            return False

        # Build the ams_change_filament command. Encoding differs by target type:
        #   - AMS slots (0..15): slot_id is the local slot, curr/tar_temp = -1.
        #   - External spool (tray_id=254): legacy capture from a single-extruder
        #     printer used slot_id=254, curr/tar_temp=-1; preserved here.
        #   - Ext-R on dual-nozzle H2D (tray_id=255): captured shape from
        #     BambuStudio uses slot_id=0 (extruder index, 0=right), and
        #     curr_temp/tar_temp = the actual right-nozzle temp. See upstream
        #     #891 for the BambuStudio traffic capture rationale.
        self._sequence_id += 1
        wire_target = tray_id
        if tray_id == 255:
            ams_id = 255
            slot_id = 0  # extruder index for the right nozzle
            right_temp = int(self.state.temperatures.get("nozzle_2", 0) or 0)
            if right_temp < 180:
                right_temp = 215  # Reasonable default if right nozzle is cold/unknown
            curr_temp = right_temp
            tar_temp = right_temp
        elif tray_id == 254:
            ams_id = 255  # External spool
            slot_id = 254
            curr_temp = -1
            tar_temp = -1
        elif (_a2l := a2l_lite_wire_ids(tray_id // 4, tray_id)) is not None:
            # A2L AMS-Lite: physical unit 16 + local slot confirmed; the wire
            # ``target`` (physical global 64-67) is extrapolated (no A2L load
            # capture yet). See a2l_lite_wire_ids.
            ams_id, slot_id, wire_target = _a2l
            curr_temp = -1
            tar_temp = -1
        else:
            ams_id = tray_id // 4  # AMS unit (0, 1, 2, 3...)
            slot_id = tray_id % 4  # Slot within AMS (0, 1, 2, 3)
            curr_temp = -1
            tar_temp = -1

        command = {
            "print": {
                "command": "ams_change_filament",
                "sequence_id": str(self._sequence_id),
                "ams_id": ams_id,
                "slot_id": slot_id,
                "target": wire_target,
                "curr_temp": curr_temp,
                "tar_temp": tar_temp,
            }
        }

        command_json = json.dumps(command)
        logger.info("[%s] Publishing ams_change_filament command: %s", self.serial_number, command_json)
        self._client.publish(self.topic_publish, command_json, qos=1)
        logger.info("[%s] Loading filament from tray %s (AMS %s slot %s)", self.serial_number, tray_id, ams_id, slot_id)

        # Track this load request for H2D dual-nozzle disambiguation
        # H2D reports only slot number (0-3) in tray_now, so we use our tracked value
        self._last_load_tray_id = tray_id
        self.state.pending_tray_target = tray_id
        logger.info("[%s] Set pending_tray_target=%s for H2D disambiguation", self.serial_number, tray_id)

        return True

    def ams_unload_filament(self) -> bool:
        """Unload the currently loaded filament.

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot unload filament: not connected", self.serial_number)
            return False

        # Get the currently loaded tray info
        tray_now = self.state.tray_now
        logger.info("[%s] Unload requested, tray_now=%s", self.serial_number, tray_now)

        # Determine source ams_id for the unload command
        if tray_now == 255 or tray_now == 254:
            ams_id = 255  # No filament or external spool
        elif (_a2l := a2l_lite_wire_ids(tray_now // 4, tray_now)) is not None:
            ams_id = _a2l[0]  # A2L AMS-Lite: normalised 6 → physical 16
        else:
            ams_id = tray_now // 4  # Source AMS

        # Command format from BambuStudio traffic capture:
        # - No extruder_id field
        # - For UNLOAD: curr_temp and tar_temp are the actual nozzle temp (e.g., 210)
        # - slot_id=255 and target=255 for unload
        # Get current nozzle temperature for the unload command
        nozzle_temp = int(self.state.temperatures.get("nozzle", 210))
        if nozzle_temp < 180:
            nozzle_temp = 210  # Default to PLA temp if nozzle is cold

        self._sequence_id += 1
        command = {
            "print": {
                "command": "ams_change_filament",
                "sequence_id": str(self._sequence_id),
                "ams_id": ams_id,
                "slot_id": 255,  # 255 = unload marker
                "target": 255,  # 255 = unload destination
                "curr_temp": nozzle_temp,
                "tar_temp": nozzle_temp,
            }
        }

        command_json = json.dumps(command)
        logger.info("[%s] Publishing ams_change_filament (unload) command: %s", self.serial_number, command_json)
        self._client.publish(self.topic_publish, command_json, qos=1)
        logger.info("[%s] Unloading filament (tray_now was %s)", self.serial_number, tray_now)

        # Clear tracked load request since we're unloading
        self._last_load_tray_id = None
        self.state.pending_tray_target = None
        logger.info("[%s] Cleared pending_tray_target (unload)", self.serial_number)

        return True

    def ams_control(self, action: str) -> bool:
        """Control AMS operations.

        Args:
            action: "resume", "reset", or "pause"

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot control AMS: not connected", self.serial_number)
            return False

        if action not in ("resume", "reset", "pause"):
            logger.warning("[%s] Invalid AMS action: %s", self.serial_number, action)
            return False

        command = {"print": {"command": "ams_control", "param": action, "sequence_id": "0"}}
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] AMS control: %s", self.serial_number, action)
        return True

    def ams_refresh_tray(self, ams_id: int, tray_id: int) -> tuple[bool, str]:
        """Trigger RFID re-read for a specific AMS tray.

        Args:
            ams_id: AMS unit ID (0-3, or 128 for H2D external tray)
            tray_id: Tray ID within the AMS (0-3)

        Returns:
            Tuple of (success, message)
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot refresh AMS tray: not connected", self.serial_number)
            return False, "Printer not connected"

        # Check if filament is currently loaded (tray_now != 255)
        # RFID refresh requires the AMS to move filament, which can't happen if one is loaded
        tray_now = self.state.tray_now
        if tray_now != 255:
            # Decode which tray is loaded for the message
            if tray_now == 254:
                loaded_tray = "external spool"
            elif tray_now >= 0 and tray_now < 128:
                loaded_ams = tray_now // 4
                loaded_slot = tray_now % 4
                loaded_tray = f"AMS {loaded_ams + 1} slot {loaded_slot + 1}"
            else:
                loaded_tray = f"tray {tray_now}"
            logger.warning("[%s] Cannot refresh AMS tray: filament loaded from %s", self.serial_number, loaded_tray)
            return False, f"Please unload filament first. Currently loaded: {loaded_tray}"

        # A2L AMS-Lite: physical unit 16 + local slot (matches ams_mapping2).
        wire_ams_id, wire_slot_id = ams_id, tray_id
        if (_a2l := a2l_lite_wire_ids(ams_id, tray_id)) is not None:
            wire_ams_id, wire_slot_id, _ = _a2l

        # Use ams_get_rfid command to trigger RFID re-read
        # This command is used by Bambu Studio to re-read the RFID tag
        command = {
            "print": {"command": "ams_get_rfid", "ams_id": wire_ams_id, "slot_id": wire_slot_id, "sequence_id": "0"}
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] Triggering RFID re-read: AMS %s, slot %s", self.serial_number, ams_id, tray_id)

        return True, f"Refreshing AMS {ams_id} tray {tray_id}"

    def ams_set_filament_setting(
        self,
        ams_id: int,
        tray_id: int,
        tray_info_idx: str,
        tray_type: str,
        tray_sub_brands: str,
        tray_color: str,
        nozzle_temp_min: int,
        nozzle_temp_max: int,
        setting_id: str = "",
    ) -> bool:
        """Set AMS tray filament settings (type, color, temperature).

        Note: K value is set separately via extrusion_cali_sel command.

        Args:
            ams_id: AMS unit ID (0-3 for regular AMS, 128-135 for HT AMS)
            tray_id: Tray ID within the AMS (0-3)
            tray_info_idx: Filament ID short format (e.g., "GFL05")
            tray_type: Filament type (e.g., "PLA", "PETG")
            tray_sub_brands: Sub-brand name (e.g., "PLA Basic", "PETG HF")
            tray_color: Color in RRGGBBAA hex format (e.g., "FFFF00FF")
            nozzle_temp_min: Minimum nozzle temperature
            nozzle_temp_max: Maximum nozzle temperature
            setting_id: Full setting ID with version (e.g., "GFSL05_07") - optional

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set AMS filament setting: not connected", self.serial_number)
            return False

        # Calculate mqtt IDs based on AMS type.
        # External-spool convention verified against a BambuStudio → X1C
        # packet capture (upstream Bambuddy #1279, May 2026): for
        # ``ams_filament_setting`` Studio sends the *global* tray index
        # in ``tray_id``, not a local position within the virtual unit.
        # The printer's response echoes ``tray_id: 0`` (slot position),
        # which is what the original code was matching — but the
        # request and response use different semantics for that field.
        # Sending ``tray_id: 0`` is what the P1S in #1279 rejected with
        # ``result: "fail"``, silently breaking external-spool filament
        # selection on every Bambu printer with no AMS or external
        # spool in active use.
        if ams_id == 255:
            vt_tray = self.state.raw_data.get("vt_tray", []) if self.state.raw_data else []
            if len(vt_tray) > 1:
                # Dual external slots (H2D): each ext slot is its own
                # virtual AMS unit (254=ext-L / slot 0, 255=ext-R /
                # slot 1). The dual case is NOT covered by the X1C
                # capture — left at ``mqtt_tray_id = 0`` until a
                # captured Studio → H2D exchange confirms the correct
                # value.
                mqtt_ams_id = 254 + tray_id
                mqtt_tray_id = 0
            else:
                # Single external slot (X1C, P1S, A1): global tray_id=254.
                mqtt_ams_id = 255
                mqtt_tray_id = 254
            slot_id = 0
        elif (_a2l := a2l_lite_wire_ids(ams_id, tray_id)) is not None:
            # A2L AMS-Lite: physical unit 16, local 0-3 slot (matches the
            # firmware's own ams_mapping2 {ams_id:16, slot_id:0-3}).
            mqtt_ams_id, slot_id, _ = _a2l
            mqtt_tray_id = slot_id
        elif ams_id <= 3:
            mqtt_ams_id = ams_id
            mqtt_tray_id = tray_id
            slot_id = tray_id
        else:
            # AMS-HT: single tray per unit
            mqtt_ams_id = ams_id
            mqtt_tray_id = tray_id
            slot_id = 0

        command = {
            "print": {
                "command": "ams_filament_setting",
                "ams_id": mqtt_ams_id,
                "tray_id": mqtt_tray_id,
                "slot_id": slot_id,
                "tray_info_idx": tray_info_idx,
                "tray_type": tray_type,
                "tray_sub_brands": tray_sub_brands,
                "tray_color": tray_color,
                "nozzle_temp_min": nozzle_temp_min,
                "nozzle_temp_max": nozzle_temp_max,
                "sequence_id": "0",
            }
        }

        # Include setting_id if provided (helps slicer show correct profile)
        if setting_id:
            command["print"]["setting_id"] = setting_id

        command_json = json.dumps(command)
        logger.info(
            f"[{self.serial_number}] Publishing ams_filament_setting: AMS {ams_id}, tray {tray_id}, tray_info_idx={tray_info_idx}, setting_id={setting_id}"
        )
        logger.debug("[%s] ams_filament_setting command: %s", self.serial_number, command_json)
        self._last_ams_cmd_time = time.monotonic()  # zombie detection (#887)
        self._client.publish(self.topic_publish, command_json, qos=1)
        return True

    def reset_ams_slot(self, ams_id: int, tray_id: int) -> bool:
        """Reset an AMS slot to empty/unconfigured state.

        Args:
            ams_id: AMS unit ID (0-3 for regular AMS, 128-135 for HT AMS)
            tray_id: Tray ID within the AMS (0-3)

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot reset AMS slot: not connected", self.serial_number)
            return False

        # Calculate mqtt IDs based on AMS type — same convention as
        # ``ams_set_filament_setting`` above. See its comment for the
        # #1279 capture rationale.
        if ams_id == 255:
            vt_tray = self.state.raw_data.get("vt_tray", []) if self.state.raw_data else []
            if len(vt_tray) > 1:
                # Dual external slots (H2D): each ext slot is its own
                # virtual AMS unit. Dual-external left at
                # ``mqtt_tray_id = 0`` pending a Studio → H2D capture.
                mqtt_ams_id = 254 + tray_id
                mqtt_tray_id = 0
            else:
                # Single external slot (X1C, P1S, A1): global tray_id=254.
                mqtt_ams_id = 255
                mqtt_tray_id = 254
            slot_id = 0
        elif (_a2l := a2l_lite_wire_ids(ams_id, tray_id)) is not None:
            # A2L AMS-Lite: physical unit 16, local 0-3 slot (matches the
            # firmware's own ams_mapping2 {ams_id:16, slot_id:0-3}).
            mqtt_ams_id, slot_id, _ = _a2l
            mqtt_tray_id = slot_id
        elif ams_id <= 3:
            mqtt_ams_id = ams_id
            mqtt_tray_id = tray_id
            slot_id = tray_id
        else:
            # AMS-HT: single tray per unit
            mqtt_ams_id = ams_id
            mqtt_tray_id = tray_id
            slot_id = 0

        command = {
            "print": {
                "command": "ams_filament_setting",
                "ams_id": mqtt_ams_id,
                "tray_id": mqtt_tray_id,
                "slot_id": slot_id,
                "tray_info_idx": "",
                "tray_type": "",
                "tray_sub_brands": "",
                "tray_color": "00000000",
                "nozzle_temp_min": 0,
                "nozzle_temp_max": 0,
                "sequence_id": "0",
            }
        }

        command_json = json.dumps(command)
        logger.info("[%s] Resetting AMS slot: AMS %s, tray %s", self.serial_number, ams_id, tray_id)
        logger.debug("[%s] reset_ams_slot command: %s", self.serial_number, command_json)
        self._last_ams_cmd_time = time.monotonic()  # zombie detection (#887)
        self._client.publish(self.topic_publish, command_json, qos=1)
        return True

    # ----------------- AMS Settings dialog publishers -----------------
    # The four below back the BambuStudio AMSSetting dialog (port). The
    # firmware_switch + reorder commands live in separate methods because
    # their payloads are not yet pinned down.

    def ams_user_setting(
        self,
        startup_read: bool,
        tray_read: bool,
        calibrate_remain: bool,
    ) -> tuple[bool, str | None]:
        """BS ``command_ams_user_settings`` (DeviceManager.cpp:1575).

        Sends the three RFID/remain toggles in one MQTT message.
        ``ams_id=-1`` is BS's "apply to every AMS on this printer" convention.
        Stamps a 3-second hold-timer on the three corresponding state fields
        so the push parser doesn't clobber the just-sent value while the
        printer confirms.

        Returns:
            (success, sequence_id_string_or_None)
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot send ams_user_setting: not connected", self.serial_number)
            return False, None

        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "print": {
                "command": "ams_user_setting",
                "sequence_id": seq,
                "ams_id": -1,
                "startup_read_option": bool(startup_read),
                "tray_read_option": bool(tray_read),
                "calibrate_remain_flag": bool(calibrate_remain),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        now = time.time()
        self.state.ams_settings_hold["ams_insertion_update"] = now
        self.state.ams_settings_hold["ams_power_on_update"] = now
        self.state.ams_settings_hold["ams_remain_capacity"] = now
        logger.info(
            "[%s] ams_user_setting: startup=%s tray=%s remain=%s seq=%s",
            self.serial_number,
            startup_read,
            tray_read,
            calibrate_remain,
            seq,
        )
        return True, seq

    def print_option_auto_switch_filament(self, enabled: bool) -> tuple[bool, str | None]:
        """BS ``command_ams_switch_filament`` (DeviceManager.cpp:1751).

        Routed through ``print.command = "print_option"`` (NOT
        ``ams_user_setting``) — same channel as ``air_print_detect``.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot send print_option auto_switch_filament: not connected", self.serial_number)
            return False, None

        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "print": {
                "command": "print_option",
                "sequence_id": seq,
                "auto_switch_filament": bool(enabled),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.ams_settings_hold["ams_auto_switch_filament"] = time.time()
        logger.info(
            "[%s] print_option auto_switch_filament=%s seq=%s",
            self.serial_number,
            enabled,
            seq,
        )
        return True, seq

    def print_option_air_print_detect(self, enabled: bool) -> tuple[bool, str | None]:
        """BS ``command_ams_air_print_detect`` (DeviceManager.cpp:1765)."""
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot send print_option air_print_detect: not connected", self.serial_number)
            return False, None

        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "print": {
                "command": "print_option",
                "sequence_id": seq,
                "air_print_detect": bool(enabled),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        self.state.ams_settings_hold["ams_air_print_detect"] = time.time()
        self.state.printer_settings_hold["air_print_nonvisual"] = time.time()
        logger.info(
            "[%s] print_option air_print_detect=%s seq=%s",
            self.serial_number,
            enabled,
            seq,
        )
        return True, seq

    def ams_calibrate(self, ams_id: int) -> bool:
        """BS ``command_ams_calibrate`` (DeviceManager.cpp:1595): ``M620 C<id>``.

        Wrapped via the existing ``send_gcode`` so the printer's
        ``gcode_claim_action`` envelope is added consistently with our other
        macro calls.
        """
        return self.send_gcode(f"M620 C{int(ams_id)}\n")

    def ams_firmware_switch(self, firmware_idx: int) -> tuple[bool, str | None]:
        """BS ``DevAmsSystemFirmwareSwitch::CrtlSwitchFirmware`` (DevFilaAmsSettingCtrl.cpp).

        Note: this payload sits under ``upgrade`` (NOT ``print``). ``src_id=1``
        is the slicer identifier — we reuse 1 so the printer treats us the
        same as BambuStudio.

        ``firmware_idx`` is the **id the device reported** for the chosen entry,
        never a position in a list. BS sends ``m_type_combobox->GetSelection()``
        — a row index — which coincides with the id only because the two A1
        personalities happen to be 0 and 1; the wire field is documented as the
        id, so we send the id and stay correct if a third ever appears.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot send ams_firmware_switch: not connected", self.serial_number)
            return False, None

        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "upgrade": {
                "command": "mc_for_ams_firmware_upgrade",
                "sequence_id": seq,
                "src_id": 1,
                "id": int(firmware_idx),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        # BS latches ``m_status = "SWITCHING"`` locally the moment the publish
        # succeeds, so the picker disappears before the printer has said
        # anything (DevFilaAmsSettingCtrl.cpp). We do the same, and take the
        # standard 3 s hold so the report already in flight — which still
        # carries the OLD selection — cannot flip the answer back.
        self.state.ams_firmware_status = "SWITCHING"
        self.state.ams_firmware_idx_sel = int(firmware_idx)
        self.state.ams_settings_hold["ams_firmware_switch"] = time.time()
        logger.info(
            "[%s] ams_firmware_switch: firmware_idx=%s seq=%s",
            self.serial_number,
            firmware_idx,
            seq,
        )
        return True, seq

    def ams_reset_sequence(self) -> tuple[bool, str | None]:
        """BS ``DevFilaSystem::CtrlAmsReset`` (DevFilaSystemCtrl.cpp:11).

        Resets the AMS ID sequence. The dialog flow expects the user to
        physically disconnect + reconnect AMS units in their desired order
        AFTER this is sent — there's no order array on the wire.
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot send ams_reset: not connected", self.serial_number)
            return False, None

        self._sequence_id += 1
        seq = str(self._sequence_id)
        command = {
            "print": {
                "command": "ams_reset",
                "sequence_id": seq,
            }
        }
        self._client.publish(self.topic_publish, json.dumps(command), qos=1)
        logger.info("[%s] ams_reset_sequence: seq=%s", self.serial_number, seq)
        return True, seq

    def extrusion_cali_sel(
        self,
        ams_id: int,
        tray_id: int,
        cali_idx: int,
        filament_id: str,
        nozzle_diameter: str = "0.4",
    ) -> bool:
        """Set calibration profile (K value) for an AMS slot.

        This command selects a K profile from the printer's calibration list.
        Use cali_idx=-1 to use the default K value (0.020).

        Note: Do NOT send setting_id in this command - BambuStudio never includes
        it, and adding it causes the firmware to mislink the profile on X1C/P1S.

        Args:
            ams_id: AMS unit ID (0-3 for regular AMS, 128-135 for HT AMS)
            tray_id: Tray ID within the AMS (0-3)
            cali_idx: Calibration profile index (-1 for default)
            filament_id: Filament preset ID (same as tray_info_idx)
            nozzle_diameter: Nozzle diameter string (e.g., "0.4")

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set calibration: not connected", self.serial_number)
            return False

        # Calculate mqtt IDs based on AMS type.
        # IMPORTANT: extrusion_cali_sel uses GLOBAL tray_id (unlike ams_filament_setting
        # which uses LOCAL).  BambuStudio confirms: tray_id = ams_id * 4 + slot.
        if ams_id == 255:
            # External spool: extrusion_cali_sel uses GLOBAL tray_id (unlike
            # ams_filament_setting which uses LOCAL tray_id=0).
            vt_tray = self.state.raw_data.get("vt_tray", []) if self.state.raw_data else []
            if len(vt_tray) > 1:
                # Dual external slots (H2D): each ext slot is its own virtual AMS unit
                # Confirmed from BambuStudio logs: ext-R sends ams_id=255, tray_id=255
                mqtt_ams_id = 254 + tray_id
                mqtt_tray_id = 254 + tray_id
            else:
                # Single-external (X1C / P1S / A1 / A1 mini / P1P):
                # VIRTUAL_TRAY_MAIN_ID per BambuStudio DevDefs.h.
                # Earlier code used 254 historically; firmware tolerated it
                # but it's out-of-spec (BS DevCalib.cpp:188-200 routes 254
                # to vt_slot[1] which only exists on H2D — non-H2D ack
                # falls into the else-branch parser path and never updates
                # the slot's local cali_idx cache).
                mqtt_ams_id = 255
                mqtt_tray_id = 255
            slot_id = 0
        elif (_a2l := a2l_lite_wire_ids(ams_id, tray_id)) is not None:
            # A2L AMS-Lite: physical unit 16 + local slot are confirmed; the
            # GLOBAL tray_id this command wants (physical 16*4+slot) is
            # extrapolated (no A2L cali_sel capture yet) — see a2l_lite_wire_ids.
            mqtt_ams_id, slot_id, mqtt_tray_id = _a2l
        elif ams_id <= 3:
            mqtt_ams_id = ams_id
            mqtt_tray_id = ams_id * 4 + tray_id
            slot_id = tray_id
        elif ams_id >= 128 and ams_id <= 135:
            mqtt_ams_id = ams_id
            mqtt_tray_id = tray_id
            slot_id = 0
        else:
            mqtt_ams_id = ams_id
            mqtt_tray_id = tray_id
            slot_id = 0

        command = {
            "print": {
                "command": "extrusion_cali_sel",
                "cali_idx": cali_idx,
                "filament_id": filament_id,
                "nozzle_diameter": nozzle_diameter,
                "ams_id": mqtt_ams_id,
                "tray_id": mqtt_tray_id,
                "slot_id": slot_id,
                "sequence_id": "0",
            }
        }

        command_json = json.dumps(command)
        logger.info(
            f"[{self.serial_number}] Publishing extrusion_cali_sel: AMS {ams_id}, tray {tray_id}, cali_idx={cali_idx}"
        )
        logger.debug("[%s] extrusion_cali_sel command: %s", self.serial_number, command_json)
        self._client.publish(self.topic_publish, command_json, qos=1)
        return True

    def extrusion_cali_set(
        self,
        tray_id: int,
        k_value: float,
        nozzle_diameter: str = "0.4",
        nozzle_temp: int = 220,
        filament_id: str = "",
        setting_id: str = "",
        name: str = "",
        cali_idx: int = -1,
    ) -> bool:
        """Directly set K value (pressure advance) for a tray.

        Uses the filaments array format required by current firmware.

        Args:
            tray_id: Global tray ID (ams_id * 4 + slot)
            k_value: Pressure advance K value (e.g., 0.020)
            nozzle_diameter: Nozzle diameter string (e.g., "0.4")
            nozzle_temp: Nozzle temperature for calibration reference
            filament_id: Filament preset ID (e.g., "GFA02")
            setting_id: Setting ID (e.g., "GFSA02_07")
            name: Profile display name
            cali_idx: Calibration index (-1 for new)

        Returns:
            True if command was sent, False otherwise
        """
        if not self._client or not self.state.connected:
            logger.warning("[%s] Cannot set K value: not connected", self.serial_number)
            return False

        nozzle_id = f"HS00-{nozzle_diameter}"

        # A2L AMS-Lite: a normalised global tray (24-27) must go out as the
        # physical global (extrapolated 64-67; see a2l_lite_wire_ids). ams_id
        # stays 0 (hardcoded, as for every other unit here).
        wire_tray_id = tray_id
        if 0 <= tray_id <= 253 and (_a2l := a2l_lite_wire_ids(tray_id // 4, tray_id)) is not None:
            wire_tray_id = _a2l[2]

        filament_entry = {
            "ams_id": 0,
            "cali_idx": cali_idx,
            "extruder_id": 0,
            "filament_id": filament_id,
            "k_value": f"{k_value:.6f}",
            "n_coef": "1.400000",
            "name": name,
            "nozzle_diameter": nozzle_diameter,
            "nozzle_id": nozzle_id,
            "setting_id": setting_id,
            "tray_id": wire_tray_id,
        }

        command = {
            "print": {
                "command": "extrusion_cali_set",
                "filaments": [filament_entry],
                "nozzle_diameter": nozzle_diameter,
                "sequence_id": str(self._sequence_id),
            }
        }

        command_json = json.dumps(command)
        logger.info("[%s] Publishing extrusion_cali_set: tray %s, k_value=%s", self.serial_number, tray_id, k_value)
        logger.debug("[%s] extrusion_cali_set command: %s", self.serial_number, command_json)
        self._client.publish(self.topic_publish, command_json, qos=1)
        return True

    # ---------- Filament Calibration wizard (m062 / Plan 1) ----------

    def extrusion_cali_start(
        self,
        *,
        nozzle_diameter: float,
        cali_mode: int,
        filaments: list[dict],
    ) -> tuple[bool, str | None]:
        """Start PA calibration. MQTT ``print.command=extrusion_cali``.

        cali_mode: 0=auto (X1 lidar), 1=manual. ``filaments`` is the
        full BS payload (one dict per tray, see CalibrationService).
        """
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        payload = {
            "print": {
                "command": "extrusion_cali",
                "sequence_id": seq,
                "nozzle_diameter": str(nozzle_diameter),
                "mode": cali_mode,
                "filaments": filaments,
            }
        }
        self._client.publish(self.topic_publish, json.dumps(payload), qos=1)
        self.state.extrusion_cali_session_id = seq
        self.state.extrusion_cali_status = "running"
        return True, seq

    def flow_rate_cali_start(
        self,
        *,
        nozzle_diameter: float,
        filaments: list[dict],
    ) -> tuple[bool, str | None]:
        """Start flow rate calibration (X1 auto). Same ``extrusion_cali``
        verb but with ``flow_rate`` populated in each filament dict.
        """
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        payload = {
            "print": {
                "command": "extrusion_cali",
                "sequence_id": seq,
                "nozzle_diameter": str(nozzle_diameter),
                "filaments": filaments,
            }
        }
        self._client.publish(self.topic_publish, json.dumps(payload), qos=1)
        self.state.extrusion_cali_session_id = seq
        self.state.extrusion_cali_status = "running"
        return True, seq

    def extrusion_cali_query_history(
        self,
        *,
        nozzle_diameter: float,
        extruder_id: int = 0,
    ) -> tuple[bool, str | None]:
        """Ask printer for current PA history. Reply pushes back via
        ``extrusion_cali_get`` (handled by ``_on_message``).
        """
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        payload = {
            "print": {
                "command": "extrusion_cali_get",
                "sequence_id": seq,
                "nozzle_diameter": str(nozzle_diameter),
                "extruder_id": extruder_id,
            }
        }
        self._client.publish(self.topic_publish, json.dumps(payload), qos=1)
        return True, seq

    def extrusion_cali_query_result(
        self,
        *,
        nozzle_diameter: float,
    ) -> tuple[bool, str | None]:
        """Ask printer for auto-cali result (X1 lidar batches).

        Reply lands in ``extrusion_cali_get_result`` push (handled by
        ``_on_message`` → ``state.extrusion_cali_results``).
        """
        if not self._client or not self.state.connected:
            return False, None
        self._sequence_id += 1
        seq = str(self._sequence_id)
        payload = {
            "print": {
                "command": "extrusion_cali_get_result",
                "sequence_id": seq,
                "nozzle_diameter": str(nozzle_diameter),
            }
        }
        self._client.publish(self.topic_publish, json.dumps(payload), qos=1)
        return True, seq
