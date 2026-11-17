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

import hashlib
import logging
import time

logger = logging.getLogger(__name__)

# Where a card keeps them. The list is ordered and model-dependent: X1/A1 use
# /timelapse, older P1 firmware /record. First directory with videos wins.
FTP_TIMELAPSE_DIRS = ("/timelapse", "/timelapse/video", "/record", "/recording")

# MP4 on X1/A1, AVI on P1.
VIDEO_EXTENSIONS = (".mp4", ".avi")

# The tunnel's own root, reported in every listing entry. Kept for the callers
# that need to tell an internal path from a card one.
INTERNAL_TIMELAPSE_ROOT = "/userdata/media/timelapse/"


# Whether the last listing of each printer heard from every medium it asked
# (audit D6 part 4). An empty listing means "no recordings" AND "the card never
# answered", and a baseline recorded from the second is not evidence of anything.
_listing_answered: dict[int, bool] = {}

# What each medium said in the last listing of each printer (G10 C3): the
# baseline kept with an archive records it per medium, so "the card was not
# asked" and "the card is empty" stay two different facts after a restart.
MEDIUM_ANSWERED = "answered"
MEDIUM_UNAVAILABLE = "unavailable"
MEDIUM_NOT_ASKED = "not_asked"
_listing_media: dict[int, dict[str, str]] = {}

# Automatic scans of a card whose file service did not let us connect wait
# before they knock again (upstream 36991dadc, G10 spec r4 §5.2): per endpoint,
# 30 s doubling to 5 min. A manual scan is never held back, and any answer from
# the card — or a changed address or access code, or a restart — starts over.
_BACKOFF_FIRST = 30.0
_BACKOFF_MAX = 300.0
_backoff: dict[str, tuple[float, float]] = {}


def _endpoint(printer) -> str:
    code = getattr(printer, "access_code", "") or ""
    return f"{printer.ip_address}|{hashlib.sha256(code.encode('utf-8')).hexdigest()[:12]}"


def _held_back(printer) -> bool:
    entry = _backoff.get(_endpoint(printer))
    return entry is not None and time.monotonic() < entry[0]


def _note_unreachable(printer) -> None:
    key = _endpoint(printer)
    previous = _backoff.get(key)
    step = _BACKOFF_FIRST if previous is None else min(previous[1] * 2, _BACKOFF_MAX)
    _backoff[key] = (time.monotonic() + step, step)


def _note_answered(printer) -> None:
    _backoff.pop(_endpoint(printer), None)


def last_listing_answered(printer_id: int) -> bool:
    """Whether the last :func:`list_timelapse_videos` for *printer_id* heard from every medium it asked.

    False when the card did not answer completely for every directory it was
    asked (a 550 the parent listing cannot settle counts as no answer), when the
    internal catalogue was needed and could not be read, and for a printer never
    listed: nothing has been heard from it (G10 CR2).
    """
    return _listing_answered.get(printer_id, False)


def last_listing_media(printer_id: int) -> dict[str, str]:
    """``{"external": …, "internal": …}`` of the last listing: answered, unavailable or not asked."""
    return dict(_listing_media.get(printer_id, {}))


def recording_storage(remote_path: str) -> str:
    """The medium a recording's path belongs to — read off the path, as the reader and the deleter read it."""
    return "internal" if (remote_path or "").startswith(INTERNAL_TIMELAPSE_ROOT) else "external"


def recording_key(entry: dict) -> str:
    """One recording on one medium: ``<storage>:<normalised path>``.

    Two files of the same name on the card and in internal storage are two
    recordings; a bare name cannot tell them apart.
    """
    from backend.app.services.archive_media import normalise_remote_path

    path = entry.get("path") or f"/timelapse/{entry.get('name', '')}"
    return f"{recording_storage(path)}:{normalise_remote_path(path)}"


def _is_in_baseline(entry: dict, baseline: set[str]) -> bool:
    """Whether a listed recording was already there. A baseline holds keys (:func:`recording_key`);
    a bare name in it — an older caller's — excludes that name on every medium, the safe direction."""
    return recording_key(entry) in baseline or entry.get("name", "") in baseline


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


async def list_timelapse_videos(printer, *, automatic: bool = False) -> tuple[list[dict], str | None]:
    """Every recording on the printer, from whichever medium has them.

    Returns ``(videos, source)`` where ``source`` is the directory the files
    came from, or ``"internal"`` for the tunnel catalogue. ``([], None)`` when
    there are none — which is an ordinary answer, not a failure — or when the
    printer did not answer; :func:`last_listing_answered` tells the two apart.

    ``automatic`` — a scan nobody asked for (the baseline at print start, the
    retries after completion): it skips a card that refused its last connect
    for a bounded while, and the internal catalogue is still read.
    """
    videos, source, answered = await _list_both_media(printer, automatic=automatic)
    _listing_answered[printer.id] = answered
    return videos, source


async def _list_both_media(printer, *, automatic: bool = False) -> tuple[list[dict], str | None, bool]:
    """``(videos, source, answered)`` — the listing, and whether every medium asked replied."""
    from backend.app.services.bambu_ftp import BambuFTPClient, list_files_checked_async

    card_answered = True
    card_skipped = automatic and _held_back(printer)
    if card_skipped:
        logger.info("[TIMELAPSE] %s refused its last FTP connect — the automatic scan skips the card", printer.name)
        card_answered = False
    started = time.monotonic()
    for directory in () if card_skipped else FTP_TIMELAPSE_DIRS:
        try:
            found, answered = await list_files_checked_async(
                printer.ip_address, printer.access_code, directory, printer_model=printer.model
            )
        except Exception as exc:  # noqa: BLE001 — an error is no answer (G10 CR2), not an empty directory
            logger.debug("[TIMELAPSE] %s failed on %s: %s", directory, printer.name, exc)
            card_answered = False
            continue
        if not answered:
            # ⚠️ Stop the whole FTP leg, not just this directory. The printer's
            # FTP daemon can wedge — a live X2D did, and stayed unreachable
            # until it was restarted — and every remaining directory then costs
            # another full timeout for the same answer. Four of them delayed a
            # recording that was sitting in internal storage by five minutes.
            logger.info(
                "[TIMELAPSE] %s is not answering on FTP — not trying the rest of the card",
                printer.name,
            )
            card_answered = False
            if BambuFTPClient.connect_failure_since(printer.ip_address, started):
                _note_unreachable(printer)
            break
        videos = _videos_only(found)
        if videos:
            _note_answered(printer)
            _listing_media[printer.id] = {
                "external": MEDIUM_ANSWERED if card_answered else MEDIUM_UNAVAILABLE,
                "internal": MEDIUM_NOT_ASKED,
            }
            return videos, directory, card_answered
    if card_answered:
        _note_answered(printer)
    card_state = MEDIUM_NOT_ASKED if card_skipped else MEDIUM_ANSWERED if card_answered else MEDIUM_UNAVAILABLE

    # Nothing on the card. On a machine that records internally there is a
    # whole catalogue FTP cannot see — this is the gap that left an archive
    # without its recording while the file sat on the printer.
    if not _supports_internal_timelapse(printer):
        _listing_media[printer.id] = {"external": card_state, "internal": MEDIUM_NOT_ASKED}
        return [], None, card_answered

    from backend.app.services.printer_files.factory import transport_for

    try:
        entries = await transport_for(printer, "internal").list_files("/", file_type="timelapse")
    except Exception as exc:  # noqa: BLE001 — same as above: absence is normal
        logger.debug("[TIMELAPSE] internal catalogue failed on %s: %s", printer.name, exc)
        _listing_media[printer.id] = {"external": card_state, "internal": MEDIUM_UNAVAILABLE}
        return [], None, False

    _listing_media[printer.id] = {"external": card_state, "internal": MEDIUM_ANSWERED}
    videos = _videos_only([e.as_dict() for e in entries])
    return (videos, "internal", card_answered) if videos else ([], None, card_answered)


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


def pick_new_recording(
    video_files: list[dict], baseline_names: set[str], reported_path: str, *, require_unambiguous: bool = False
) -> dict | None:
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

    *require_unambiguous* — the baseline was taken off a card that did not
    answer, so "new since the baseline" narrows nothing down (audit D6 part 4,
    upstream 59d2713a). One new recording, or the one the printer names, still
    answers; between several others this refuses to guess and leaves them for
    Scan for timelapse — the first of them would be an arbitrary video attached
    to this print, and with the tidy-up on, deleted off the printer.
    """
    new_files = [f for f in video_files if not _is_in_baseline(f, baseline_names)]
    if not new_files:
        return None

    wanted = reported_path.rsplit("/", 1)[-1].lower()
    if wanted:
        named = next((f for f in new_files if f.get("name", "").lower() == wanted), None)
        if named is not None:
            return named

    if require_unambiguous and len(new_files) > 1:
        logger.warning(
            "[TIMELAPSE] %s new recordings (%s) and no trustworthy baseline to tell them apart — "
            "leaving them on the printer for Scan for timelapse",
            len(new_files),
            ", ".join(str(f.get("name")) for f in new_files),
        )
        return None

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

    return await transport_for(printer, recording_storage(remote_path)).read_bytes(remote_path)


def remote_source(printer, remote_path: str, entry: dict | None = None):
    """The recording at ``remote_path`` as the publication seam claims it (``archive_media.RemoteSource``).

    The printer is named by its immutable ``connector_identity`` — a numeric id
    is handed to the next printer once this one is deleted — and the medium is
    read off the path, exactly as :func:`read_timelapse_video` reads it. ``None``
    for a printer without an identity: nothing is claimed then, rather than a
    claim under a name that identifies nobody.
    """
    from backend.app.services.archive_media import RemoteSource

    identity = getattr(printer, "connector_identity", None)
    if not isinstance(identity, str) or not identity:
        return None
    entry = entry or {}
    size = entry.get("size")
    mtime = entry.get("mtime")
    return RemoteSource(
        printer_identity=identity,
        storage=recording_storage(remote_path),
        path=remote_path,
        size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        mtime_raw=str(mtime) if mtime is not None else None,
    )


async def remove_recording_after_attach(printer, remote_path: str, *, may_run=None) -> bool:
    """Delete a recording from the printer once BamDude has a copy of it.

    ⚠️ **"A copy" means these bytes, proven, on disk, now — not this name** (G10 C).
    Three things must all hold at the moment of the command:

    * an archive holds a LIVE copy — the primary row that claims this printer,
      medium and path, shown by its archive, its file on disk. It is pinned
      for the whole tidy-up (``archive_media.pin_live_copy``): replacing or
      removing that video meanwhile revokes the pin, and a revoked pin
      deletes nothing. A receipt left by a replaced video is history and
      authorises nothing;
    * the recording as it is on the printer NOW has that copy's SHA-256 — it
      is read again and deleted in ONE occupancy of the printer's file lane
      (``delete_verified``), so nothing of ours comes between the proof and
      the command. A printer that recorded another video under the name
      meanwhile keeps it;
    * the file guard and the caller's own proof, asked last.

    A transport that cannot read and delete in one occupancy deletes nothing.
    The cost is a second read of the recording, paid only by installs that
    switched the tidy-up on. What stays outside: another client of the printer
    that replaces the file in the instant between that read and the command,
    and a delete the firmware has already accepted.

    *may_run* — the caller's own proof that it still owns this printer's
    moment (the scan after a print: still the same run). It is asked with the
    file guard after the wait for the printer's file channel, immediately
    before the command.

    Opt-in, off by default, and asked here rather than at each of the three
    call sites so the answer cannot differ between the automatic scan after a
    print and the two buttons.

    ⚠️ **Deleted through the transport that READ it**, chosen from the path for
    the same reason :func:`read_timelapse_video` chooses it that way. An FTP
    delete aimed at ``/userdata/media/timelapse/…`` succeeds at doing nothing —
    the same shape of bug that once let the auto-scan find a recording it could
    not then fetch.

    ⚠️ **Only called where the attach has already succeeded.** "We have a copy"
    is the whole justification for removing somebody's video, and it is not the
    same claim as "we tried to take a copy".

    ⚠️ **The answer is read** (G10 B). The transport reports a refused or failed
    delete as a result, not an exception, so "no exception" used to be logged as
    "removed" while the recording was still there. It also asks the file guard:
    nothing is deleted on a printer whose state is not freshly known.

    Never raises: a recording that cannot be removed is a full disk later, and
    an exception here would abandon the archive that was just completed.
    """
    from backend.app.api.routes.settings import get_setting
    from backend.app.core.database import async_session
    from backend.app.services import printer_file_guard
    from backend.app.services.bambu_ftp import DeleteResult
    from backend.app.services.printer_files.factory import transport_for

    try:
        async with async_session() as db:
            enabled = (await get_setting(db, "delete_timelapse_after_attach") or "").lower() == "true"
        if not enabled:
            return False
        if may_run is not None and not may_run():
            return False

        from backend.app.core.config import settings as app_settings
        from backend.app.services import archive_media

        identity = getattr(printer, "connector_identity", None)
        storage = recording_storage(remote_path)
        pin = None
        if isinstance(identity, str) and identity:
            async with async_session() as db:
                pin = await archive_media.pin_live_copy(db, app_settings.base_dir, identity, storage, remote_path)
        try:
            transport = transport_for(printer, storage)
            verified_delete = getattr(transport, "delete_verified", None)
            if verified_delete is None:
                logger.info(
                    "[TIMELAPSE] %s stays on %s — this transport cannot prove what it deletes",
                    remote_path,
                    printer.name,
                )
                return False
            # Decided on the lane, after its wait: a place in the lane is no right to delete.
            result = await verified_delete(
                remote_path,
                ready=lambda: pin is not None and pin.holds(),
                verify=lambda data: pin is not None and pin.matches(data),
                may_run=printer_file_guard.may_delete(printer.id, remote_path, also=may_run),
            )
        finally:
            archive_media.release_pin(pin)
    except Exception as exc:  # noqa: BLE001 — tidying up must not cost an archive
        logger.warning("[TIMELAPSE] Could not remove %s from %s: %s", remote_path, printer.name, exc)
        return False

    if result != DeleteResult.DELETED:
        logger.info(
            "[TIMELAPSE] %s stays on %s (%s) — BamDude has its copy either way",
            remote_path,
            printer.name,
            getattr(result, "value", result),
        )
        return False

    logger.info("[TIMELAPSE] Removed %s from %s — BamDude has it now", remote_path, printer.name)
    return True


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
