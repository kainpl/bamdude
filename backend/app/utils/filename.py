"""Print-file filename validation matching Bambu Studio's save-dialog rules.

The Bambu printer SD card is FAT32/exFAT. Names containing the Windows /
DOS-reserved set (``< > : " / \\ | ? *``), ASCII control characters
(0x00-0x1F), or trailing dots / spaces cannot be created on it — FTP fails
with ``553 Could not create file`` (upstream Bambuddy #1540), far from the
rename/upload action that caused it. Bambu Studio refuses such names
client-side; BamDude now does the same at the rename, upload, and dispatch
boundaries so the failure surfaces with a clear message.
"""

INVALID_FILENAME_CHARS = '<>:"/\\|?*'

# FAT/exFAT cap on a single path component; UTF-8 byte length, not codepoints,
# because that is what the on-disk encoding limit actually is.
MAX_FILENAME_BYTES = 255


class InvalidFilenameError(ValueError):
    """Filename contains characters or shape the printer SD card rejects.

    ``char`` is the first offending character when the failure is a
    character-set violation, or ``None`` for structural failures (empty,
    bare ``.``, trailing space, too long, etc.). The frontend echoes it
    back to the user in the Bambu-Studio-style error message.
    """

    def __init__(self, message: str, char: str | None = None):
        super().__init__(message)
        self.char = char


def validate_print_filename(name: str) -> None:
    """Raise ``InvalidFilenameError`` if ``name`` would fail on the SD card.

    Matches Bambu Studio's save-dialog rejection set. Callers translate the
    exception into an HTTP 400 (or a clean dispatch rejection); the message is
    intentionally short and ASCII so it fits a translation template.
    """
    if not name or not name.strip():
        raise InvalidFilenameError("Filename cannot be empty")

    if name in (".", ".."):
        raise InvalidFilenameError("Filename cannot be '.' or '..'")

    for ch in name:
        if ch in INVALID_FILENAME_CHARS:
            raise InvalidFilenameError(f"Filename contains invalid character: {ch}", char=ch)
        if ord(ch) < 0x20:
            raise InvalidFilenameError("Filename contains a control character", char=ch)

    if name.endswith(" ") or name.endswith("."):
        raise InvalidFilenameError("Filename cannot end with a space or dot")

    if len(name.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise InvalidFilenameError(f"Filename exceeds {MAX_FILENAME_BYTES} bytes")


def safe_path_component(name: str, *, fallback: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    """Reduce a display name to something usable as ONE path component.

    ⚠️ A print's display name is not a filename. It comes from the ``print_name``
    embedded in the 3MF — a MakerWorld title like "Planter Pot with Drip Tray,
    12 cm / 5 inches" arrives verbatim — and several places build a directory or
    a file out of it. A ``/`` in such a name is a path SEPARATOR, not a
    character: the join silently gains a level, ``mkdir(parents=True)`` creates
    the one it implied, and the write that follows lands on a parent nobody
    made. The containment check on the join does not catch this — the deeper
    path is still under the parent, so it passes and then fails on ENOENT.

    Every character the SD-card rules already reject is REPLACED rather than
    dropped, so the result still reads like the original: that set is exactly
    the separators plus the Windows-reserved punctuation, which a Windows
    install needs for the same reason Linux needs the separators. Leading and
    trailing dots and spaces go too — so ``..`` reduces to nothing rather than
    to a relative path — and the result is capped to what one component holds.

    Returns *fallback* when nothing usable survives, so a name made entirely of
    separators cannot produce an empty path component.

    ⚠️ *max_bytes* is the budget for THIS COMPONENT ALONE. A caller that wraps
    the result in a prefix or an extension must subtract those, or the composed
    name can still exceed what the filesystem accepts.

    ⚠️ Deliberately not applied to the name BamDude displays. A title is allowed
    its punctuation, and refusing the slash would reject the very name this was
    reported about.
    """
    cleaned = "".join("-" if (ch in INVALID_FILENAME_CHARS or ord(ch) < 0x20 or ord(ch) == 0x7F) else ch for ch in name)
    cleaned = cleaned.strip(" .")

    if len(cleaned.encode("utf-8")) > max_bytes:
        # Cut on the byte limit, then drop any partial character the cut left.
        cleaned = cleaned.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore").strip(" .")

    return cleaned or fallback


def is_sliced_file(filename: str) -> bool:
    """Whether this filename names something the printer can be handed.

    Sliced files are ``.gcode`` and any ``.gcode.`` container (``.gcode.3mf``).
    Lives here rather than in the library router because the queue service needs
    the same answer, and a service importing a route module would be a circular
    import as well as backwards.

    ``BackgroundDispatchService._is_sliced_file`` looks like a duplicate and is
    not: it accepts only ``.gcode.3mf`` exactly. Deliberately left alone — the
    dispatcher is the last gate before FTP and its narrower rule is not this
    one's to widen.
    """
    lower = filename.lower()
    return lower.endswith(".gcode") or ".gcode." in lower


def derive_remote_filename(filename: str) -> str:
    """Compute the SD-card filename used when uploading a sliced print file.

    Strips repeated trailing ``.gcode.3mf`` / ``.3mf`` suffixes until the bare
    stem remains, then appends a single ``.3mf``; spaces are replaced with
    underscores because the firmware parses ``ftp://{filename}`` as a URL.

    Canonical for both the dispatch uploader and the post-print SD cleanup —
    when the two drift apart the cleanup misses, and a library row whose stored
    filename ended up with a doubled ``.gcode.3mf`` (#1542) leaves the real file
    on the SD card. On A1 firmware that lingering file becomes a ghost print on
    the next power-on (same family as the P1S behaviour in #374).

    Raises ``TypeError`` on non-string input rather than entering the strip
    loop, because a duck-typed object that returns truthy sentinels from
    ``endswith`` would never escape and the resulting unbounded allocation has
    cgroup-OOM'd the test runner under mocks.
    """
    if not isinstance(filename, str):
        raise TypeError(f"derive_remote_filename requires str, got {type(filename).__name__}")
    stem = filename
    while True:
        if stem.endswith(".gcode.3mf"):
            stem = stem[:-10]
        elif stem.endswith(".3mf"):
            stem = stem[:-4]
        else:
            break
    return f"{stem}.3mf".replace(" ", "_")
