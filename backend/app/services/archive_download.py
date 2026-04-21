"""Helpers for downloading a 3MF file from a printer's SD card.

Uses :func:`bambu_ftp.download_file_try_paths_async` — the same one-shot,
multi-path, no-asyncio-timeout approach the cover-image endpoint uses
reliably for 28 MB files.  Previously the retry/on_print_start path used
``download_file_async`` with a hardcoded 60 s ``asyncio.wait_for`` wrapper,
which killed legitimate in-flight transfers on slow SD cards.
"""

from __future__ import annotations

import logging
from pathlib import Path

from backend.app.models.printer import Printer
from backend.app.services.bambu_ftp import (
    download_file_try_paths_async,
    get_ftp_retry_settings,
    list_files_async,
)

logger = logging.getLogger(__name__)


def build_filename_candidates(subtask_name: str | None, filename: str | None) -> list[str]:
    """Build the ordered list of filenames to probe.

    - ``{subtask_name}.gcode.3mf`` / ``{subtask_name}.3mf``
    - filename with variants (with/without .gcode, with/without .3mf)
    - space→underscore variants of every name above
    """
    names: list[str] = []

    if subtask_name:
        names.append(f"{subtask_name}.gcode.3mf")
        names.append(f"{subtask_name}.3mf")

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


async def try_download_3mf(
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
    3. If that fails, dir-list + fuzzy-match fallback (for filenames the
       printer renamed internally).

    Returns ``(temp_path, downloaded_filename)`` on success.  Caller
    cleans up the temp file.
    """
    candidates = build_filename_candidates(subtask_name, filename)
    if not candidates:
        return None

    _enabled, _count, _delay, ftp_timeout = await get_ftp_retry_settings()
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Stage 1: direct path probes via a single connection.  Root first,
    # then cache/model/data — same list the cover endpoint uses.
    all_remote_paths: list[tuple[str, str]] = []  # (remote_path, filename)
    for try_filename in candidates:
        if not try_filename.endswith(".3mf"):
            continue
        for subdir in ("/", "/cache/", "/model/", "/data/", "/data/Metadata/"):
            all_remote_paths.append((f"{subdir}{try_filename}", try_filename))

    # Use the first candidate's filename for the temp file name.  On
    # success we'll return the actual downloaded filename (which may
    # differ if a later path succeeded — but bambu_ftp overwrites
    # temp_path, so the bytes are correct either way).
    primary_filename = candidates[0]
    if not primary_filename.endswith(".3mf"):
        primary_filename = next((c for c in candidates if c.endswith(".3mf")), primary_filename)
    temp_path = temp_dir / primary_filename

    remote_paths_only = [rp for rp, _ in all_remote_paths]
    try:
        downloaded = await download_file_try_paths_async(
            printer.ip_address,
            printer.access_code,
            remote_paths_only,
            temp_path,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
        )
        if downloaded:
            logger.info(
                "Downloaded 3MF for printer %s (tried %d paths, temp=%s)",
                printer.name,
                len(remote_paths_only),
                temp_path,
            )
            return temp_path, primary_filename
    except Exception as e:
        logger.debug("download_file_try_paths_async failed: %s", e)

    # Stage 2: dir-list + fuzzy match.  Fires only if direct probes all
    # missed — common case: printer renamed the file internally or it
    # lives in an unexpected subdir.
    search_term = (subtask_name or filename or "").lower().replace(".gcode", "").replace(".3mf", "")
    if not search_term:
        return None

    search_dirs = ["/", "/cache", "/model", "/data", "/data/Metadata"]
    search_normalized = search_term.replace(" ", "_")
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
            if not fname.endswith(".3mf"):
                continue
            fname_normalized = fname.lower().replace(" ", "_")
            if search_normalized not in fname_normalized:
                continue
            full_remote = f"{search_dir}/{fname}" if search_dir != "/" else f"/{fname}"
            fallback_paths.append((full_remote, fname))

    if not fallback_paths:
        return None

    # Single connection, try every fuzzy-matched path.
    fuzzy_filename = fallback_paths[0][1]
    fuzzy_temp_path = temp_dir / fuzzy_filename
    try:
        downloaded = await download_file_try_paths_async(
            printer.ip_address,
            printer.access_code,
            [rp for rp, _ in fallback_paths],
            fuzzy_temp_path,
            socket_timeout=ftp_timeout,
            printer_model=printer.model,
        )
        if downloaded:
            logger.info(
                "Downloaded 3MF for printer %s via fuzzy match (tried %d paths)",
                printer.name,
                len(fallback_paths),
            )
            return fuzzy_temp_path, fuzzy_filename
    except Exception as e:
        logger.debug("fuzzy download_file_try_paths_async failed: %s", e)

    return None
