"""Finding and reading a printer's timelapse recordings, on either medium.

One place, because there were three. The auto-scan after a print, the manual
``/archives/{id}/timelapse/scan`` and ``/timelapse/select`` each carried their
own copy of "walk these four directories over FTP" — so teaching one of them
about internal storage would have left the other two blind, and the three would
have drifted apart the way three copies of anything do.

⚠️ **The two media are found in genuinely different ways.** On a card the
recordings are files in a directory, and which directory depends on the model.
Over the tunnel they are a *catalogue* asked for by name, rooted at
``/userdata/media/timelapse/``. Same idea, two mechanisms — see
``printer_files`` for the same split on model files.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Where a card keeps them. The list is ordered and model-dependent: X1/A1 use
# /timelapse, older P1 firmware /record. First directory with videos wins.
FTP_TIMELAPSE_DIRS = ("/timelapse", "/timelapse/video", "/record", "/recording")

# MP4 on X1/A1, AVI on P1.
VIDEO_EXTENSIONS = (".mp4", ".avi")

# The tunnel's own root, reported in every listing entry. Kept for the callers
# that need to tell an internal path from a card one.
INTERNAL_TIMELAPSE_ROOT = "/userdata/media/timelapse/"


def _videos_only(entries: list[dict]) -> list[dict]:
    return [e for e in entries if not e.get("is_directory") and e.get("name", "").lower().endswith(VIDEO_EXTENSIONS)]


def _supports_internal_timelapse(printer) -> bool:
    """⚠️ ``fun`` bit 28, NOT the bit that gates the model catalogue.

    A machine can keep models internally and timelapses on the card, or the
    other way round — BambuStudio gates the two browser tabs on different
    flags. Reusing the model one here would ask for a catalogue the printer
    does not keep.
    """
    from backend.app.services.printer_manager import printer_manager

    state = printer_manager.get_status(printer.id)
    support = getattr(state, "print_option_support", None) or {}
    return bool(support.get("internal_timelapse"))


async def list_timelapse_videos(printer) -> tuple[list[dict], str | None]:
    """Every recording on the printer, from whichever medium has them.

    Returns ``(videos, source)`` where ``source`` is the directory the files
    came from, or ``"internal"`` for the tunnel catalogue. ``([], None)`` when
    there are none — which is an ordinary answer, not a failure.
    """
    from backend.app.services.bambu_ftp import list_files_async

    for directory in FTP_TIMELAPSE_DIRS:
        try:
            found = await list_files_async(
                printer.ip_address, printer.access_code, directory, printer_model=printer.model
            )
        except Exception as exc:  # noqa: BLE001 — a missing directory is normal
            logger.debug("[TIMELAPSE] %s failed on %s: %s", directory, printer.name, exc)
            continue
        videos = _videos_only(found)
        if videos:
            return videos, directory

    # Nothing on the card. On a machine that records internally there is a
    # whole catalogue FTP cannot see — this is the gap that left an archive
    # without its recording while the file sat on the printer.
    if not _supports_internal_timelapse(printer):
        return [], None

    from backend.app.services.printer_files.factory import transport_for

    try:
        entries = await transport_for(printer, "internal").list_files("/", file_type="timelapse")
    except Exception as exc:  # noqa: BLE001 — same as above: absence is normal
        logger.debug("[TIMELAPSE] internal catalogue failed on %s: %s", printer.name, exc)
        return [], None

    videos = _videos_only([e.as_dict() for e in entries])
    return (videos, "internal") if videos else ([], None)


def last_recording_path(printer) -> str:
    """The absolute path the printer says it last finished writing.

    ``device.cam.timelapse_path`` — ``/userdata/media/timelapse/…`` for internal,
    ``/media/usb0/timelapse/…`` for the card. Cleared to ``""`` when a print
    starts and filled once the file is closed, so an empty string means "nothing
    finished", not "no camera".

    ⚠️ **The LAST recording, not THIS print's.** Normally the same file; not on
    two prints in quick succession, nor after somebody records from the
    printer's own screen. It is evidence, never an answer on its own — see
    :func:`pick_new_recording`.
    """
    from backend.app.services.printer_manager import printer_manager

    return str(getattr(printer_manager.get_status(printer.id), "timelapse_path", "") or "")


def pick_new_recording(video_files: list[dict], baseline_names: set[str], reported_path: str) -> dict | None:
    """Which of these recordings belongs to the print that just ended.

    Two independent signals, and the weaker one is still the gate:

    * the **baseline** — files that were already there when this print started
      are not it. This alone is what the picker used to be, and it assumes the
      one new file is the right one;
    * the printer's own **reported path**, which names the file outright.

    The named file is taken only when it is ALSO new, so the ambiguity in
    :func:`last_recording_path` cannot promote a recording from an earlier
    print. Where they disagree — or where the printer reported nothing — the
    baseline answer stands, exactly as before.
    """
    new_files = [f for f in video_files if f.get("name", "") not in baseline_names]
    if not new_files:
        return None

    wanted = reported_path.rsplit("/", 1)[-1].lower()
    if wanted:
        named = next((f for f in new_files if f.get("name", "").lower() == wanted), None)
        if named is not None:
            return named

    # ⚠️ Still first-of-the-new, not "newest by timestamp". A card carries no
    # reliable mtime for these and the tunnel catalogue is ordered by the
    # printer; sorting on either was what made this guess a guess.
    return new_files[0]


async def read_timelapse_video(printer, remote_path: str) -> bytes | None:
    """Read one recording, choosing the medium from the path it came from.

    ⚠️ The path decides, not the printer's current state. A card inserted
    between the listing and the download must not turn an internal path into an
    FTP one — the file is where the listing said it was.
    """
    from backend.app.services.printer_files.factory import transport_for

    storage = "internal" if remote_path.startswith(INTERNAL_TIMELAPSE_ROOT) else "external"
    return await transport_for(printer, storage).read_bytes(remote_path)


def match_by_model_name(videos: list[dict], *candidates: str | None) -> dict | None:
    """The exact match, when the medium offers one.

    ⚠️ Only the tunnel's catalogue carries ``model_name``, and it equals the
    ``subtask_name`` the print command sent — so this is an identity, not a
    heuristic. The FTP path has no such field and must keep guessing from
    filenames and timestamps; this returns ``None`` there and the callers fall
    through to those strategies.

    Several candidates because the name is not kept in one place: an archive has
    the stem of its filename, and ``extra_data["original_subtask"]`` when the
    print was observed live. They usually agree; when they do not, either may be
    the one the printer recorded.
    """
    wanted = {c.strip().lower() for c in candidates if c and c.strip()}
    if not wanted:
        return None
    for video in videos:
        if (video.get("model_name") or "").strip().lower() in wanted:
            return video
    return None


def archive_subtask_name(archive) -> str | None:
    """The subtask name an archive remembers, if it saw the print happen."""
    meta = getattr(archive, "extra_data", None) or {}
    print_data = meta.get("_print_data") or {}
    return print_data.get("subtask_name") or meta.get("original_subtask")
