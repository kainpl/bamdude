"""Durable installation startup pause, independent of printer reconnects.

Only new work admission consumes this latch. Completion, plate swaps, macros
of running prints and typed operator controls retain their release behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from backend.app.services.filament_routing import RoutingDeferred
from backend.app.services.render_runtime import _try_lock, _unlock

logger = logging.getLogger(__name__)
_command_tasks: set[asyncio.Future] = set()


class FarmStartupRefused(RoutingDeferred):
    disposition = "retry_later"


def _now():
    return datetime.now(timezone.utc).isoformat()


async def drain_commands():
    from backend.app.services.preview_artifacts import owned

    if _command_tasks:
        await owned(asyncio.gather(*tuple(_command_tasks), return_exceptions=True))


async def notify_change():
    from backend.app.core.websocket import ws_manager

    try:
        await ws_manager.broadcast({"type": "farm_startup_changed"})
    except Exception:
        logger.warning("Farm startup: state invalidation failed")


def track_command_io(future):
    """A timed-out FTP executor still owns its wire until the thread ends."""
    if farm_startup_guard.active:
        _command_tasks.add(future)
        future.add_done_callback(_command_tasks.discard)
    return future


class FarmStartupGuard:
    def __init__(self):
        self._mutex = threading.RLock()
        self._lock_file = None
        self.path: Path | None = None
        self.state: dict = {}
        self.active = False
        self.ready = False
        self.stopping = False
        self.storage_error = False
        self.readiness_failed = False
        self.restart_required = False

    def _write(self, state):
        fd, filename = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=self.path.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _valid(value):
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            return False
        if value.get("lifecycle") not in ("running", "clean") or not isinstance(value.get("boot_id"), str):
            return False
        if "hold" not in value:
            return False
        reasons = {"unclean_shutdown", "previous_run_unknown", "restore"}
        try:
            UUID(value["boot_id"])
            hold = value["hold"]
            if hold is not None:
                if not isinstance(hold, dict) or hold.get("reason") not in reasons:
                    return False
                UUID(hold["id"])
                if datetime.fromisoformat(hold["since"]).tzinfo is None:
                    return False
            ack = value.get("last_ack")
            if ack is not None:
                if (
                    not isinstance(ack, dict)
                    or ack.get("reason") not in reasons
                    or not isinstance(ack.get("actor"), str)
                ):
                    return False
                UUID(ack["boot_id"])
                UUID(ack["hold_id"])
                if datetime.fromisoformat(ack["at"]).tzinfo is None:
                    return False
            return True
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def start(self, data_dir: Path):
        with self._mutex:
            if self._lock_file is not None:
                raise FarmStartupRefused("owner_alive")
            self.active = True
            self.ready = False
            self.stopping = False
            self.storage_error = False
            self.readiness_failed = False
            self.restart_required = False
            self.path = Path(data_dir) / "runtime-state" / "farm-startup" / "state.json"
            try:
                for directory in (
                    self.path.parent.parent,
                    self.path.parent,
                    self.path,
                    self.path.parent / "owner.lock",
                ):
                    if directory.is_symlink() or directory.is_junction():
                        raise OSError("operational state must not follow links")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                handle = (self.path.parent / "owner.lock").open("a+b")
                try:
                    if not _try_lock(handle):
                        raise FarmStartupRefused("owner_alive")
                except BaseException:
                    handle.close()
                    raise
                self._lock_file = handle
                try:
                    previous = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    previous = None
                valid = self._valid(previous)
                hold = previous.get("hold") if valid else None
                if hold is None and (not valid or previous["lifecycle"] != "clean"):
                    hold = {
                        "id": str(uuid4()),
                        "since": _now(),
                        "reason": "unclean_shutdown" if valid else "previous_run_unknown",
                    }
                state = {
                    "version": 1,
                    "boot_id": str(uuid4()),
                    "lifecycle": "running",
                    "hold": hold,
                    "last_ack": previous.get("last_ack") if valid else None,
                }
                self._write(state)
                self.state = state
                logger.info("Farm startup: state=%s boot=%s", "held" if hold else "starting", state["boot_id"])
            except FarmStartupRefused:
                raise
            except OSError:
                self.storage_error = True
                logger.error("Farm startup: storage_unavailable; automation remains closed")

    def admit_new_installation(self, *, new_empty_store):
        """Only the schema creator can positively prove an empty first install."""
        with self._mutex:
            hold = self.state.get("hold") or {}
            if (
                new_empty_store is not True
                or hold.get("reason") != "previous_run_unknown"
                or self.state.get("last_ack")
                or self.storage_error
                or self.stopping
                or self.restart_required
                or self.ready
                or self.readiness_failed
            ):
                return False
            state = {**self.state, "hold": None}
            try:
                self._write(state)
            except OSError:
                self.storage_error = True
                return False
            self.state = state
            logger.info("Farm startup: new empty installation; technical readiness still pending")
            return True

    def mark_ready(self):
        with self._mutex:
            if not self.stopping and not self.storage_error and not self.readiness_failed:
                self.ready = True

    def close_admission(self):
        with self._mutex:
            self.stopping = True
            self.ready = False

    def boot_id(self) -> str | None:
        """This process's boot, only while it holds the installation's owner lock.

        A dispatch stamps it on its execution archive; a later process that holds the
        same lock and reads another boot there knows that attempt's owner has ended.
        None when the guard is not running, owns nothing or could not record its state —
        then nothing is proven.
        """
        with self._mutex:
            if self._lock_file is None or self.storage_error:
                return None
            boot = self.state.get("boot_id")
            return boot if isinstance(boot, str) else None

    def allowed(self):
        with self._mutex:
            return not self.active or not (
                not self.ready
                or self.stopping
                or self.storage_error
                or self.state.get("hold")
                or self.readiness_failed
                or self.restart_required
            )

    def require(self):
        if not self.allowed():
            raise FarmStartupRefused("farm_recovery_hold")

    def hold(self, reason):
        with self._mutex:
            if not self.active:
                return
            if reason == "restore":
                self.restart_required = True
            state = deepcopy(self.state)
            # A new restore is a new review boundary, even when another hold
            # already exists. A dialog opened before it cannot release it.
            state["hold"] = {"id": str(uuid4()), "since": _now(), "reason": reason}
            try:
                self._write(state)
            except OSError as exc:
                self.storage_error = True
                raise FarmStartupRefused("storage_unavailable") from exc
            self.state = state

    def status(self):
        with self._mutex:
            hold = self.state.get("hold") or {}
            state = (
                "storage_unavailable"
                if self.storage_error
                else (
                    "stopping"
                    if self.stopping
                    else "restart_required"
                    if self.restart_required
                    else "readiness_failed"
                    if self.readiness_failed
                    else "starting"
                    if self.active and not self.ready
                    else "held"
                    if hold
                    else "running"
                )
            )
            return {
                "state": state,
                "boot_id": self.state.get("boot_id"),
                "hold_id": hold.get("id"),
                "reason": hold.get("reason"),
                "since": hold.get("since"),
                "ready": self.ready,
                "can_resume": self.ready
                and bool(hold)
                and not (self.storage_error or self.stopping or self.restart_required or self.readiness_failed),
            }

    def resume(self, boot_id, hold_id, *, actor):
        with self._mutex:
            if self.storage_error:
                raise FarmStartupRefused("storage_unavailable")
            if not self.ready or self.stopping or self.readiness_failed:
                raise FarmStartupRefused("not_ready")
            if boot_id != self.state.get("boot_id"):
                raise FarmStartupRefused("stale")
            current = self.state.get("hold")
            if current is None:
                ack = self.state.get("last_ack") or {}
                if hold_id and ack.get("boot_id") == boot_id and ack.get("hold_id") == hold_id:
                    return "already_released"
                raise FarmStartupRefused("stale")
            if hold_id != current["id"]:
                raise FarmStartupRefused("stale")
            if self.restart_required:
                raise FarmStartupRefused("restart_required")
            state = deepcopy(self.state)
            state["hold"] = None
            state["last_ack"] = {
                "actor": actor,
                "at": _now(),
                "boot_id": boot_id,
                "hold_id": hold_id,
                "reason": current["reason"],
            }
            try:
                self._write(state)
            except OSError as exc:
                raise FarmStartupRefused("storage_unavailable") from exc
            self.state = state
            logger.info("Farm startup: released boot=%s hold=%s actor=%s", boot_id, hold_id, actor)
            return "released"

    def finish(self, *, clean):
        with self._mutex:
            self.close_admission()
            try:
                if clean and not self.storage_error and not self.readiness_failed and self.state:
                    state = {**self.state, "lifecycle": "clean"}
                    self._write(state)
                    self.state = state
            finally:
                if self._lock_file is not None:
                    handle = self._lock_file
                    self._lock_file = None
                    try:
                        _unlock(handle)
                    finally:
                        handle.close()


farm_startup_guard = FarmStartupGuard()


class FarmReadyMiddleware:
    """Open the technical barrier only after the ASGI server receives startup.complete."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "lifespan":
            return await self.app(scope, receive, send)

        async def ready_send(message):
            await send(message)
            if message["type"] == "lifespan.startup.complete":
                farm_startup_guard.mark_ready()
                from backend.app.core.auth_runtime import auth_runtime

                auth_runtime.open_admission()

        return await self.app(scope, receive, ready_send)
