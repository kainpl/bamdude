"""Helpers for downloading a 3MF file from a printer's SD card.

Uses :func:`bambu_ftp.download_file_try_paths_async` — the same one-shot,
multi-path, no-asyncio-timeout approach the cover-image endpoint uses
reliably for 28 MB files.  Previously the retry/on_print_start path used
``download_file_async`` with a hardcoded 60 s ``asyncio.wait_for`` wrapper,
which killed legitimate in-flight transfers on slow SD cards.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path, PurePosixPath

from backend.app.models.printer import Printer
from backend.app.services.archive import sd_stem
from backend.app.services.bambu_ftp import (
    BambuFTPClient,
    download_first_path,
    get_ftp_retry_settings,
    list_files_async,
)
from backend.app.utils.safe_path import safe_join_under

logger = logging.getLogger(__name__)

# Why the last download for each printer came back empty (audit D6). The callers
# that mark an archive "3MF unavailable" read it straight after the call, and the
# Archives banner words itself by it. ⚠️ There is deliberately no "internal
# storage" reason: both storages are read on every printer that has both, so a
# file kept in the printer's own memory is never why an archive is empty.
_failure_reasons: dict[int, str] = {}

# How a failed FTP connect reads as a reason. Anything else — every connect
# succeeded — means the file was on neither storage.
_REASON_BY_CONNECT_FAILURE = {
    "tls": "ftps_refused",
    "sessions": "ftps_refused",
    "auth": "auth_rejected",
    "timeout": "unreachable",
    "network": "unreachable",
}


def last_download_failure_reason(printer_id: int) -> str | None:
    """Why the last :func:`try_download_3mf` for *printer_id* found nothing, or None.

    ``ftps_refused`` / ``auth_rejected`` / ``unreachable`` — BamDude never got to
    look; ``not_found`` — it looked and the file was not there. ``None`` after a
    success.
    """
    return _failure_reasons.get(printer_id)


class DownloadedSource(tuple):
    """``(temp_path, downloaded_filename)`` — and ``storage``, the medium the bytes came from.

    Still a two-item tuple, so every caller that unpacks it as before keeps
    working; ``storage`` (``external`` / ``internal``) is the provenance the
    transport proved (G10 A7). The name is the basename of the path that really
    downloaded, never the temp file's name.
    """

    storage: str

    def __new__(cls, temp_path: Path, filename: str, storage: str):
        source = super().__new__(cls, (temp_path, filename))
        source.storage = storage
        return source


def build_filename_candidates(subtask_name: str | None, filename: str | None) -> list[str]:
    """Build the ordered list of filenames to probe.

    - ``{stem}.gcode.3mf`` / ``{stem}.3mf`` of the subtask — and the subtask
      itself first when it already is a card name
    - filename with variants (with/without .gcode, with/without .3mf)
    - space→underscore variants of every name above

    ⚠️ An A1 printing from USB reports the card name, suffix included, as the
    subtask (#1542). Appending suffixes to it probed paths that cannot exist
    and, because the first candidate names the temp file, stored the archive
    as ``X.gcode.3mf.gcode.3mf`` — and its ``print_name`` as ``X.gcode.3mf``.
    """
    names: list[str] = []

    if subtask_name:
        if subtask_name.endswith(".3mf"):
            names.append(subtask_name)
        stem = sd_stem(subtask_name)
        names.append(f"{stem}.gcode.3mf")
        names.append(f"{stem}.3mf")

    if filename:
        fname = filename.split("/")[-1] if "/" in filename else filename
        if fname.endswith(".3mf"):
            names.append(fname)
        elif fname.endswith(".gcode"):
            base = fname.rsplit(".", 1)[0]
            names.append(f"{base}.gcode.3mf")
            names.append(f"{base}.3mf")
        else:
            names.append(f"{fname}.gcode.3mf")
            names.append(f"{fname}.3mf")

    # Space→underscore variants (Bambu Studio often normalises filenames).
    space_variants = [name.replace(" ", "_") for name in names if " " in name]
    names.extend(space_variants)

    # Dedupe, preserve order.
    seen: set[str] = set()
    return [x for x in names if not (x in seen or seen.add(x))]


def _comparable(name: str) -> str:
    """A card name as the printer may spell it: case and space/underscore folded."""
    return name.strip().lower().replace(" ", "_")


def _comparable_names(candidates: list[str]) -> frozenset[str]:
    """The 3MF names a print's own file can have, ready for comparison."""
    return frozenset(_comparable(c) for c in candidates if c.lower().endswith(".3mf"))


def is_named_for_print(listed_name: str, wanted_names: frozenset[str]) -> bool:
    """Whether a file of a printer's directory listing is THIS print's 3MF.

    Only a whole-name match on one of the print's own name variants
    (``X.3mf`` / ``X.gcode.3mf``, in the printer's case and with its underscores
    for spaces) counts. A file that only contains the name belongs to another
    print, and "the one file that contains it" is still a guess about bytes
    that become this archive's thumbnail, weights and usage figures.
    """
    if not listed_name.lower().endswith(".3mf"):
        return False
    return _comparable(listed_name) in wanted_names


async def try_download_3mf(
    printer: Printer,
    subtask_name: str | None,
    filename: str | None,
    temp_dir: Path,
) -> tuple[Path, str] | None:
    """Attempt to download a 3MF file from *printer*'s storage.

    Returns ``(temp_path, downloaded_filename)`` on success. On failure
    :func:`last_download_failure_reason` says why.
    """
    _failure_reasons.pop(printer.id, None)
    started = time.monotonic()
    result = await _download_3mf(printer, subtask_name, filename, temp_dir)
    if result is None:
        kind = BambuFTPClient.connect_failure_since(printer.ip_address, started)
        _failure_reasons[printer.id] = _REASON_BY_CONNECT_FAILURE.get(kind or "", "not_found")
    return result


async def _download_3mf(
    printer: Printer,
    subtask_name: str | None,
    filename: str | None,
    temp_dir: Path,
) -> tuple[Path, str] | None:
    """Attempt to download a 3MF file from *printer*'s SD card.

    Strategy:
    1. Build list of filename candidates (subtask / filename variants +
       space-normalised), expand against common remote directories.
    2. One single ``download_file_try_paths_async`` call — opens ONE FTP
       session, tries every remote path on that session, returns on first
       success.  No outer asyncio timeout kills the transfer mid-flight.
    3. If that fails, dir-list + a whole-name match on the same candidates
       in the printer's own spelling (case, underscores for spaces) — never
       a substring of the name.

    Returns ``(temp_path, downloaded_filename)`` on success.  Caller
    cleans up the temp file.
    """
    candidates = build_filename_candidates(subtask_name, filename)
    if not candidates:
        return None
    original_temp_dir = temp_dir

    _enabled, _count, _delay, ftp_timeout = await get_ftp_retry_settings()
    # Per-printer subdir avoids the cross-printer race when two printers
    # download a same-named file concurrently. Without this, both writers
    # opened the same temp/<filename> with "wb", truncated each other's
    # output, and the resulting bytes were a sparse / corrupt 3MF.
    temp_dir = temp_dir / str(printer.id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1: direct path probes via a single connection.  Root first,
    # then cache/model/data — same list the cover endpoint uses.
    all_remote_paths: list[tuple[str, str]] = []  # (remote_path, filename)
    for try_filename in candidates:
        if not try_filename.endswith(".3mf"):
            continue
        for subdir in ("/", "/cache/", "/model/", "/data/", "/data/Metadata/"):
            all_remote_paths.append((f"{subdir}{try_filename}", try_filename))

    # Use the first candidate's filename for the temp file name. The name
    # RETURNED is the basename of the remote path that actually downloaded:
    # a later candidate may be the one that existed, and only the transport
    # can say which (swap-source-lifecycle spec r4 §7) — the temp file's name
    # proves nothing.
    primary_filename = candidates[0]
    if not primary_filename.endswith(".3mf"):
        primary_filename = next((c for c in candidates if c.endswith(".3mf")), primary_filename)
    # candidates are built from printer MQTT / FTP-derived names — containment-check.
    temp_path = safe_join_under(temp_dir, primary_filename, http=False)

    remote_paths_only = [rp for rp, _ in all_remote_paths]
    stage_one_started = time.monotonic()
    # What this ONE walk has already tried and must not fetch again (upstream
    # f0818ff18): a path whose transfer started and failed, and paths the printer
    # refused. A later trigger — reconnect, completion, a manual retry — is a new
    # walk and may try them again.
    tried: set[str] = set()
    try:
        walk = await download_first_path(
            printer.ip_address,
            printer.access_code,
            remote_paths_only,
            temp_path,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
        )
        if walk.path:
            logger.info(
                "Downloaded 3MF for printer %s (tried %d paths, temp=%s)",
                printer.name,
                len(remote_paths_only),
                temp_path,
            )
            return DownloadedSource(temp_path, PurePosixPath(walk.path).name, "external")
        tried.update(walk.not_readable)
        if walk.transfer_failed:
            tried.add(walk.transfer_failed)
            logger.info(
                "3MF transfer of %s from printer %s started and failed; this walk does not fetch it again",
                walk.transfer_failed,
                printer.name,
            )
    except Exception as e:
        logger.debug("download_first_path failed: %s", e)

    # Stage 2: dir-list + a match on the print's OWN name. Fires only if the
    # direct probes all missed — the printer keeps the name in another case or
    # with its own space/underscore spelling. Never a substring: a file that
    # merely contains the name is another print's ("Cube_large.3mf" is not
    # "Cube"'s file), and its bytes would be attached to this print's archive
    # (upstream bb0bf03f6).
    wanted_names = _comparable_names(candidates)
    if not wanted_names:
        return await _try_internal_storage(printer, candidates, original_temp_dir)
    # ...but only when the printer answered. Stage 1 could not even connect —
    # refused, rejected, silent — so five directory listings, each a connect of
    # its own, cannot get further (upstream 91acac2b). The retry triggers come
    # back for the file; internal storage is another transport and is still asked.
    if BambuFTPClient.connect_failure_since(printer.ip_address, stage_one_started):
        return await _try_internal_storage(printer, candidates, original_temp_dir)

    search_dirs = ["/", "/cache", "/model", "/data", "/data/Metadata"]
    fallback_paths: list[tuple[str, str]] = []  # (full_remote, filename)

    for search_dir in search_dirs:
        try:
            dir_files = await list_files_async(
                printer.ip_address, printer.access_code, search_dir, printer_model=printer.model
            )
        except Exception as e:
            logger.debug("Failed to list %s: %s", search_dir, e)
            continue

        for f in dir_files:
            if f.get("is_directory"):
                continue
            fname = f.get("name", "")
            if not is_named_for_print(fname, wanted_names):
                continue
            full_remote = f"{search_dir}/{fname}" if search_dir != "/" else f"/{fname}"
            if full_remote in tried:
                continue  # this walk already tried it — and a failed transfer is not retried here
            fallback_paths.append((full_remote, fname))

    if not fallback_paths:
        return await _try_internal_storage(printer, candidates, original_temp_dir)

    # Single connection, try every fuzzy-matched path.
    fuzzy_filename = fallback_paths[0][1]
    # fname comes straight from the printer FTP dir listing — containment-check.
    fuzzy_temp_path = safe_join_under(temp_dir, fuzzy_filename, http=False)
    try:
        walk = await download_first_path(
            printer.ip_address,
            printer.access_code,
            [rp for rp, _ in fallback_paths],
            fuzzy_temp_path,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
        )
        if walk.path:
            logger.info(
                "Downloaded 3MF for printer %s via fuzzy match (tried %d paths)",
                printer.name,
                len(fallback_paths),
            )
            return DownloadedSource(fuzzy_temp_path, PurePosixPath(walk.path).name, "external")
    except Exception as e:
        logger.debug("fuzzy download_first_path failed: %s", e)

    return await _try_internal_storage(printer, candidates, original_temp_dir)


async def _try_internal_storage(printer: Printer, candidates: list[str], temp_dir: Path) -> tuple[Path, str] | None:
    """Last resort: the file may be in the printer's internal storage.

    FTP only ever sees the card. On a machine that keeps models internally —
    X2D, the H2 family — a print started from the printer's own screen with no
    card inserted leaves nothing for any of the stages above to find, and the
    archive would keep ``file_path=""`` and ``no_3mf_available=True`` for ever
    across all four of ``archive_download_retry``'s triggers, with the file
    sitting right there on the machine.

    ⚠️ Gated on the capability, not attempted blindly: on P1S and A1 mini port
    6000 is open and completes a TLS handshake, but that is the camera daemon
    and it answers no tunnel frame at all. Probing it would cost a timeout on
    every failed recovery for the majority of the farm.
    """
    from backend.app.services.printer_files.factory import transport_for
    from backend.app.services.printer_manager import printer_manager
    from backend.app.utils.printer_storage import INTERNAL, storage_capability_for

    capability = storage_capability_for(printer.model, printer_manager.get_status(printer.id))
    if not capability["can_browse_internal"]:
        return None

    transport = transport_for(printer, INTERNAL)
    try:
        entries = await transport.list_files("/")
    except Exception as e:
        logger.debug("Tunnel listing failed while recovering a 3MF for %s: %s", printer.name, e)
        return None

    wanted = {c.lower() for c in candidates}
    match = next((e for e in entries if e.name.lower() in wanted), None)
    if match is None:
        return None

    temp_dir = temp_dir / str(printer.id)
    # ``match.name`` comes from the printer's own listing — containment-check it
    # the same way the FTP stages do.
    temp_path = safe_join_under(temp_dir, match.name, http=False)
    # Streamed to disk off the loop, checked frame by frame (G10 A7): never the
    # whole file in memory, never a blocking write on the event loop.
    outcome = await transport.download_to_file(match.path, temp_path)
    if not outcome.completed:
        logger.info("Could not recover %s from %s's internal storage (%s)", match.name, printer.name, outcome.status)
        return None
    logger.info("Recovered %s from %s's internal storage", match.name, printer.name)
    return DownloadedSource(temp_path, match.name, "internal")
