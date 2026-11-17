"""D418: owned native Windows cluster, actual consent restore and cold PG restart."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path


async def main(stop_only=False):
    from backend.app.core import database
    from backend.app.core.config import settings
    from backend.app.core.schema_repair_state import DEBT, read_record, write_pending
    from backend.app.services import embedded_postgres as pg

    root = Path(os.environ["D4_NATIVE_ROOT"]).resolve()
    assert settings.embedded_postgres and not settings.embedded_pg_external_service
    assert settings.data_dir.resolve() == root
    assert settings.embedded_pg_data_dir.resolve().is_relative_to(root / "postgres")
    if stop_only:
        await pg.stop()
        return {"stopped": not await pg.is_running()}
    try:
        await pg.start()
        from backend.tests.integration.schema_consent_runner import main as restore

        result = await restore()
        assert result["confirmed_http_restore"] and result["dialect"] == "postgresql"
        async with database.engine.begin() as conn:
            await write_pending(conn, database.Base.metadata)
        await database.engine.dispose()
        await pg.stop()
        assert not await pg.is_running()
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "backend.app.cli", "schema_repair", "retry"],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=150,
        )
        assert result.returncode == 0, result.stdout[-1000:] + result.stderr[-3000:]
        assert not await pg.is_running()  # CLI cleaned up only the child it started.
        await pg.start()
        await database.init_db()
        async with database.engine.begin() as conn:
            assert await read_record(conn, DEBT) is None
        return {
            "windows_native": sys.platform == "win32",
            "actual_http_restore": True,
            "cold_pg_restart": True,
            "debt": None,
        }
    finally:
        await database.engine.dispose()
        await pg.stop()
        assert not await pg.is_running()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1:2] == ["stop"]))))
