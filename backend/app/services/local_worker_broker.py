"""One lifespan-owned loopback broker shared by disposable worker services."""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path

from backend.app.services.preview_artifacts import disk
from backend.app.services.preview_protocol import CONTROL_SECONDS, STARTUP_SECONDS

logger = logging.getLogger(__name__)


class LocalWorkerBroker:
    def __init__(self, root: Path):
        # Keep the original lease domain and store location. Moving it would
        # permit two owners to write the same JetStream store after upgrade.
        self.root = root
        self.token = secrets.token_urlsafe(32)
        self.server = None
        self.nc = None

    @property
    def url(self) -> str:
        return self.server.url

    async def start(self) -> None:
        import nats
        from embedded_nats import NatsServer

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.server = NatsServer(
            self.root / "broker",
            auth_token=self.token,
            max_file_store="3072MB",
            max_memory_store="64MB",
            max_payload=1024**2,
            startup_timeout=15,
            shutdown_timeout=2,
            recover_stale=True,
        )
        try:
            async with asyncio.timeout(STARTUP_SECONDS):
                await disk(self.server.start)
                self.nc = await nats.connect(
                    self.server.url,
                    token=self.token,
                    allow_reconnect=False,
                    connect_timeout=CONTROL_SECONDS,
                )
        except BaseException:
            try:
                await self.stop()
            except Exception:
                logger.warning("Local worker broker cleanup needs manual verification")
            raise
        if self.server.recovered_generation:
            logger.warning("Local worker broker recovered abandoned generation=%s", self.server.recovered_generation)

    async def stop(self) -> None:
        if self.nc:
            await self.nc.close()
            self.nc = None
        if self.server:
            try:
                await disk(self.server.stop)
            finally:
                self.server = None


_owner: LocalWorkerBroker | None = None


def get_local_worker_broker() -> LocalWorkerBroker | None:
    return _owner


async def start_local_worker_broker(base: Path) -> LocalWorkerBroker:
    global _owner
    if _owner is not None:
        raise RuntimeError("local worker broker already started")
    owner = LocalWorkerBroker(base / ".cache" / "preview-service")
    await owner.start()
    _owner = owner
    return owner


async def stop_local_worker_broker() -> None:
    global _owner
    owner, _owner = _owner, None
    if owner:
        await owner.stop()
