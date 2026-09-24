"""Local NATS preview control process. Never imports ORM/routes or renders here."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path

import nats

from backend.app.services.preview_artifacts import describe, disk, get, owned, put
from backend.app.services.preview_process import PreviewProcess
from backend.app.services.preview_protocol import (
    ATTEMPT_BYTES,
    CONTROL_SECONDS,
    JSON_BYTES,
    RSS_BYTES,
    TERMINAL_LIMIT,
    TTL_SECONDS,
    Command,
    PreviewError,
    decode,
    encode,
    remaining,
)
from backend.app.services.worker_staging import cleanup_owned


class Service:
    def __init__(self, config: dict):
        self.config = config
        self.highwater = 0
        self.terminals = OrderedDict()
        self.active: Command | None = None
        self.task = None
        self.child = None
        self.stopping = asyncio.Event()
        self.handlers = set()
        self.uncertain = False

    async def start(self):
        self.nc = await nats.connect(
            self.config["url"],
            token=self.config["token"],
            allow_reconnect=False,
            connect_timeout=CONTROL_SECONDS,
            closed_cb=self.disconnected,
        )
        self.store = await self.nc.jetstream(timeout=CONTROL_SECONDS).object_store(self.config["bucket"])
        self.subject = f"bamdude.preview.{self.config['generation']}.{self.config['epoch']}"
        await self.nc.subscribe(self.subject, cb=self.receive)
        await self.nc.flush(timeout=CONTROL_SECONDS)

    async def disconnected(self):
        self.stopping.set()
        if self.task:
            self.task.cancel()

    async def receive(self, message):
        # A long run reply must not serialize cancel/status behind the render.
        if len(self.handlers) >= 16:
            await message.respond(encode({"outcome": "busy"}))
            return
        task = asyncio.create_task(self.handle(message))
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    def remember(self, command: Command, result: dict):
        self.terminals[command.attempt_seq] = (time.monotonic(), command, result)
        while self.terminals and (
            len(self.terminals) > TERMINAL_LIMIT
            or next(iter(self.terminals.values()))[0] < time.monotonic() - TTL_SECONDS
        ):
            self.terminals.popitem(last=False)

    async def handle(self, message):
        try:
            command = Command.parse(decode(message.data), self.config["generation"], self.config["epoch"])
            result = await self.command(command)
        except PreviewError as exc:
            result = {"outcome": exc.outcome}
        except Exception:
            result = {"outcome": "unavailable"}
        try:
            await message.respond(encode(result))
        except Exception:
            pass

    async def command(self, command: Command) -> dict:
        while self.terminals and next(iter(self.terminals.values()))[0] < time.monotonic() - TTL_SECONDS:
            self.terminals.popitem(last=False)
        operation = command.operation
        if operation == "ready":
            await self.store.status()
            return {
                "outcome": "ok",
                "protocol_version": 1,
                "epoch": self.config["epoch"],
                "monotonic_ns": time.monotonic_ns(),
            }
        if operation == "shutdown":
            self.stopping.set()
            return {"outcome": "ok"}
        cached = self.terminals.get(command.attempt_seq)
        if cached:
            original = cached[1]
            if original.wire("run") != command.wire("run"):
                raise PreviewError("protocol_error")
            return cached[2]
        if self.active and command.attempt_seq == self.active.attempt_seq:
            if self.active.wire("run") != command.wire("run"):
                raise PreviewError("protocol_error")
            if operation == "cancel":
                self.task.cancel()
                terminal = await owned(self.task)
                reply = {"outcome": "unavailable" if self.uncertain else "canceled"}
                if isinstance(terminal, dict) and "staging_cleanup" in terminal:
                    reply["staging_cleanup"] = terminal["staging_cleanup"]
                return reply
            return {"outcome": "busy"}  # duplicate/status never execute again
        if command.attempt_seq <= self.highwater:
            return {"outcome": "canceled"}  # evicted terminal cannot resurrect
        if operation == "status":
            return {"outcome": "unavailable"}
        if operation == "cancel":
            self.highwater = command.attempt_seq
            result = {"outcome": "canceled"}
            self.remember(command, result)
            return result
        if operation != "run":
            raise PreviewError("protocol_error")
        if self.uncertain:
            return {"outcome": "unavailable"}
        if self.active:
            return {"outcome": "busy"}
        remaining(command.deadline_monotonic_ns)
        roles = {x.role for x in command.manifest}
        if not roles or roles - {"mesh", "sliced", "source_png"} or not roles & {"mesh", "sliced"}:
            raise PreviewError("protocol_error")
        if any(
            x.kind not in ({"stl", "obj"} if x.role == "mesh" else {"3mf"} if x.role == "sliced" else {"png"})
            for x in command.manifest
        ):
            raise PreviewError("protocol_error")
        self.highwater = command.attempt_seq
        self.active = command
        self.task = asyncio.create_task(self.run(command))
        try:
            result = await asyncio.shield(self.task)
        except Exception:
            result = {"outcome": "unavailable"}
        finally:
            self.active = None
            self.task = None
        if self.uncertain:
            result = {"outcome": "unavailable"}
        self.remember(command, result)
        return result

    async def render(self, root: Path, operation: str, deadline: int, kind="stl"):
        async def spawn():
            self.child = await disk(
                PreviewProcess,
                "backend.app.preview_render",
                {"root": str(root), "operation": operation, "kind": kind, "deadline": deadline},
                Path(self.config["cache"]),
            )

        try:
            await owned(spawn())
            while self.child.process.poll() is None:
                remaining(deadline)
                if await disk(self.child.rss) > RSS_BYTES:
                    raise PreviewError("resource_limit")
                if await disk(lambda: sum(p.stat().st_size for p in root.iterdir() if p.is_file())) > ATTEMPT_BYTES:
                    raise PreviewError("resource_limit")
                await asyncio.sleep(0.05)
            result_path = root / "result.json"

            def read_result():
                if result_path.stat().st_size > JSON_BYTES:
                    raise PreviewError("protocol_error")
                return decode(result_path.read_bytes())

            result = await disk(read_result)
            if result.get("outcome") != "ok":
                raise PreviewError(result.get("outcome", "render_failed"))
            await disk(result_path.unlink)
        finally:
            if self.child is not None:
                await disk(self.child.stop)
                self.child = None

    async def run(self, command: Command):
        root = Path(self.config["staging"]) / command.attempt_id
        deadline = command.deadline_monotonic_ns
        total = sum(x.size for x in command.manifest)
        transfer_nc = None
        reply = {"outcome": "unavailable"}
        try:
            await disk(root.mkdir)
            transfer_nc = await nats.connect(
                self.config["url"], token=self.config["token"], allow_reconnect=False, connect_timeout=CONTROL_SECONDS
            )
            transfer_store = await transfer_nc.jetstream(timeout=CONTROL_SECONDS).object_store(self.config["bucket"])
            for ref in command.manifest:
                await get(transfer_store, ref, root / f"{ref.role}.{ref.kind}", deadline)
            outputs = []

            async def publish(role, kind):
                nonlocal total
                path = root / f"{role}.{kind}"
                ref = await disk(describe, path, command.attempt_id, role, kind, deadline)
                total += ref.size
                if total > ATTEMPT_BYTES:
                    raise PreviewError("resource_limit")
                await put(transfer_store, path, ref, deadline)
                outputs.append(ref.wire())

            if any(x.role == "sliced" for x in command.manifest):
                try:
                    await self.render(root, "source", deadline)
                except PreviewError as exc:
                    if exc.outcome != "render_failed":
                        raise
                    # Rendering a usable source PNG and injecting it into ZIP
                    # are separate results. Keep the former even if ZIP failed.
                    await disk(shutil.copyfile, root / "sliced.3mf", root / "checkpoint.3mf")
                if await disk((root / "preview.png").exists):
                    await publish("preview", "png")
                await publish("checkpoint", "3mf")
                receipt = await self.nc.request(
                    command.subject + ".checkpoint",
                    encode({"command": command.wire("checkpoint-receipt"), "manifest": outputs}),
                    timeout=remaining(deadline),
                )
                if decode(receipt.data).get("outcome") != "ok":
                    raise PreviewError("canceled")
                await disk(shutil.copyfile, root / "checkpoint.3mf", root / "sliced.3mf")
                await self.render(root, "plates", deadline)
                await publish("output", "3mf")
            else:
                await self.render(root, "mesh", deadline, command.manifest[0].kind)
                await publish("preview", "png")
            reply = {"outcome": "ok", "manifest": outputs}
            return reply
        except asyncio.CancelledError:
            reply = {"outcome": "canceled"}
            return reply
        except PreviewError as exc:
            reply = {"outcome": exc.outcome}
            return reply
        except Exception:
            reply = {"outcome": "render_failed"}
            return reply
        finally:
            if transfer_nc:
                await owned(transfer_nc.close())
            if self.child is not None:
                try:
                    await disk(self.child.stop)
                    self.child = None
                except Exception:
                    self.uncertain = True
            if not self.uncertain:
                cleanup = await disk(cleanup_owned, root)
                if cleanup.status == "retained_error":
                    reply["staging_cleanup"] = {
                        "path": str(cleanup.path),
                        "error": cleanup.error_type,
                        "code": cleanup.error_code,
                    }

    async def stop(self):
        if self.task:
            self.task.cancel()
            await owned(self.task)
        for task in list(self.handlers):
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        await self.nc.close()


async def main():
    config = json.loads(sys.stdin.buffer.readline(16385))
    service = Service(config)
    try:
        await service.start()
        await service.stopping.wait()
    finally:
        if hasattr(service, "nc"):
            await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
