"""A separate process (and storage mutex) for C04's real PostgreSQL deadlock witness."""

import asyncio
import json
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def main():
    payload = json.loads(sys.stdin.read())
    from backend.app.core import database
    from backend.app.models.library import LibraryFile
    from backend.app.services.library_trash import library_trash_service

    database.import_all_models()
    engine = create_async_engine(
        os.environ["G02_C_TEST_PG_URL"],
        connect_args={
            "server_settings": {
                "search_path": payload["schema"],
                "application_name": payload["schema"],
                "lock_timeout": "10000",
            }
        },
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.async_session = factory
    try:
        async with factory() as db:
            row = await db.get(LibraryFile, payload["file_id"])
            assert row is not None
            print("worker_preflight", flush=True)
            await library_trash_service.hard_delete_now(db, row)
        print("worker_deleted", flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
