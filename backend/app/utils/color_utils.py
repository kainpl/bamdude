"""Color comparison utilities for RFID/firmware color matching."""

# Alpha byte that means "fully opaque". Bambu firmware reports every opaque
# spool as RRGGBBFF, so this is the overwhelmingly common value.
_OPAQUE_ALPHA = "FF"


def spoolman_color_hex(rgba: str | None) -> str | None:
    """Normalise an RRGGBB(AA) value to what Spoolman's ``color_hex`` should hold
    (upstream 73912d4f, #2912).

    Eight characters only when the spool is genuinely translucent. Truncating to
    six unconditionally turned a clear spool's ``00000000`` into opaque black;
    passing everything through would rewrite the ``color_hex`` of every opaque
    spool on its next touch. Keeping the opaque case at six leaves existing data
    byte-identical.

    ``None`` for a missing value. Shorter than six passes through unchanged, so
    a malformed colour is not reshaped into something that looks valid.
    """
    if not rgba:
        return None
    clean = rgba.strip().removeprefix("#").upper()
    if len(clean) < 6:
        return clean or None
    if len(clean) >= 8 and clean[6:8] != _OPAQUE_ALPHA:
        return clean[:8]
    return clean[:6]


def color_match_key(color_hex: str | None) -> str:
    """The key two colours are compared on: **the shape they would be stored as**.

    The same rule as :func:`spoolman_color_hex`, so two colours match exactly when
    storing them would give the same value. ``000000FF`` still matches the
    six-character ``000000`` every existing filament holds (without that, the next
    AMS sync mints a duplicate filament for every spool), while a clear
    ``00000000`` gets its own filament and never matches the black one, in either
    direction. ``""`` rather than ``None`` so callers compare without guarding.
    """
    return spoolman_color_hex(color_hex) or ""


def colors_similar(hex_a: str, hex_b: str, threshold: int = 50) -> bool:
    """Compare two RRGGBB(AA) hex colors with tolerance for RFID/firmware variations.

    Uses Euclidean RGB distance. Alpha channel (bytes 7-8) is ignored.
    Default threshold of 50 accommodates typical RFID read variations
    (e.g. 7CC4D5 vs 56B7E6 = distance ~43.6) while rejecting clearly
    different colors (e.g. red vs blue = distance ~360).
    """
    a = hex_a.strip().upper()
    b = hex_b.strip().upper()
    if a == b:
        return True
    if len(a) < 6 or len(b) < 6:
        return False
    try:
        ra, ga, ba = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
        rb, gb, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    except ValueError:
        return False
    dist = ((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2) ** 0.5
    return dist <= threshold
