"""Local NATS part-render worker (spec §7; plan E3, task 17).

One attempt at a time, each in a disposable ``part_render`` child under its own guardian. The worker never
reads the source: the task's path goes to the child untouched, so a hung mount freezes the child alone.
An attempt ends when its whole tree -- guardian, part_render, Node -- is proven gone
(``PreviewProcess.stop`` and the recorded pids, part_render_tree); only then does the worker answer.
Unproven ownership makes the worker uncertain: every later run is refused with "unavailable", and the
runtime retires it.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

import nats

from backend.app.services.analysis_transport import describe, put
from backend.app.services.part_render_protocol import ATTEMPT_BYTES, RSS_BYTES
from backend.app.services.part_render_tree import launch, record, tree_gone
from backend.app.services.preview_artifacts import disk, owned
from backend.app.services.preview_process import PreviewProcess, SpawnUnproven
from backend.app.services.preview_protocol import CONTROL_SECONDS, JSON_BYTES, PreviewError, decode, encode, remaining
from backend.app.services.worker_staging import cleanup_owned

_HEX = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMAND_KEYS = {"version", "generation", "epoch", "attempt_id", "sequence", "operation", "deadline_ns", "task", "node"}
_TASK_KEYS = {"path", "root", "kind", "sha256", "size", "plate_index"}
_OPERATIONS = {"ready", "run", "status", "cancel", "shutdown"}
_RESULT_OUTCOMES = {"ok", "failed", "unavailable"}
_POLL_SECONDS = 0.05
_TRANSFER_SECONDS = 60
_STAGING_SLACK = 4 * 1024 * 1024  # result.json, the pid records and the pack's own header


class _Ended(Exception):
    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


def _valid_task(task) -> bool:
    """Lexical only: this process never touches the path (plan E3, Global Constraints)."""
    if not isinstance(task, dict) or set(task) != _TASK_KEYS:
        return False
    for key in ("path", "root"):
        value = task[key]
        if not isinstance(value, str) or not 0 < len(value) <= 4096 or not Path(value).is_absolute():
            return False
    return (
        task["kind"] in ("3mf", "gcode")
        and isinstance(task["sha256"], str)
        and _SHA256.fullmatch(task["sha256"]) is not None
        and type(task["size"]) is int
        and task["size"] >= 0
        and type(task["plate_index"]) is int
        and task["plate_index"] >= 0
    )


def _valid_node(node) -> bool:
    return node is None or (isinstance(node, str) and 0 < len(node) <= 4096 and Path(node).is_absolute())


def _read_result(root: Path) -> dict | None:
    path = root / "result.json"
    try:
        if path.stat().st_size > JSON_BYTES:
            return None
        result = decode(path.read_bytes())
    except (OSError, PreviewError, ValueError):
        return None
    reason = result.get("reason")
    if result.get("outcome") not in _RESULT_OUTCOMES or not (reason is None or isinstance(reason, str)):
        return None
    return result


def _bytes_under(root: Path) -> int:
    return sum(entry.stat().st_size for entry in root.iterdir() if entry.is_file())


class Service:
    def __init__(self, config: dict):
        self.config = config
        self.subject = f"bamdude.partrender.{config['generation']}.{config['epoch']}"
        self.staging = Path(config["staging"])
        self.cache = self.staging / "cache"  # retained_entries / abandoned_attempts know this name
        self.child: PreviewProcess | None = None
        self.active: dict | None = None
        self.active_task: asyncio.Task | None = None
        self.highwater = 0
        self.terminals: dict[int, tuple[dict, dict]] = {}
        self.stopping = asyncio.Event()
        self.handlers: set[asyncio.Task] = set()
        self.uncertain = False
        self.store = None

    async def start(self):
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.nc = await nats.connect(
            self.config["url"],
            token=self.config["token"],
            allow_reconnect=False,
            connect_timeout=CONTROL_SECONDS,
            closed_cb=self.disconnected,
        )
        self.store = await self.nc.jetstream(timeout=CONTROL_SECONDS).object_store(self.config["bucket"])
        await self.nc.subscribe(self.subject, cb=self.receive)
        await self.nc.flush(timeout=CONTROL_SECONDS)

    async def disconnected(self):
        self.stopping.set()
        if self.active_task:
            self.active_task.cancel()

    async def receive(self, message):
        if len(self.handlers) >= 16:
            await message.respond(encode({"outcome": "busy"}))
            return
        task = asyncio.create_task(self.handle(message))
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    def parse(self, raw: bytes) -> dict:
        data = decode(raw)
        if set(data) != _COMMAND_KEYS:
            raise PreviewError("protocol_error")
        if (
            type(data["version"]) is not int
            or data["version"] != 1
            or data["generation"] != self.config["generation"]
            or data["epoch"] != self.config["epoch"]
            or not isinstance(data["attempt_id"], str)
            or not _HEX.fullmatch(data["attempt_id"])
            or type(data["sequence"]) is not int
            or data["sequence"] < 1
            or type(data["operation"]) is not str
            or data["operation"] not in _OPERATIONS
            or type(data["deadline_ns"]) is not int
        ):
            raise PreviewError("protocol_error")
        if data["operation"] in ("run", "cancel"):
            if not _valid_task(data["task"]) or not _valid_node(data["node"]):
                raise PreviewError("protocol_error")
        elif data["task"] is not None or data["node"] is not None:
            raise PreviewError("protocol_error")
        return data

    async def handle(self, message):
        try:
            result = await self.command(self.parse(message.data))
        except PreviewError as exc:
            result = {"outcome": exc.outcome}
        except Exception:
            result = {"outcome": "unavailable"}
        try:
            await message.respond(encode(result))
        except Exception:
            pass

    async def command(self, command: dict) -> dict:
        operation = command["operation"]
        if operation == "ready":
            return {
                "outcome": "unavailable" if self.uncertain else "ok",
                "epoch": self.config["epoch"],
                "version": 1,
                "monotonic_ns": time.monotonic_ns(),
            }
        if operation == "status":
            state = "busy" if self.active else "unavailable" if self.uncertain else "ready"
            return {
                "outcome": "ok",
                "state": state,
                "attempt_id": self.active["attempt_id"] if self.active else None,
                "epoch": self.config["epoch"],
            }
        if operation == "shutdown":
            self.stopping.set()
            return {"outcome": "ok"}
        sequence = command["sequence"]
        settled = {"outcome": "unavailable" if self.uncertain else "canceled"}
        if sequence in self.terminals:
            original, result = self.terminals[sequence]
            if operation == "cancel" and original == {**command, "operation": "run"}:
                # a cancel never turns an unproven end into a proven one: main must retire (consilium E3-R2)
                return {"outcome": "unavailable"} if result.get("outcome") == "unavailable" else settled
            return result if original == command else {"outcome": "protocol_error"}
        if self.active and self.active["sequence"] == sequence:
            if self.active != {**command, "operation": "run"}:
                return {"outcome": "protocol_error"}
            if operation == "cancel":
                self.active_task.cancel()
                await owned(self.active_task)  # the run's finally has ended and proven the tree by now
                return {"outcome": "unavailable" if self.uncertain else "canceled"}
            return {"outcome": "busy"}
        if sequence <= self.highwater:
            return settled
        if operation == "cancel":
            self.highwater = sequence
            return settled
        if self.uncertain:
            return {"outcome": "unavailable"}
        if self.active:
            return {"outcome": "busy"}
        remaining(command["deadline_ns"])
        self.highwater = sequence
        self.active = command
        self.active_task = asyncio.create_task(self.run(command))
        try:
            result = await asyncio.shield(self.active_task)
        finally:
            self.active = self.active_task = None
        self.terminals[sequence] = (command, result)
        if len(self.terminals) > 256:
            self.terminals.pop(next(iter(self.terminals)))
        return result

    async def run(self, command: dict) -> dict:
        attempt = command["attempt_id"]
        root = self.staging / attempt
        deadline = command["deadline_ns"]
        reply: dict = {"outcome": "crashed"}
        child: PreviewProcess | None = None
        try:
            await disk(root.mkdir)
            await disk(launch, root, "guardian")  # from here, a missing record is unknown, not absent (R12)

            async def spawn():
                nonlocal child
                try:
                    child = await disk(
                        PreviewProcess,
                        "backend.app.part_render",
                        {"root": str(root), "task": command["task"], "deadline_ns": deadline, "node": command["node"]},
                        self.cache,
                        on_spawn=lambda pid: record(root / "guardian.pid", pid),  # before the guardian's bootstrap
                    )
                except SpawnUnproven:
                    # the guardian existed, its start failed and the constructor's cleanup proved nothing. Set
                    # here, inside the owned task, so not even a cancel loses it (consilium E3.2-R1).
                    self.uncertain = True
                    raise
                self.child = child

            await owned(spawn())
            while child.process.poll() is None:
                try:
                    remaining(deadline)
                except PreviewError as exc:
                    raise _Ended("timeout") from exc
                if await disk(child.rss) > RSS_BYTES:
                    raise _Ended("memory_limit")
                if await disk(_bytes_under, root) > ATTEMPT_BYTES + _STAGING_SLACK:
                    raise _Ended("invalid_output")
                await asyncio.sleep(_POLL_SECONDS)
            result = await disk(_read_result, root)
            if result is None:
                raise _Ended("crashed")
            reply = {"outcome": "done", "result": result, "artifact": None}
        except _Ended as end:
            reply = {"outcome": end.outcome}
        except asyncio.CancelledError:
            reply = {"outcome": "canceled"}
        except Exception:
            reply = {"outcome": "crashed"}
        finally:
            if child is not None:
                try:
                    await owned(self._end_tree(child, root))
                except Exception:
                    self.uncertain = True
                self.child = None
        if self.uncertain:
            return {"outcome": "unavailable"}  # staging stays: its owners are not proven gone
        if reply["outcome"] == "done" and reply["result"]["outcome"] == "ok":
            transfer = time.monotonic_ns() + _TRANSFER_SECONDS * 10**9
            try:
                ref = await disk(describe, root / "result.bin", attempt, transfer, "partrender", ATTEMPT_BYTES)
                await put(self.store, root / "result.bin", ref, transfer)
                reply["artifact"] = ref.wire()
            except Exception:
                reply = {"outcome": "invalid_output"}
        cleanup = await disk(cleanup_owned, root)
        if cleanup.status == "retained_error":
            reply["staging_cleanup"] = {
                "path": str(cleanup.path),
                "error": cleanup.error_type,
                "code": cleanup.error_code,
            }
        return reply

    async def _end_tree(self, child: PreviewProcess, root: Path) -> None:
        # EOF -> the guardian kills its group (POSIX) / its child, the Job Object the rest (Windows);
        # stop() raises unless every descendant it saw is reaped
        await disk(child.stop)
        # ... and the records see what stop() could not: a process orphaned out of the guardian's tree. Not
        # strict: an unrecorded process was in the group / job stop() has just killed.
        if not await disk(lambda: tree_gone(root, strict=False)):
            raise PreviewError("unavailable")

    async def stop(self):
        if self.active_task:
            self.active_task.cancel()
            await owned(self.active_task)
        for handler in list(self.handlers):
            if handler is not asyncio.current_task():
                handler.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        if hasattr(self, "nc"):
            await self.nc.close()


async def main():
    config = json.loads(sys.stdin.buffer.readline(16385))
    service = Service(config)
    try:
        await service.start()
        await service.stopping.wait()
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
