"""Record a printer's MQTT traffic to a file, for as long as it is asked to.

Watching MQTT used to mean running ``scripts/mqtt_sniffer.py`` in a terminal and
keeping the window open — close it and the evidence stops arriving. That is the
wrong shape for the faults it catches, which are the intermittent ones nobody is
sitting and watching for.

⚠️ **This tees the connection BamDude already holds.** It registers on
``BambuMQTTClient``'s raw fan-out — the same extension point the VP MQTT bridge
uses — and never opens a session of its own. A second session to a printer that
is already connected is what made generating a support bundle disturb the whole
farm; see the branch-order comment in ``printer_diagnostic``.

⚠️ **The raw handler runs on paho's network thread and must not block.** It
appends to a queue and returns; a daemon thread does the writing. A disk write
on that thread stalls message ingest for every printer sharing it — the same
reasoning as ``services/measurement_buffer.py``.

**Nothing caps the size,** deliberately: recording runs until stopped, because a
cap that trips just before the fault reproduces is worse than a large file. What
keeps it honest is visibility — the printer card shows the badge and the current
size, so a forgotten recording is something you see rather than something you
discover when the disk fills.

Files are written raw. They are the operator's own copy of their own farm, and
redacting the serial would make two machines impossible to tell apart. They are
NOT swept into the support bundle: that is opt-in per printer, and the control
that offers it says what it adds.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Bounded so a stalled writer cannot grow without limit. Dropping is the right
# failure here: a recorder must never become backpressure on the client that
# feeds every other feature.
_QUEUE_MAX = 10_000
# How much of the tail to read for a screenful. Generous enough that a few
# hundred lines always fit, small enough that the size of the file does not
# matter to whoever is reading it.
_TAIL_BYTES = 1_000_000


class MQTTRecorder:
    """One writer thread; one file per printer per day."""

    def __init__(self, log_dir: Path | None = None, printer_manager=None):
        self._log_dir = log_dir
        self._manager = printer_manager
        self._handlers: dict[int, object] = {}
        self._publish_handlers: dict[int, object] = {}
        # The client each handler is attached to, so a rebuilt session is
        # detected rather than assumed away. See start().
        self._clients: dict[int, object] = {}
        self._files: dict[int, Path] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._writer: threading.Thread | None = None
        self._stop_writer = threading.Event()
        # Test hook: lets a test prove the raw handler returns without waiting
        # for the disk.
        self._writer_paused = threading.Event()

    @property
    def log_dir(self) -> Path:
        if self._log_dir is not None:
            return self._log_dir
        from backend.app.core.config import settings

        return settings.log_dir

    def _get_client(self, printer_id: int):
        if self._manager is not None:
            return self._manager.get_client(printer_id)
        from backend.app.services.printer_manager import printer_manager

        return printer_manager.get_client(printer_id)

    def start(self, printer_id: int) -> Path:
        """Begin recording, or re-attach if the client underneath has changed.

        ⚠️ **Re-attaching is not an optimisation, it is the feature working at
        all.** ``connection_watchdog`` rebuilds a stalled MQTT session by
        creating a NEW ``BambuMQTTClient``, and ``ensure_fresh_connection*``
        does the same on the dispatch path. The handler registered on the old
        client dies with it, so a recording that was asked to run "until
        stopped" would stop at the first reconnect — silently, with the badge
        still showing and the file simply never growing again. Same trap that
        blanked the skip-objects list on reconnect.

        Calling this repeatedly is therefore correct and cheap: it is how a
        recording survives the farm.
        """
        client = self._get_client(printer_id)
        if client is None:
            raise RuntimeError(f"printer {printer_id} has no live MQTT client to record")

        if printer_id in self._handlers:
            if self._clients.get(printer_id) is client:
                return self._files[printer_id]
            # The session was rebuilt underneath us. Drop the dead handle and
            # re-register on the live client, keeping the same file.
            self._handlers.pop(printer_id, None)
            self._publish_handlers.pop(printer_id, None)
            logger.info("MQTT recording re-attached for printer %s after its session was rebuilt", printer_id)

        # Same file across a re-attach: a rebuilt session is the same recording
        # to the operator, and a second file per reconnect would shred it.
        path = self._files.get(printer_id)
        if path is None:
            directory = self.log_dir / "mqtt"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            path = directory / f"mqtt-{stamp}-{printer_id}.log"

        # ⚠️ The request topic carries every command travelling TO the printer —
        # BambuStudio's included, because the broker echoes them to us. Arriving
        # on our socket makes them inbound, but where they are GOING is what a
        # reader needs, so they are filed "out" beside our own commands. This is
        # the only way "what does Studio actually put in that command?" can be
        # answered from an operator's own capture.
        request_topic = getattr(client, "topic_publish", None)

        def _handler(topic: str, payload: bytes, _pid=printer_id) -> None:
            # paho's network thread. Enqueue and return; never write here.
            direction = "out" if request_topic and topic == request_topic else "in"
            try:
                self._queue.put_nowait((_pid, time.time(), direction, topic, payload))
            except queue.Full:
                pass

        def _sent(topic: str, payload: bytes, _pid=printer_id) -> None:
            try:
                self._queue.put_nowait((_pid, time.time(), "out", topic, payload))
            except queue.Full:
                pass

        self._files[printer_id] = path
        self._handlers[printer_id] = _handler
        self._publish_handlers[printer_id] = _sent
        self._clients[printer_id] = client
        client.register_raw_message_handler(_handler)
        # Both halves, or the transcript cannot be read. The case that proved it:
        # an external-slot assignment the printer answered "success", then wiped
        # with a delta one message later - diagnosing it needed what we asked for
        # as well as what came back.
        client.register_raw_publish_handler(_sent)
        self._ensure_writer()
        logger.info("MQTT recording started for printer %s -> %s", printer_id, path)
        return path

    def stop(self, printer_id: int) -> None:
        handler = self._handlers.pop(printer_id, None)
        sent = self._publish_handlers.pop(printer_id, None)
        if handler is None:
            return
        client = self._get_client(printer_id)
        if client is not None:
            try:
                client.unregister_raw_message_handler(handler)
            except Exception:
                logger.debug("unregister failed for printer %s", printer_id, exc_info=True)
            if sent is not None:
                try:
                    client.unregister_raw_publish_handler(sent)
                except Exception:
                    logger.debug("publish unregister failed for printer %s", printer_id, exc_info=True)
        self._files.pop(printer_id, None)
        self._clients.pop(printer_id, None)
        logger.info("MQTT recording stopped for printer %s", printer_id)

    def is_recording(self, printer_id: int) -> bool:
        return printer_id in self._handlers

    def file_for(self, printer_id: int) -> Path | None:
        return self._files.get(printer_id)

    def size_bytes(self, printer_id: int) -> int:
        """Size of the RUNNING recording, or 0.

        ⚠️ Deliberately not the file on disk: the printer card shows this beside
        the badge, and a number next to no badge would read as a recording that
        is still going. Whoever wants the stored file's size asks
        :meth:`size_on_disk`.
        """
        path = self._files.get(printer_id)
        if path is None or not path.exists():
            return 0
        return path.stat().st_size

    def size_on_disk(self, printer_id: int) -> int:
        """Total size of this printer's stored recordings, running or not."""
        total = 0
        for path in self.paths_for(printer_id):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def paths_for(self, printer_id: int) -> list[Path]:
        """Every recording on disk for this printer, oldest first.

        ⚠️ Not ``file_for``: that only knows a path while the recording runs, and
        a stopped recording is exactly the one somebody wants to read. Lookup
        goes by the naming convention instead, which is why the printer id is
        the last segment of the name.
        """
        directory = self.log_dir / "mqtt"
        if not directory.is_dir():
            return []
        return sorted(directory.glob(f"mqtt-*-{printer_id}.log"))

    def tail(self, printer_id: int, limit: int = 500) -> list[dict]:
        """The last ``limit`` recorded messages, newest last.

        ⚠️ Reads only the end of the file. Nothing caps a recording's size, so
        loading it whole to show a screenful is how the debugging aid becomes
        the thing that falls over.

        A line whose payload is not JSON comes back as the raw string rather
        than being dropped — a truncated last line (the writer appends while
        this reads) must not blank the view.
        """
        paths = self.paths_for(printer_id)
        if not paths:
            return []
        path = paths[-1]
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - _TAIL_BYTES))
                chunk = fh.read()
        except OSError:
            return []

        text = chunk.decode("utf-8", "replace")
        if len(chunk) == _TAIL_BYTES and "\n" in text:
            # The window almost certainly cut the first line in half.
            text = text.split("\n", 1)[1]

        entries: list[dict] = []
        for line in text.splitlines()[-limit:]:
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            stamp, direction, topic, raw = parts
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw
            entries.append({"timestamp": stamp, "direction": direction, "topic": topic, "payload": payload})
        return entries

    def prune(self, days: int) -> int:
        """Drop recordings older than ``days``; returns how many went.

        Shares ``log_retention_days`` with the application log rather than
        carrying a setting of its own — one knob, one mental model. Recordings
        cannot join the rotating handler itself: the writer runs on its own
        thread, deliberately outside ``logging``, so it never blocks paho's
        network thread. What they share is when the answer is applied, which is
        why this hangs off ``core.logging_state.update_log_retention``.

        ⚠️ **Never removes a file a recording is currently writing.** A capture
        started before the cutoff is still running, and deleting it under the
        writer throws away exactly what somebody is sitting and waiting for —
        while the badge goes on saying it is being recorded.

        ⚠️ ``days <= 0`` keeps everything. The setting is clamped elsewhere, but
        a zero arriving here must not read as "delete today's too".
        """
        if days <= 0:
            return 0

        live = set(self._files.values())
        cutoff = time.time() - days * 86400
        directory = self.log_dir / "mqtt"
        if not directory.is_dir():
            return 0

        removed = 0
        # Only our own naming — the folder sits inside the operator's log
        # directory and is not ours to sweep wholesale.
        for path in directory.glob("mqtt-*-*.log"):
            if path in live:
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                logger.debug("could not prune recording %s", path, exc_info=True)
        if removed:
            logger.info("Pruned %d MQTT recording(s) older than %d days", removed, days)
        return removed

    def delete(self, printer_id: int) -> int:
        """Remove this printer's recordings; returns how many files went.

        ⚠️ Does NOT stop an active recording — clearing means "start the
        transcript over", and the badge stays on. Stopping here would leave the
        badge lying about what is happening. The writer recreates the file on
        its next append.
        """
        removed = 0
        for path in self.paths_for(printer_id):
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.debug("could not remove recording %s", path, exc_info=True)
        return removed

    def recording_printer_ids(self) -> list[int]:
        return sorted(self._handlers)

    def _ensure_writer(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            return
        self._stop_writer.clear()
        self._writer = threading.Thread(target=self._drain, name="mqtt-recorder", daemon=True)
        self._writer.start()

    def _drain(self) -> None:
        while not self._stop_writer.is_set():
            if self._writer_paused.is_set():
                time.sleep(0.01)
                continue
            try:
                printer_id, ts, direction, topic, payload = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            path = self._files.get(printer_id)
            if path is None:
                # Stopped between enqueue and write. Dropping is correct: the
                # operator asked for it to stop.
                continue
            try:
                stamp = datetime.fromtimestamp(ts, timezone.utc).isoformat()
                line = payload.decode("utf-8", "replace")
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{stamp}\t{direction}\t{topic}\t{line}\n")
            except Exception:
                logger.debug("MQTT recorder write failed for printer %s", printer_id, exc_info=True)


mqtt_recorder = MQTTRecorder()


async def resume_recordings() -> None:
    """Restart every recording the database says should be running.

    Called from the lifespan after printers connect — there has to be a client
    to tee. A printer still offline is skipped and picked up the next time this
    runs, so the failure mode is "starts late", never "starts a session of its
    own".
    """
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

    async with async_session() as db:
        ids = [row[0] for row in (await db.execute(select(Printer.id).where(Printer.mqtt_recording.is_(True)))).all()]

    for printer_id in ids:
        try:
            mqtt_recorder.start(printer_id)
        except Exception:
            logger.warning("Could not resume MQTT recording for printer %s", printer_id, exc_info=True)
