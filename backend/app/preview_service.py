"""Local NATS preview control process. Never imports ORM/routes or renders here."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path

import nats

from backend.app.services.preview_artifacts import close_transfer, describe, disk, get, owned, put
from backend.app.services.preview_process import PreviewProcess
from backend.app.services.preview_protocol import (
    ATTEMPT_BYTES,
    CAD_KINDS,
    CAD_TRIANGLES,
    CONTROL_SECONDS,
    JOB_COMMIT_BYTES,
    JSON_BYTES,
    PDF_INPUT_BYTES,
    PDF_PNG_BYTES,
    PDF_RSS_BYTES,
    RSS_BYTES,
    STAGES,
    STEP_WATCHDOG_BYTES,
    TERMINAL_LIMIT,
    TTL_SECONDS,
    VERSION,
    Command,
    PreviewError,
    closed_why,
    decode,
    encode,
    proof_envelope,
    remaining,
)
from backend.app.services.worker_staging import cleanup_owned, quarantine_owned


def _marker(root: Path) -> str | None:
    """The converter's last stage marker, only when it is one of the closed stages (never free text)."""
    try:
        text = (root / "stage").read_text(encoding="ascii")[:16]
    except (OSError, UnicodeError):
        return None
    return text if text in STAGES else None


async def settle(awaitable):
    """Finish one owned cleanup step even when this task is cancelled while it runs (spec §6.1).

    The cancel RPC cancels the run task; landing in its finally, a re-raised CancelledError used to skip the child
    stop and the staging cleanup. The cancellation has already stopped the attempt, so a late one is absorbed
    (uncancel) and the step completes; the STEP's own exception still propagates — a failed child stop must still
    mark the service uncertain."""
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Only a step that was itself cancelled ends here. A step that FINISHED in the very moment the late
            # cancel landed is done, not cancelled: reading "done" as one skipped the rest of the finally — the
            # staging cleanup — while the service still answered cancel_ack (measured under load on the real
            # chain); the next loop returns its result.
            if task.cancelled():
                raise
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                current.uncancel()


def _staging_bytes(root: Path) -> int:
    """The attempt's bytes on disk, as the watchdog counts them every poll. The child renames its own files into
    place meanwhile (the stage marker, result.json, mesh.stl, the picture): an entry gone by the time it is stat'ed
    was renamed or removed after the listing, and its bytes count under the new name at the next poll. Stat'ing the
    listed path again let that rename end the attempt render_failed (CI 39059567831, win-x64-cp313/cp314)."""
    total = 0
    with os.scandir(root) as entries:
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except FileNotFoundError:
                continue
    return total


async def _require_picture(root: Path, operation: str, result: dict) -> None:
    """A PNG child that said ok left a picture: one that exits 0 without it is named, never left to the publish."""
    if not await disk((root / "preview.png").is_file):
        raise PreviewError("render_failed", why="png_missing", by=operation, exit_code=result.get("exit"))


class _CadUnavailable(Exception):
    """The STEP bundle is absent or unusable for this attempt: a nonfatal terminal, never a service fault."""

    def __init__(self, diagnostics: dict | None = None):
        super().__init__("cad_unavailable")
        self.diagnostics = diagnostics or {}


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
                "protocol_version": VERSION,
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
                raise PreviewError("protocol_error")  # a replay is never given for another command on the same seq
            if operation == "cancel" and not self.uncertain:
                # cached only after the run's finally (child stopped and joined): proof of the END, not of success
                return {**cached[2], "proof": proof_envelope(original, "terminal_replay")}
            return cached[2]
        if self.active and command.attempt_seq == self.active.attempt_seq:
            if self.active.wire("run") != command.wire("run"):
                raise PreviewError("protocol_error")
            if operation == "cancel":
                self.task.cancel()
                try:
                    terminal = await owned(self.task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise  # this handler itself is being stopped
                    terminal = None  # the run task ended cancelled (before its first step): it is over
                reply = {"outcome": "unavailable" if self.uncertain else "canceled"}
                if isinstance(terminal, dict) and "staging_cleanup" in terminal:
                    reply["staging_cleanup"] = terminal["staging_cleanup"]
                if isinstance(terminal, dict) and terminal.get("stage") in STAGES:
                    reply["stage"] = terminal["stage"]  # a stopped STEP still names where it was stopped
                if not self.uncertain:
                    reply["proof"] = proof_envelope(command, "cancel_ack")  # reaped and joined
                return reply
            return {"outcome": "busy"}  # duplicate/status never execute again
        if command.attempt_seq <= self.highwater:
            reply = {"outcome": "canceled"}  # evicted terminal cannot resurrect
            if operation == "cancel" and not self.uncertain:
                reply["proof"] = proof_envelope(command, "cancel_ack")  # not active here: nothing of it can be live
            return reply
        if operation == "status":
            return {"outcome": "unavailable"}
        if operation == "cancel":
            self.highwater = command.attempt_seq
            result = {"outcome": "canceled"}
            self.remember(command, result)
            if not self.uncertain:
                return {**result, "proof": proof_envelope(command, "cancel_ack")}  # proven never started
            return result
        if operation != "run":
            raise PreviewError("protocol_error")
        if self.uncertain:
            return {"outcome": "unavailable"}
        if self.active:
            return {"outcome": "busy"}
        remaining(command.deadline_monotonic_ns)
        roles = {x.role for x in command.manifest}
        inputs = {"mesh": {"stl", "obj", *CAD_KINDS}, "sliced": {"3mf"}, "source_png": {"png"}, "document": {"pdf"}}
        if not roles or roles - set(inputs) or not roles & {"mesh", "sliced", "document"}:
            raise PreviewError("protocol_error")
        if any(x.kind not in inputs[x.role] for x in command.manifest):
            raise PreviewError("protocol_error")
        if command.kind == "pdf" and roles != {"document"}:
            raise PreviewError("protocol_error")  # a document is an attempt of its own
        if command.kind == "step" and (roles != {"mesh"} or command.cad is None):
            raise PreviewError("protocol_error")  # a STEP attempt is its mesh alone, with its bundle named
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

    async def render(
        self,
        root: Path,
        operation: str,
        deadline: int,
        kind="stl",
        *,
        module: str = "backend.app.preview_render",
        bootstrap: dict | None = None,
        rss_limit: int = RSS_BYTES,
        job_memory: int | None = None,
        fail_on_error: bool = True,
    ) -> dict:
        """One disposable child, watched every 50 ms against the aggregate RSS of its owned tree (guardian
        included) and the attempt's staging bytes; the deadline is the attempt's ABSOLUTE one, never reset. A
        watchdog stop is the parent's own decision (resource_limit); a child that ends without a result is
        render_failed, whatever memory it was near."""
        payload = bootstrap or {"root": str(root), "operation": operation, "kind": kind, "deadline": deadline}

        async def spawn():
            self.child = await disk(
                PreviewProcess, module, payload, Path(self.config["cache"]), job_memory_limit=job_memory
            )

        try:
            await owned(spawn())
            while self.child.process.poll() is None:
                remaining(deadline)
                if await disk(self.child.rss) > rss_limit:
                    raise PreviewError("resource_limit", why="rss", by=operation)
                if await disk(_staging_bytes, root) > ATTEMPT_BYTES:
                    raise PreviewError("resource_limit", why="staging_bytes", by=operation)
                await asyncio.sleep(0.05)
            code = self.child.process.poll()  # the guardian's, which is its child's
            result_path = root / "result.json"

            def read_result():
                if result_path.stat().st_size > JSON_BYTES:
                    raise PreviewError("protocol_error", by=operation, exit_code=code)
                return decode(result_path.read_bytes())

            try:
                result = await disk(read_result)
            except FileNotFoundError:
                # a crash writes no result: no OOM claim
                raise PreviewError("render_failed", why="no_result", by=operation, exit_code=code) from None
            if fail_on_error and result.get("outcome") != "ok":
                raise PreviewError(
                    result.get("outcome", "render_failed"), why=result.get("why"), by=operation, exit_code=code
                )
            await disk(result_path.unlink)
            result["exit"] = code  # the child's real exit code, never one it wrote itself
            return result
        finally:
            if self.child is not None:
                await disk(self.child.stop)  # the next phase never starts before this one is proven over
                self.child = None

    def cad_site(self, cad: dict) -> Path | None:
        """The service's own reconstruction of the bundle main named: same catalog digest, same key for THIS
        interpreter, a receipt that matches — else None (cad_unavailable). Never a path from the command."""
        from backend.app import step_runtime

        try:
            app_dir = Path(self.config.get("app_dir") or Path(__file__).resolve().parents[2])
            found = step_runtime.locate(app_dir)  # no pip: the final Docker image has none
        except Exception:
            return None
        if not found.ok or found.digest != cad["manifest_digest"] or found.key != cad["bundle_key"]:
            return None
        return found.site

    async def step(self, root: Path, command: Command, store, progress: dict) -> None:
        """STEP: conversion child, its proven end, then the PNG child — one slot, one attempt id, one absolute
        deadline; the PNG never starts while the converter lives."""
        deadline = command.deadline_monotonic_ns
        ref = command.manifest[0]
        await get(store, ref, root / "input.step", deadline)
        progress["stage"] = "cad_import"
        site = await disk(self.cad_site, command.cad)
        if site is None:
            raise _CadUnavailable
        try:
            result = await self.render(
                root,
                "convert",
                deadline,
                module="backend.app.services.step_thumbnail",
                bootstrap={"root": str(root), "deadline": deadline, "site": str(site)},
                rss_limit=STEP_WATCHDOG_BYTES,
                job_memory=JOB_COMMIT_BYTES,
                fail_on_error=False,
            )
        except BaseException:  # a stop, a timeout and a cancel or preempt alike name where the converter was
            progress["stage"] = await settle(disk(_marker, root)) or progress["stage"]
            raise
        finally:
            await disk(lambda: (root / "stage").unlink(missing_ok=True))
        if result.get("stage") in STAGES:
            progress["stage"] = result["stage"]
        said = {"why": result.get("why"), "by": "convert", "exit_code": result.get("exit")}
        if result.get("outcome") == "cad_unavailable":
            raise _CadUnavailable(PreviewError("cad_unavailable", **said).diagnostics())
        if result.get("outcome") != "ok":
            raise PreviewError(result.get("outcome", "render_failed"), **said)
        triangles = result.get("triangles")
        if type(triangles) is not int or not 0 < triangles <= CAD_TRIANGLES:
            raise PreviewError("render_failed", **{**said, "why": "triangles"})
        await disk((root / "input.step").unlink)  # the PNG child gets only the mesh
        progress["stage"] = "png"
        png = await self.render(root, "cad_png", deadline, rss_limit=STEP_WATCHDOG_BYTES, job_memory=JOB_COMMIT_BYTES)
        await _require_picture(root, "cad_png", png)

    async def pdf(self, root: Path, command: Command) -> None:
        """A PDF's first page: one disposable child under the PDF budgets — the input's size before the child
        exists, its RSS while it runs, the picture's bytes after it; the attempt's absolute deadline is main's."""
        if command.manifest[0].size > PDF_INPUT_BYTES:
            raise PreviewError("resource_limit", why="pdf_input_bytes", by="service")
        result = await self.render(root, "pdf_png", command.deadline_monotonic_ns, "pdf", rss_limit=PDF_RSS_BYTES)
        await _require_picture(root, "pdf_png", result)
        if await disk(lambda: (root / "preview.png").stat().st_size) > PDF_PNG_BYTES:
            raise PreviewError("resource_limit", why="pdf_png_bytes", by="pdf_png", exit_code=result.get("exit"))

    async def run(self, command: Command):
        root = Path(self.config["staging"]) / command.attempt_id
        deadline = command.deadline_monotonic_ns
        total = sum(x.size for x in command.manifest)
        transfer_nc = None
        reply = {"outcome": "unavailable"}
        progress = {"stage": "snapshot"} if command.kind == "step" else None
        try:
            await disk(root.mkdir)
            transfer_nc = await nats.connect(
                self.config["url"], token=self.config["token"], allow_reconnect=False, connect_timeout=CONTROL_SECONDS
            )
            transfer_store = await transfer_nc.jetstream(timeout=CONTROL_SECONDS).object_store(self.config["bucket"])
            if command.kind != "step":
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
            elif command.kind == "step":
                await self.step(root, command, transfer_store, progress)
                await publish("preview", "png")
            elif command.kind == "pdf":
                await self.pdf(root, command)
                await publish("preview", "png")
            else:
                mesh = next(x for x in command.manifest if x.role == "mesh")
                await _require_picture(root, "mesh", await self.render(root, "mesh", deadline, mesh.kind))
                await publish("preview", "png")
            reply = {"outcome": "ok", "manifest": outputs}
            return reply
        except asyncio.CancelledError:
            reply = {"outcome": "canceled"}
            return reply
        except _CadUnavailable as exc:
            reply = {"outcome": "cad_unavailable", **exc.diagnostics}
            return reply
        except PreviewError as exc:
            reply = {"outcome": exc.outcome, **exc.diagnostics()}
            return reply
        except Exception as exc:
            # The service's own failure: its class names it, never its text (which can carry a path).
            reply = {"outcome": "render_failed", "by": "service"}
            if why := closed_why(type(exc).__name__):
                reply["why"] = why
            return reply
        finally:
            if progress is not None:
                reply["stage"] = progress["stage"]
            if transfer_nc:
                await settle(close_transfer(transfer_nc))
            if self.child is not None:
                try:
                    await settle(asyncio.to_thread(self.child.stop))
                    self.child = None
                except Exception:
                    self.uncertain = True
            if not self.uncertain:
                cleanup = await settle(asyncio.to_thread(cleanup_owned, root))
                quarantined = False
                if cleanup.status == "retained_error" and self.config.get("quarantine"):
                    # this service owns the dir and its run is over (child reaped): it may move it out of staging
                    moved = await settle(
                        asyncio.to_thread(
                            quarantine_owned,
                            root,
                            Path(self.config["quarantine"]),
                            self.config["generation"],
                            "service",
                        )
                    )
                    quarantined = moved.status == "quarantined"
                if cleanup.status == "retained_error":
                    reply["staging_cleanup"] = {
                        "path": str(cleanup.path),
                        "error": cleanup.error_type,
                        "code": cleanup.error_code,
                        "quarantined": quarantined,
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
