"""One process-owned file reader, separate from SDK and database lifetimes."""

import asyncio
import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from backend.app.core.config import settings
from backend.app.mcp_server.contracts import Coverage
from backend.app.services import log_reader
from backend.app.services.farm_diagnostics import paging
from backend.app.services.farm_diagnostics.access import DiagnosticError, require
from backend.app.services.farm_diagnostics.contracts import Evidence, ReadResult
from backend.app.services.queue_sources import current_epoch

RAW_BUDGET = 2 * 1024 * 1024
ENTRY_BUDGET = 4096
ENTRY_LIMIT = 200


class FileReader:
    def __init__(self):
        self._slot = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-log")
        self._future = None

    async def read(self, function, *args):
        if not self._slot.acquire(blocking=False):
            raise DiagnosticError("busy")

        def work():
            try:
                return function(*args)
            finally:
                self._slot.release()

        try:
            future = self._executor.submit(work)
        except BaseException:
            self._slot.release()
            raise
        self._future = future
        # Cancelling a request cannot cancel/release a thread still blocked in I/O.
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(wrapped)

    async def drain(self):
        future = self._future
        if future is not None:
            try:
                await asyncio.shield(asyncio.wrap_future(future))
            except Exception:
                pass  # errors are reported to the reader, never logged with secrets


file_reader = FileReader()


def _identity(stat):
    return [stat.st_dev, stat.st_ino]


def _anchor(stream, upper):
    stream.seek(0)
    first = stream.read(min(128, upper))
    stream.seek(max(0, upper - 128))
    return hashlib.sha256(first + stream.read(min(128, upper))).hexdigest()


def read_tail(args, secrets, position):
    # This is the only path, never supplied by the caller or the cursor.
    path = settings.log_dir / "bamdude.log"
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        upper = position["upper"] if position else stat.st_size
        if position and (_identity(stat) != position["identity"] or stat.st_size < upper):
            raise DiagnosticError("log_changed")
        anchor = _anchor(stream, upper)
        if position and anchor != position["anchor"]:
            raise DiagnosticError("log_changed")
        before = position["before"] if position else upper
        if not 0 <= before <= upper:
            raise DiagnosticError("invalid_cursor")
        # Budget includes both anchor passes, including the final rotation check.
        start = max(0, before - (RAW_BUDGET - 512))
        stream.seek(start)
        raw = stream.read(before - start)
        count = len(raw) + 4 * min(128, upper)
        cut = start > 0
        if cut:
            boundary = raw.find(b"\n")
            raw = raw[boundary + 1 :] if boundary >= 0 else b""
            start += boundary + 1 if boundary >= 0 else len(raw)
        offset, groups, current = start, [], None
        partial_end = bool(raw and not raw.endswith(b"\n")) or bool(position and not position["clean_end"])
        for physical in raw.splitlines(keepends=True):
            text = physical.decode("utf-8", errors="replace").rstrip("\r\n")
            if log_reader.parse_log_line(text):
                if current is not None:
                    groups.append(current)
                current = [offset, text]
            elif current is not None:
                current[1] += "\n" + text
            offset += len(physical)
        if current is not None:
            groups.append(current)
        dropped = int(cut)
        if partial_end and groups:
            groups.pop()
            dropped += 1
        entries, next_before, output_bytes = [], None, 0
        for offset, text in reversed(groups):
            # Exact secrets of any length, then the shared shape/PII sanitizer.
            safe = log_reader.sanitize_log_content(text, secrets)
            if len(safe.encode("utf-8")) > ENTRY_BUDGET:
                dropped += 1
                continue
            lines = safe.split("\n", 1)
            parsed = log_reader.parse_log_line(lines[0])
            if parsed is None:
                dropped += 1
                continue
            observed = datetime.strptime(parsed.timestamp, "%Y-%m-%d %H:%M:%S,%f").astimezone(timezone.utc)
            message = parsed.message + ("\n" + lines[1] if len(lines) > 1 else "")
            if not args.from_ <= observed < args.to:
                continue
            if args.level and parsed.level != args.level or args.component and parsed.logger_name != args.component:
                continue
            if args.search and args.search.casefold() not in (parsed.logger_name + "\n" + message).casefold():
                continue
            entry = {
                "observed_at": observed.isoformat(),
                "level": parsed.level
                if parsed.level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                else "UNKNOWN",
                "component": parsed.logger_name,
                "data_text": message,
            }
            size = len(json.dumps(entry, ensure_ascii=True).encode())
            # Leave room for the MCP text and structuredContent copies.
            if output_bytes + size > 32000:
                # Retry this complete entry on the next page, without including
                # entries already returned. Its upper boundary is the following header.
                index = next(i for i, group in enumerate(groups) if group[0] == offset)
                next_before = groups[index + 1][0] if index + 1 < len(groups) else before
                break
            entries.append(entry)
            output_bytes += size
            if len(entries) >= min(args.limit, ENTRY_LIMIT):
                next_before = offset
                break
        clean_end = True
        if next_before is None:
            if cut:
                next_before = groups[0][0] if groups else max(0, before - (RAW_BUDGET - 512))
                clean_end = bool(groups)
            else:
                next_before = 0
        if (
            _identity(path.stat()) != _identity(stat)
            or os.fstat(stream.fileno()).st_size < upper
            or _anchor(stream, upper) != anchor
        ):
            raise DiagnosticError("log_changed")
        continuation = (
            {
                "identity": _identity(stat),
                "upper": upper,
                "anchor": anchor,
                "before": next_before,
                "clean_end": clean_end,
            }
            if next_before
            else None
        )
        return entries, continuation, dropped, count


async def read_logs(access, args, request_id, owner):
    require(access, "bamdude_read_logs")
    started = datetime.now(timezone.utc)
    filters = args.model_dump(mode="json", by_alias=True, exclude={"cursor"})
    position = paging.decode_position(args.cursor, access, "logs", filters) if args.cursor else None
    if position:
        try:
            assert set(position) == {"identity", "upper", "anchor", "before", "clean_end"}
            assert type(position["upper"]) is int and type(position["before"]) is int
            assert 0 <= position["before"] <= position["upper"]
            assert len(position["identity"]) == 2 and isinstance(position["anchor"], str)
            assert type(position["clean_end"]) is bool
        except (AssertionError, TypeError):
            raise DiagnosticError("invalid_cursor") from None

    async def database_phase():
        from backend.app.core import database

        async with database.async_session() as db:
            return await log_reader.collect_diagnostic_sensitive_strings(db)

    secrets = await owner.database_read(database_phase())
    if access.epoch != current_epoch():
        raise DiagnosticError("state_changed")
    entries, continuation, dropped, count = await file_reader.read(read_tail, args, secrets, position)
    if access.epoch != current_epoch():
        raise DiagnosticError("state_changed")
    finished = datetime.now(timezone.utc)
    missing = ["log_retention_unknown", "log_timezone_offset_not_recorded"]
    if dropped:
        missing.append("incomplete_or_oversized_entries_omitted")
    if continuation:
        missing.append("additional_log_bytes_available")
    return ReadResult(
        request_id=request_id,
        collection_started_at=started,
        collection_finished_at=finished,
        data={
            "entries": entries,
            "dropped_entries_or_boundaries": dropped,
            "raw_bytes_read": count,
            "selection_basis": "server_local_log_time_converted_to_utc",
        },
        evidence=[
            Evidence(
                id="application_log",
                source="sanitized_application_log",
                collected_at=finished,
                coverage=Coverage(complete=False, missing=missing),
            )
        ],
        coverage=Coverage(complete=False, missing=missing, truncated=bool(continuation)),
        next_cursor=paging.encode_position(access, "logs", filters, continuation) if continuation else None,
        warnings=["log_text_is_untrusted_data", "redaction_cannot_guarantee_removal_of_unknown_secrets"],
    )
