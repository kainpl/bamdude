"""Small local process owner; no worker receives bootstrap before containment."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import psutil

from backend.app.services.preview_protocol import LOG_BYTES, PreviewError, encode
from backend.app.services.worker_containment import WorkerContainment
from backend.app.services.worker_process import descendants, descendants_reaped, kill_owned_group


class PreviewProcess:
    def __init__(self, module: str, bootstrap: dict, cache: Path):
        # No .env, database URL, API credentials or NATS token in renderer env.
        keys = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
            "MPLCONFIGDIR",
        }
        env = {key: value for key, value in os.environ.items() if key.upper() in keys}
        env.update(BAMDUDE_IGNORE_DOTENV="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            env[key] = "1"
        cache.mkdir(parents=True, exist_ok=True)
        env.setdefault("MPLCONFIGDIR", str(cache))
        self.tail = bytearray()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "backend.app.worker_guardian"],
            cwd=Path(__file__).resolve().parents[3],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.containment = None
        try:
            self.containment = WorkerContainment.attach(self.process.pid)
            self.reader = threading.Thread(target=self._drain, daemon=True)
            self.reader.start()
            self.process.stdin.write(encode({"module": module, **bootstrap}) + b"\n")
            self.process.stdin.flush()
        except BaseException:
            self.stop()
            raise

    def _drain(self):
        while block := self.process.stdout.read(4096):
            self.tail.extend(block)
            if len(self.tail) > LOG_BYTES:
                del self.tail[:-LOG_BYTES]

    def rss(self) -> int:
        try:
            process = psutil.Process(self.process.pid)
            children = process.children(recursive=True)
            return sum(p.memory_info().rss for p in [process, *children] if p.is_running())
        except psutil.NoSuchProcess:
            return 0

    def stop(self):
        process = self.process
        children = descendants(process.pid)
        if process.stdin:
            process.stdin.close()  # EOF guardian kills even detached grandchildren
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    kill_owned_group(process.pid)
                elif self.containment:
                    self.containment.close()
                else:
                    process.kill()
                process.wait(timeout=5)
        if self.containment:
            self.containment.close()
        if os.name != "nt":
            # The group can outlive its leader (guardian crash). It is the
            # owned session we created, not a PID adopted from a marker.
            kill_owned_group(process.pid)
        if not descendants_reaped(children):
            raise PreviewError("unavailable")  # no replacement while ownership is uncertain
        if hasattr(self, "reader"):
            self.reader.join(timeout=5)
            if self.reader.is_alive():
                raise PreviewError("unavailable")
        if process.stdout:
            process.stdout.close()
