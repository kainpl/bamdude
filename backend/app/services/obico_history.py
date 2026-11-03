"""Bounded, opt-in evidence recorder. No camera or print commands belong here.

The guard owns publication and every unlink, including the entire file worker
after cancellation. Backup pins register under that guard but do not retain it
while copying. SQL deletion commits before the pin check/unlink (GH105 P1).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from PIL import Image
from sqlalchemy import and_, delete, func, or_, select

from backend.app.core import database
from backend.app.core.db_portable import _file_work
from backend.app.models.obico_event import ObicoEvent
from backend.app.models.printer import Printer
from backend.app.services.obico_geometry import PROFILE, project_boxes, validate_boxes

logger = logging.getLogger(__name__)
ROOT_NAME = "obico-detections"
MAX_BYTES = 1_000_000_000
MAX_EVENTS = 10_000
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000
QUEUE_BYTES = 32 * 1024 * 1024
QUEUE_COUNT = 8
KEEP_DAYS = 15
PRIORITY = {"safe": 0, "warning": 1, "failure": 2}
_OWNED_NAME = re.compile(r"^[0-9a-f]{32}\.(?:jpg|part)$")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat()


def filename(event_id: str) -> str:
    return UUID(event_id).hex + ".jpg"


def _regular(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise ValueError("Evidence store refuses links")
    if path.exists() and not path.is_file():
        raise ValueError("Evidence store refuses special files")


def inventory(root: Path) -> dict[str, int]:
    if root.is_symlink() or root.is_junction():
        raise ValueError("Evidence store refuses a linked root")
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    ignored = 0
    for path in root.iterdir():
        owned = bool(_OWNED_NAME.fullmatch(path.name))
        if not owned and not path.is_symlink() and not path.is_junction() and path.is_dir():
            ignored += 1
            continue  # Foreign indexing directories are never traversed.
        # Never traverse a directory/link in this flat, store-owned namespace.
        _regular(path)
        if not owned:
            ignored += 1
            continue  # Finder/Explorer metadata belongs neither to quota nor GC.
        files[path.name] = path.stat().st_size
    if ignored:
        logger.info("Ignored %s foreign entries in Obico evidence storage", ignored)
    return files


def frame_info(frame: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(frame)) as image:
        width, height = image.size  # encoded SOF grid, never exif_transpose
        if image.format != "JPEG" or width * height > MAX_PIXELS or min(width, height) <= 0:
            raise ValueError("Invalid evidence image")
        orientation = int(image.getexif().get(274, 1))
        image.load()
    return width, height, orientation


def _publish(root: Path, event_id: str, frame: bytes) -> None:
    if root.is_symlink() or root.is_junction():
        raise ValueError("Evidence store refuses a linked root")
    target = root / filename(event_id)  # SEC-PATH-OK: validated UUID, fixed root.
    part = target.with_suffix(".part")
    _regular(target)
    _regular(part)
    if target.exists() or part.exists():
        raise ValueError("Evidence file already exists")
    with part.open("xb") as stream:
        stream.write(frame)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(part, target)


def _read(root: Path, name: str, size: int, digest: str) -> bytes:
    if root.is_symlink() or root.is_junction():
        raise ValueError("Evidence store refuses a linked root")
    path = root / name  # SEC-PATH-OK: only caller supplies UUID-derived filename.
    _regular(path)
    with path.open("rb") as stream:
        data = stream.read(MAX_FRAME_BYTES + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("Evidence image changed")
    return data


def _unlink(root: Path, name: str) -> None:
    if root.is_symlink() or root.is_junction():
        raise ValueError("Evidence store refuses a linked root")
    if not _OWNED_NAME.fullmatch(name):
        raise ValueError("Invalid evidence filename")
    path = root / name  # SEC-PATH-OK: fixed UUID-only flat namespace.
    _regular(path)
    path.unlink(missing_ok=True)


@dataclass(frozen=True)
class Observation:
    id: str
    printer_id: int
    token: tuple[str, int]
    observed_at: datetime
    frame: bytes
    facts_json: str
    boxes_json: str
    invalid_boxes: int
    verdict: str


class HistoryUnavailable(Exception):
    pass


class ObicoHistory:
    def __init__(self, root: Path | None = None, session_factory=None):
        self._root = root
        self._session_factory = session_factory
        self.guard = asyncio.Lock()
        self._queue: asyncio.Queue[Observation] = asyncio.Queue(maxsize=QUEUE_COUNT)
        self._worker: asyncio.Task | None = None
        self._drain: asyncio.Task | None = None
        self._epoch = secrets.token_hex(16)
        self._generations: dict[int, int] = {}
        self._blocked: Counter[int] = Counter()
        self._deletions_idle = asyncio.Event()
        self._deletions_idle.set()
        self._enabled = False
        self._held = True
        self._ready = False
        self._pins = 0
        self._queue_bytes = 0
        self._pending: set[str] = set()
        self._reserved = 0
        self._files: dict[str, int] = {}
        self._garbage_names: set[str] = set()
        self._count = 0
        self._error: str | None = None
        self.dropped: Counter = Counter()
        self.max_bytes = MAX_BYTES
        self.max_events = MAX_EVENTS
        self._cursor_secret = secrets.token_bytes(32)

    @property
    def root(self) -> Path:
        if self._root is not None:
            return self._root
        from backend.app.core.config import settings

        return settings.obico_detections_dir

    def session(self):
        return (self._session_factory or database.async_session)()

    def token(self, printer_id: int) -> tuple[str, int]:
        return self._epoch, self._generations.get(printer_id, 0)

    def printer_is_current(self, printer_id: int, token: tuple[str, int]) -> bool:
        return token[1] == self.token(printer_id)[1] and printer_id not in self._blocked

    def current(self, event: Observation) -> bool:
        return (
            not self._held
            and self._ready
            and self._enabled
            and event.printer_id not in self._blocked
            and event.token == self.token(event.printer_id)
        )

    def set_enabled(self, enabled: bool) -> None:
        if self._enabled and not enabled:
            self._epoch = secrets.token_hex(16)
        self._enabled = enabled

    async def start(self, *, enabled: bool = False) -> None:
        if self._worker is not None:
            return
        async with self.guard:
            self._held = False
            self.set_enabled(enabled)
            try:
                await self._reconcile_locked()
                self._ready = True
                self._error = None
            except Exception:
                self._error = "store_unavailable"
                logger.exception("Obico evidence reconciliation failed")
        self._worker = asyncio.create_task(self._run(), name="obico-evidence-writer")

    def offer(
        self,
        *,
        printer_id: int,
        token: tuple[str, int],
        frame: bytes,
        verdict: str,
        facts: dict,
        detections,
        event_id: str,
        observed_at: datetime,
    ) -> bool:
        """No await, file I/O, JPEG decode or unbounded task creation on inference."""
        if self._held or not self._enabled or self._worker is None:
            return False
        if not self._ready:
            self.dropped["store_unavailable"] += 1
            return False
        if verdict not in PRIORITY or token != self.token(printer_id) or printer_id in self._blocked:
            self.dropped["state_changed"] += 1
            return False
        if len(frame) > MAX_FRAME_BYTES:
            self.dropped["frame_too_large"] += 1
            return False
        if self._queue.full() or self._queue_bytes + len(frame) > QUEUE_BYTES:
            self.dropped["queue_full"] += 1
            return False
        boxes, invalid = validate_boxes(detections)
        facts_json = json.dumps(facts, allow_nan=False, separators=(",", ":"))
        boxes_json = json.dumps(boxes, allow_nan=False, separators=(",", ":"))
        if len(facts_json.encode()) + len(boxes_json.encode()) > 32 * 1024:
            self.dropped["metadata_too_large"] += 1
            return False
        UUID(event_id)
        event = Observation(event_id, printer_id, token, observed_at, frame, facts_json, boxes_json, invalid, verdict)
        self._queue.put_nowait(event)
        self._pending.add(event_id)
        self._queue_bytes += len(frame)
        return True

    async def _run(self) -> None:
        while True:
            event = None
            try:
                try:
                    event = await asyncio.wait_for(self._queue.get(), timeout=60)
                except TimeoutError:
                    async with self.guard:
                        if self._ready:
                            await self._sweep_locked()
                        else:
                            await self._reconcile_locked()
                            self._ready = True
                        if self._error != "cleanup_failed" or not self._garbage_names:
                            self._error = None
                    continue
                async with self.guard:
                    if self.current(event):
                        await self._persist_locked(event)
                if self._error != "cleanup_failed" or not self._garbage_names:
                    self._error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self._error = "write_failed"
                self.dropped["write_failed"] += 1
                logger.exception("Obico evidence recording failed")
            finally:
                if event is not None:
                    self._pending.discard(event.id)
                    self._queue_bytes -= len(event.frame)
                    self._queue.task_done()

    async def _persist_locked(self, event: Observation) -> None:
        # All writer/file lifetime belongs to this one task and guard.
        async with self.session() as db:
            if await db.get(ObicoEvent, event.id) is not None:
                return  # same observation UUID is idempotent
            if await db.get(Printer, event.printer_id) is None:
                return
        try:
            width, height, orientation = await _file_work(frame_info, event.frame)
        except (ValueError, OSError, Image.DecompressionBombError):
            self.dropped["invalid_frame"] += 1
            return
        await self._sweep_locked()
        if not await self._make_room_locked(len(event.frame), event.verdict):
            self.dropped["quota_pinned" if self._pins else "quota"] += 1
            return
        if not self.current(event):
            return
        self._reserved += len(event.frame)
        reserved = True
        facts = json.loads(event.facts_json)
        facts.update(
            {
                "profile": PROFILE,
                "ml_backend": None,
                "exif_orientation": orientation,
                "captured_at": None,
                "committed_at": None,
            }
        )
        row = ObicoEvent(
            id=event.id,
            printer_id=event.printer_id,
            observed_at=event.observed_at,
            verdict=event.verdict,
            state="writing",
            size_bytes=len(event.frame),
            sha256=hashlib.sha256(event.frame).hexdigest(),
            width=width,
            height=height,
            facts=facts,
            raw_boxes=json.loads(event.boxes_json),
            invalid_boxes=event.invalid_boxes,
        )
        inserted = False
        try:
            async with self.session() as db:
                db.add(row)
                await db.commit()
            inserted = True
            self._count += 1
            await _file_work(_publish, self.root, event.id, event.frame)
            self._files[filename(event.id)] = len(event.frame)
            self._reserved -= len(event.frame)
            reserved = False  # reservation became actual bytes, never both
            await _file_work(_read, self.root, filename(event.id), row.size_bytes, row.sha256)
            if not self.current(event):
                await self._delete_locked([event.id])
                return
            facts["committed_at"] = iso(now())
            async with self.session() as db:
                stored = await db.get(ObicoEvent, event.id)
                if stored is not None:
                    stored.state = "ready"
                    stored.facts = facts
                    await db.commit()
            if not self.current(event):
                await self._delete_locked([event.id])
        except BaseException:
            # A cancelled/failed commit may already be durable. Reconcile this
            # UUID even when the caller never observed the writing commit.
            try:
                await self._delete_locked([event.id])
                if not inserted:
                    async with self.session() as db:
                        self._count = await db.scalar(select(func.count()).select_from(ObicoEvent))
            except Exception:
                self._ready = False  # re-establish SQL/file accounting before another write
                raise
            finally:
                try:
                    # Even a failed DB cleanup cannot make a partial file free.
                    await self._refresh_event_files_locked(event.id)
                    if reserved:
                        self._reserved -= len(event.frame)
                        reserved = False
                    await self._garbage_locked()
                except Exception:
                    self._ready = False
                    raise
            raise
        finally:
            if reserved:
                self._reserved -= len(event.frame)

    async def _refresh_event_files_locked(self, event_id: str) -> None:
        def sizes():
            result = {}
            for suffix in (".jpg", ".part"):
                name = UUID(event_id).hex + suffix
                path = self.root / name  # SEC-PATH-OK: UUID-only fixed suffix.
                _regular(path)
                if path.exists():
                    result[name] = path.stat().st_size
            return result

        self._files.update(await _file_work(sizes))

    async def _delete_locked(self, ids: list[str]) -> bool:
        if not ids:
            return True
        async with self.session() as db:
            result = await db.execute(delete(ObicoEvent).where(ObicoEvent.id.in_(ids)))
            await db.commit()  # logical deletion precedes pin check/file work
            self._count = max(0, self._count - result.rowcount)
        removed = True
        for event_id in ids:
            for suffix in (".jpg", ".part"):
                name = UUID(event_id).hex + suffix
                self._garbage_names.add(name)
                if not await self._unlink_locked(name):
                    removed = False
        return removed

    async def _unlink_locked(self, name: str) -> bool:
        # P1: callers keep guard through BOTH pin check AND actual unlink.
        if self._pins:
            return False
        try:
            await _file_work(_unlink, self.root, name)
        except OSError:
            self._error = "cleanup_failed"
            return False
        self._files.pop(name, None)
        self._garbage_names.discard(name)
        return True

    async def _garbage_locked(self) -> None:
        for name in list(self._garbage_names):
            await self._unlink_locked(name)
        if not self._garbage_names and self._error == "cleanup_failed":
            self._error = None

    async def _sweep_locked(self) -> None:
        async with self.session() as db:
            ids = (
                await db.scalars(
                    select(ObicoEvent.id).where(ObicoEvent.observed_at < now() - timedelta(days=KEEP_DAYS))
                )
            ).all()
        await self._delete_locked(list(ids))
        if not self._pins:
            await self._garbage_locked()

    async def _make_room_locked(self, size: int, verdict: str) -> bool:
        if size > self.max_bytes:
            return False
        needed = int(size > 0)
        if (
            self._count + needed <= self.max_events
            and sum(self._files.values()) + self._reserved + size <= self.max_bytes
        ):
            return True
        # Do not delete metadata if pinned bytes cannot free the required space.
        if self._pins:
            return False
        for victim_class in PRIORITY:
            if PRIORITY[victim_class] > PRIORITY[verdict]:
                break
            async with self.session() as db:
                victims = (
                    await db.scalars(
                        select(ObicoEvent.id)
                        .where(ObicoEvent.verdict == victim_class)
                        .order_by(ObicoEvent.observed_at, ObicoEvent.id)
                    )
                ).all()
            for victim in victims:
                if not await self._delete_locked([victim]):
                    return False  # do not discard more evidence for unfreeable bytes
                if (
                    self._count + needed <= self.max_events
                    and sum(self._files.values()) + self._reserved + size <= self.max_bytes
                ):
                    return True
        return False

    async def _reconcile_locked(self) -> None:
        self._files = await _file_work(inventory, self.root)
        async with self.session() as db:
            rows = (await db.scalars(select(ObicoEvent))).all()
        self._count = len(rows)
        referenced = {filename(row.id) for row in rows if row.state == "ready"}
        self._garbage_names = set(self._files) - referenced
        for row in rows:
            if row.state == "writing" or row.state == "deleting":
                await self._delete_locked([row.id])
            elif row.state == "ready":
                # Inventory already checked existence and size. Hash lazily on
                # image read/backup/restore, not for the entire store at boot.
                # An inventory I/O error aborts reconciliation without deleting
                # or retiring intact originals; the worker retries it later.
                if self._files.get(filename(row.id)) != row.size_bytes:
                    async with self.session() as db:
                        stored = await db.get(ObicoEvent, row.id)
                        stored.state = "unavailable"
                        await db.commit()
                    self._garbage_names.add(filename(row.id))
        await self._sweep_locked()
        await self._make_room_locked(0, "failure")

    @asynccontextmanager
    async def pin_backup(self):
        async with self.guard:
            if self._held and self._worker is not None:
                raise HistoryUnavailable("Evidence storage is held for restore")
            self._pins += 1
        try:
            yield
        finally:
            async with self.guard:
                self._pins -= 1
                if not self._pins:
                    await self._garbage_locked()

    @asynccontextmanager
    async def printer_deletion(self, printer_id: int):
        if self._held and self._worker is not None:
            raise HistoryUnavailable("Evidence storage is held for restore")
        self._generations[printer_id] = self._generations.get(printer_id, 0) + 1
        self._blocked[printer_id] += 1
        self._deletions_idle.clear()
        try:
            async with self.guard, self.session() as db:
                before = set((await db.scalars(select(ObicoEvent.id).where(ObicoEvent.printer_id == printer_id))).all())
            try:
                yield  # archive deletion, task stops and VP sync do not own guard
            finally:
                async with self.guard:
                    # Rollback leaves rows readable. Only committed orphans go.
                    async with self.session() as db:
                        self._count = await db.scalar(select(func.count()).select_from(ObicoEvent))
                        remaining = set(
                            (await db.scalars(select(ObicoEvent.id).where(ObicoEvent.printer_id == printer_id))).all()
                        )
                    for event_id in before - remaining:
                        self._garbage_names.update({filename(event_id), UUID(event_id).hex + ".part"})
                    await self._garbage_locked()
        finally:
            self._blocked[printer_id] -= 1
            if not self._blocked[printer_id]:
                del self._blocked[printer_id]
            if not self._blocked:
                self._deletions_idle.set()

    @asynccontextmanager
    async def reading(self):
        if self._held or not self._ready:
            raise HistoryUnavailable("Evidence storage is held for restore")
        async with self.guard:
            if self._held or not self._ready:
                raise HistoryUnavailable("Evidence storage is held for restore")
            yield

    async def clear(self) -> int:
        self._epoch = secrets.token_hex(16)
        async with self.reading():
            async with self.session() as db:
                ids = list((await db.scalars(select(ObicoEvent.id))).all())
            await self._delete_locked(ids)
            await self._garbage_locked()
            return len(ids)

    def status(self) -> dict:
        return {
            "state": "held" if self._held else ("error" if self._error else "ready"),
            "reason": self._error,
            "dropped": dict(self.dropped),
            "used_bytes": sum(self._files.values()) + self._reserved,
            "max_bytes": self.max_bytes,
            "max_events": self.max_events,
            "max_days": KEEP_DAYS,
        }

    async def _stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        # Delete bodies no longer hold guard. Their post-commit cleanup still
        # owns storage and must finish before restore replaces the database/root.
        await self._deletions_idle.wait()
        async with self.guard:  # drains owned file readers/deletion/backup release
            while not self._queue.empty():
                event = self._queue.get_nowait()
                self._pending.discard(event.id)
                self._queue_bytes -= len(event.frame)
                self._queue.task_done()

    async def quiesce(self, timeout: float = 10) -> bool:
        self._held = True
        self._epoch = secrets.token_hex(16)
        if self._drain is None:
            self._drain = asyncio.create_task(self._stop(), name="obico-evidence-drain")
        if self._drain.done():
            self._drain.result()
            return True
        done, _ = await asyncio.wait({self._drain}, timeout=timeout)
        if not done:
            return False
        self._drain.result()
        return True

    async def shutdown(self) -> None:
        await self.quiesce()
        if self._drain is not None:
            await asyncio.shield(self._drain)

    def _cursor(self, payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.digest(self._cursor_secret, raw, "sha256")
        return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")

    def _parse_cursor(self, cursor: str, context: str) -> dict:
        try:
            if len(cursor) > 2048:
                raise ValueError
            value = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            signature, raw = value[:32], value[32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_secret, raw, "sha256")):
                raise ValueError
            payload = json.loads(raw)
            if payload["epoch"] != self._epoch or payload["context"] != context or payload["expires"] < time.time():
                raise ValueError
            UUID(payload["id"])
            datetime.fromisoformat(payload["time"])
            return payload
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid or expired history cursor") from exc

    async def page(
        self,
        *,
        scope: frozenset[int] | None,
        printer_id: int | None = None,
        verdict: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
        audience: str = "",
    ) -> dict:
        filters = []
        if scope is not None:
            filters.append(ObicoEvent.printer_id.in_(scope))
        if printer_id is not None:
            filters.append(ObicoEvent.printer_id == printer_id)
        if verdict is not None:
            filters.append(ObicoEvent.verdict == verdict)
        if since is not None:
            filters.append(ObicoEvent.observed_at >= since)
        if until is not None:
            filters.append(ObicoEvent.observed_at < until)
        context = hashlib.sha256(
            json.dumps(
                [audience, sorted(scope) if scope is not None else None, printer_id, verdict, str(since), str(until)],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        async with self.reading():
            async with self.session() as db:
                count, oldest = (
                    await db.execute(
                        select(func.count(), func.min(ObicoEvent.observed_at)).where(
                            *filters, ObicoEvent.state == "ready"
                        )
                    )
                ).one()
                total = await db.scalar(select(func.count()).select_from(ObicoEvent).where(*filters))
                page_filters = list(filters)
                if cursor:
                    previous = self._parse_cursor(cursor, context)
                    timestamp = datetime.fromisoformat(previous["time"])
                    page_filters.append(
                        or_(
                            ObicoEvent.observed_at < timestamp,
                            and_(ObicoEvent.observed_at == timestamp, ObicoEvent.id < previous["id"]),
                        )
                    )
                rows = list(
                    (
                        await db.scalars(
                            select(ObicoEvent)
                            .where(*page_filters)
                            .order_by(ObicoEvent.observed_at.desc(), ObicoEvent.id.desc())
                            .limit(limit + 1)
                        )
                    ).all()
                )
            more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = None
            if more:
                tail = rows[-1]
                next_cursor = self._cursor(
                    {
                        "epoch": self._epoch,
                        "context": context,
                        "expires": time.time() + 600,
                        "id": tail.id,
                        "time": tail.observed_at.isoformat(),
                    }
                )
            return {
                "items": [summary(row) for row in rows],
                "next_cursor": next_cursor,
                "saved_count": count,
                "total_count": total,
                "oldest_saved_at": iso(oldest) if oldest else None,
            }

    async def details(self, printer_id: int, event_id: str, *, image: bool = False):
        async with self.reading():
            async with self.session() as db:
                row = await db.get(ObicoEvent, event_id)
                if row is None or row.printer_id != printer_id:
                    return None
            if image:
                if row.state != "ready":
                    return None
                try:
                    return await _file_work(_read, self.root, filename(row.id), row.size_bytes, row.sha256)
                except (OSError, ValueError):
                    return None
            return {
                **summary(row),
                "facts": row.facts,
                "width": row.width,
                "height": row.height,
                "profile": PROFILE,
                "raw_boxes": row.raw_boxes,
                "boxes": project_boxes(row.raw_boxes, row.width, row.height),
                "invalid_boxes": row.invalid_boxes,
            }

    async def recent_states(self, ids: list[str]) -> dict[str, str]:
        async with self.reading(), self.session() as db:
            states = dict(
                (await db.execute(select(ObicoEvent.id, ObicoEvent.state).where(ObicoEvent.id.in_(ids[:50])))).all()
            )
            for event_id in ids[:50]:
                if event_id in self._pending:
                    states.setdefault(event_id, "writing")
            return states


def summary(row: ObicoEvent) -> dict:
    return {
        "id": row.id,
        "printer_id": row.printer_id,
        "timestamp": iso(row.observed_at),
        "class": row.verdict,
        "state": row.state,
        "task_name": row.facts.get("task_name", ""),
        "score": row.facts.get("score", 0),
        "current_p": row.facts.get("current_p", 0),
        "detections": len(row.raw_boxes),
    }


obico_history = ObicoHistory()
