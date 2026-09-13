"""Bounded, credential-free worker log records carried over its stderr pipe."""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
import uuid
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import quote

PREFIX = "CAMERA_WORKER_LOG "
MAX_LINE_BYTES = 8192
_URL = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]{0,63}://[^\s<>\"']+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_secrets: ContextVar[tuple[str, ...]] = ContextVar("camera_worker_log_secrets", default=())
_identity: ContextVar[str | None] = ContextVar("camera_worker_log_identity", default=None)


def set_camera_log_secrets(*values: str | None, identity: str | None = None) -> None:
    """Set only inside a capture/producer task; context follows to_thread too."""
    _secrets.set(tuple(value for value in values if value))
    _identity.set(str(uuid.UUID(identity)) if identity is not None else None)


def sanitize_message(message: str) -> str:
    # Redact before truncating: cutting a URL first can leave a password prefix.
    for value in sorted(_secrets.get(), key=len, reverse=True):
        message = message.replace(value, "[redacted]").replace(quote(value, safe=""), "[redacted]")
    message = _URL.sub("[camera-url]", message)
    return _CONTROL.sub(" ", message)[:2048]


class WorkerLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if identity := _identity.get():
                message = f"identity={identity} {message}"
            if record.exc_info and record.exc_info[1] is not None:
                # Exception messages can echo opaque credentials. Retain the
                # type and source locations, without exception text or locals.
                frames = traceback.extract_tb(record.exc_info[2])[-8:]
                locations = " > ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames)
                message += f" [{type(record.exc_info[1]).__name__}: {locations}]"
            payload = {
                "level": record.levelno,
                "logger": record.name[:160],
                "message": sanitize_message(message),
            }
            line = PREFIX + json.dumps(payload, ensure_ascii=False) + "\n"
            if len(line.encode("utf-8")) <= MAX_LINE_BYTES:
                sys.stderr.write(line)
                sys.stderr.flush()
        except Exception:
            # Never fall back to logging.handleError: it prints raw args.
            pass


def configure_worker_logging() -> None:
    logging.basicConfig(level=logging.INFO, handlers=[WorkerLogHandler()], force=True)


def decode_worker_log(line: bytes) -> tuple[int, str, str] | None:
    """Accept only our structured records; arbitrary child stderr is not logged."""
    if len(line) > MAX_LINE_BYTES or not line.startswith(PREFIX.encode()):
        return None
    try:
        record = json.loads(line[len(PREFIX) :])
        if not isinstance(record, dict) or set(record) != {"level", "logger", "message"}:
            return None
        level, name, message = record["level"], record["logger"], record["message"]
        if type(level) is not int or level not in {10, 20, 30, 40, 50}:
            return None
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,160}", name):
            return None
        if not isinstance(message, str):
            return None
        return level, name, sanitize_message(message)
    except (ValueError, UnicodeError):
        return None
