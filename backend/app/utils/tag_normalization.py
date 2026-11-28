"""Shared helpers for normalizing RFID tag and tray identifiers."""

import uuid


def normalize_hex(value: str | None) -> str:
    if not value:
        return ""
    hex_chars = "".join(ch for ch in str(value).strip() if ch in "0123456789abcdefABCDEF")
    return hex_chars.upper()


def normalize_tag_uid(value: str | None) -> str:
    uid = normalize_hex(value)
    # DB column is VARCHAR(16), so keep the least-significant bytes if longer.
    if len(uid) > 16:
        uid = uid[-16:]
    return uid


def normalize_tray_uuid(value: str | None) -> str:
    uuid = normalize_hex(value)
    # DB column is VARCHAR(32). Keep canonical 32-char UUID when possible.
    if len(uuid) >= 32:
        uuid = uuid[:32]
    return uuid


def is_bambu_tray_uuid(value: str | None) -> bool:
    """True when ``value`` is a tray UUID a Bambu tag really carries (upstream 12e543b1b).

    Every tray_uuid seen from the AMS and from tag block 9 is an RFC 4122
    version-4 UUID. An NFC reader that took tag blocks 4-5 instead -- SpoolBuddy
    before upstream #984 did -- sends the filament type ("PLA Matte" as hex),
    which fails this check: matched or stored, every spool of one type would
    share one id. The NFC integration doors (the built-in tag lookup and link,
    Spoolman's tag link) drop such a value and fall back to the tag UID.
    """
    normalized = normalize_tray_uuid(value)
    if len(normalized) != 32:
        return False
    parsed = uuid.UUID(hex=normalized)
    return parsed.version == 4 and parsed.variant == uuid.RFC_4122
