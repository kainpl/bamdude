"""Independent local NATS analysis service with one reusable guarded parser child."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import time
from pathlib import Path

import nats

from backend.app.services.analysis_source import AnalysisSource
from backend.app.services.analysis_transport import ARTIFACT_BYTES, describe, put
from backend.app.services.preview_artifacts import disk, owned
from backend.app.services.preview_process import PreviewProcess
from backend.app.services.preview_protocol import CONTROL_SECONDS, PreviewError, decode, encode, remaining

_HEX = re.compile(r"[0-9a-f]{32}\Z")
_RSS_BYTES = 1024**3


class Service:
    def __init__(self, config: dict):
        self.config = config
        self.subject = f"bamdude.analysis.{config['generation']}.{config['epoch']}"
        self.staging = Path(config["staging"])
        self.archive_root = Path(config["archive_root"])
        self.child = None
        self.child_ready = False
        self.active = None
        self.active_task = None
        self.highwater = 0
        self.terminals = {}
        self.stopping = asyncio.Event()
        self.handlers = set()
        self.uncertain = False
        self.idle_monitor = None

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
        await self.start_child()
        self.idle_monitor = asyncio.create_task(self.watch_idle_child(), name="analysis-idle-child-monitor")

    async def watch_idle_child(self):
        """Retire an idle child that crashed or retained too much memory."""
        while not self.stopping.is_set():
            await asyncio.sleep(0.5)
            child = self.child
            if self.active or child is None or self.uncertain:
                continue
            dead = child.process.poll() is not None
            over_budget = not dead and await disk(child.rss) > _RSS_BYTES
            if self.active or self.child is not child:
                continue
            if dead or over_budget:
                try:
                    await self.retire_child()
                except Exception:
                    self.uncertain = True

    async def disconnected(self):
        self.stopping.set()
        if self.active_task:
            self.active_task.cancel()

    async def start_child(self):
        if self.uncertain:
            raise PreviewError("unavailable")
        ready = self.staging / "child.ready"
        if ready.exists():
            await disk(ready.unlink)

        async def spawn():
            self.child = await disk(
                PreviewProcess,
                "backend.app.analysis_child",
                {"staging": str(self.staging), "archive_root": str(self.archive_root)},
                self.staging / "cache",
            )

        await owned(spawn())
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.child.process.poll() is not None:
                raise PreviewError("unavailable")
            if await disk(ready.exists):
                self.child_ready = True
                return
            await asyncio.sleep(0.05)
        raise PreviewError("timeout")

    async def retire_child(self):
        self.child_ready = False
        child, self.child = self.child, None
        if child:
            try:
                await disk(child.stop)
            except Exception:
                self.uncertain = True
                raise

    async def receive(self, message):
        if len(self.handlers) >= 16:
            await message.respond(encode({"outcome": "busy"}))
            return
        task = asyncio.create_task(self.handle(message))
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    def parse(self, raw: bytes) -> dict:
        data = decode(raw)
        if set(data) != {
            "version",
            "generation",
            "epoch",
            "attempt_id",
            "sequence",
            "operation",
            "deadline_ns",
            "source",
        }:
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
            or data["operation"] not in {"ready", "run", "status", "cancel", "shutdown"}
            or type(data["deadline_ns"]) is not int
        ):
            raise PreviewError("protocol_error")
        if data["operation"] in {"run", "cancel"} and data["source"] is not None:
            source = AnalysisSource.from_payload(data["source"])
            path = Path(source.path)
            try:
                path.resolve(strict=True).relative_to(self.archive_root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise PreviewError("protocol_error") from exc
        elif data["operation"] == "run" or data["source"] is not None:
            raise PreviewError("protocol_error")
        return data

    async def handle(self, message):
        try:
            command = self.parse(message.data)
            result = await self.command(command)
        except (PreviewError, ValueError) as exc:
            result = {"outcome": exc.outcome if isinstance(exc, PreviewError) else "protocol_error"}
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
                "outcome": "ok"
                if self.child_ready and self.child and self.child.process.poll() is None
                else "unavailable",
                "epoch": self.config["epoch"],
                "version": 1,
                "monotonic_ns": time.monotonic_ns(),
            }
        if operation == "status":
            return {
                "outcome": "ok",
                "state": "busy"
                if self.active
                else "ready"
                if self.child_ready and self.child and self.child.process.poll() is None and not self.uncertain
                else "unavailable",
                "attempt_id": self.active["attempt_id"] if self.active else None,
                "epoch": self.config["epoch"],
            }
        if operation == "shutdown":
            self.stopping.set()
            return {"outcome": "ok"}
        sequence = command["sequence"]
        if sequence in self.terminals:
            original, result = self.terminals[sequence]
            # Cancellation may arrive after the run has committed its terminal
            # result. The caller no longer wants that result, but ownership is
            # already settled: acknowledge the matching cancel without forcing
            # the client to retire an otherwise healthy service/child.
            if operation == "cancel" and original == {**command, "operation": "run"}:
                return {"outcome": "canceled"}
            return result if original == command else {"outcome": "protocol_error"}
        if self.active and self.active["sequence"] == sequence:
            if self.active != {**command, "operation": "run"}:
                return {"outcome": "protocol_error"}
            if operation == "cancel":
                self.active_task.cancel()
                await owned(self.active_task)
                return {"outcome": "unavailable" if self.uncertain else "canceled"}
            return {"outcome": "busy"}
        if sequence <= self.highwater:
            return {"outcome": "canceled"}
        if operation == "cancel":
            self.highwater = sequence
            return {"outcome": "canceled"}
        if operation != "run" or self.uncertain or self.active:
            return {"outcome": "busy" if self.active else "unavailable"}
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

    async def run(self, command: dict):
        attempt = command["attempt_id"]
        root = self.staging / attempt
        deadline = command["deadline_ns"]
        try:
            await disk(root.mkdir)
            if self.child is None or self.child.process.poll() is not None:
                await self.retire_child()
                await self.start_child()
            await disk(self.child.send, {"attempt_id": attempt, "source": command["source"]})
            result_path = root / "result.json"
            while not await disk(result_path.exists):
                remaining(deadline)
                if self.child.process.poll() is not None:
                    raise PreviewError("unavailable")
                if await disk(self.child.rss) > _RSS_BYTES:
                    raise PreviewError("resource_limit")
                await asyncio.sleep(0.25)
            compute_complete_ns = time.monotonic_ns()
            result = await disk(lambda: json.loads(result_path.read_text(encoding="utf-8")))
            if result != {"outcome": "ok"}:
                raise PreviewError("resource_limit" if result.get("outcome") == "resource_limit" else "unavailable")
            output = root / "output.bin"
            if await disk(lambda: output.stat().st_size) > ARTIFACT_BYTES:
                raise PreviewError("resource_limit")
            transfer_deadline = min(deadline + 60 * 10**9, compute_complete_ns + 60 * 10**9)
            ref = await disk(describe, output, attempt, transfer_deadline)
            await put(self.store, output, ref, transfer_deadline)
            return {"outcome": "ok", "artifact": ref.wire(), "compute_complete_ns": compute_complete_ns}
        except asyncio.CancelledError:
            try:
                await owned(self.retire_child())
            except Exception:
                self.uncertain = True
            return {"outcome": "canceled"}
        except PreviewError as exc:
            try:
                await owned(self.retire_child())
            except Exception:
                self.uncertain = True
            return {"outcome": exc.outcome}
        except Exception:
            try:
                await owned(self.retire_child())
            except Exception:
                self.uncertain = True
            return {"outcome": "unavailable"}
        finally:
            if not self.uncertain:
                await disk(shutil.rmtree, root, True)

    async def stop(self):
        if self.idle_monitor:
            self.idle_monitor.cancel()
            await asyncio.gather(self.idle_monitor, return_exceptions=True)
        if self.active_task:
            self.active_task.cancel()
            await owned(self.active_task)
        await self.retire_child()
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
